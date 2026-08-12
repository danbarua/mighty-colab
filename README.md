# Mighty-Colab

A fork of Google's official [`colab` CLI](https://github.com/googlecolab/google-colab-cli),
hardened for AI agents driving it end-to-end — provisioning GPU/TPU runtimes, executing
code, and tearing sessions down — with no human watching the terminal.

`mighty-colab` installs as a separate binary and coexists with the official `colab` CLI.
Every base command (`new`, `exec`, `install`, `run`, `ssh`, …) takes the same flags and does
the same thing as `colab`'s own docs describe (a handful of exit-code and stream-hygiene
fixes noted below aside), so this README doesn't re-cover that ground — see the
[official CLI](https://github.com/googlecolab/google-colab-cli) for those fundamentals, or
[`googlecolab/colab-mcp`](https://github.com/googlecolab/colab-mcp) if you want in-notebook,
interactive agent assistance rather than a terminal/automation workflow.

This document is about what `mighty-colab` adds on top: **structured JSON output** for
programmatic callers, **background execution** for long-running jobs, and a set of fixes
and extensions that came out of running a real, multi-month, agent-driven research pipeline
against the official CLI and hitting failures a human at a terminal would rarely notice.

[Demo](https://github.com/user-attachments/assets/656226a9-af13-4fdb-8eda-d7de747336a2)

> [!NOTE]
> **Platform support:** Linux and macOS only. Windows is not supported at this time.

---

## Why this exists

A CLI designed for a human conveys a lot of information *typographically* — a spinner, a
color, a line of prose — that a program driving the same CLI can't see. Three examples that
shaped this fork:

1. **A zero exit code isn't proof the work happened.** The official `colab exec` used to
   exit `0` even when the remote script raised. Fixed upstream-side of this fork
   (`679c0b6`) — but even a correct exit code can't distinguish "ran and passed" from
   "exited before reaching its verdict," which is the problem `--json` solves properly (see
   below).
2. **`exec --timeout` bounds the gap between outputs, not the run.** A script that computes
   silently for a while — model compilation, a slow download — looks identical to a hung
   one. Nothing to do here except know it and pass a generous `--timeout` explicitly; see
   `mighty-colab skill`.
3. **A blocking CLI call doesn't fit a tool-call model.** `exec` ties up the caller for the
   entire remote run. For an MCP client (or any request/response caller) that's a call that
   might never return. `exec-async` exists so a background job returns in about a second
   regardless of how long the work takes.

The full list — sixteen upstream defects fixed, five in this fork's own additions, all with
reproducing tests — is in
[`docs/AGENT_USABILITY_LEARNINGS.md`](docs/AGENT_USABILITY_LEARNINGS.md).

---

## Installation

`mighty-colab` is [published on PyPI](https://pypi.org/project/mighty-colab/). Install it
using `uv` (recommended) or standard `pip`:

```bash
# Using uv (recommended)
uv tool install mighty-colab
# Using pip
pip install mighty-colab
```

> [!NOTE]
> `--json` (below) has landed on `main` but not yet in a tagged PyPI release — the latest
> release is `v0.3.0`. Install from git (`uv tool install git+https://github.com/danbarua/mighty-colab`)
> to get it before the next release. `exec-async` shipped in `v0.3.0` and is on PyPI today.

---

## Structured output: `--json`

Every command an agent actually drives in a loop — `exec`, `run`, `exec-async`,
`log --tail`, `new`, `stop`, `sessions`, `status` — accepts a global `--json` flag that
replaces human-readable output with a single, schema-validated JSON envelope on stdout.
`[colab] ...` chatter moves to stderr, so a `| jq` pipeline sees JSON and nothing else. Even
CLI-level parse errors (an unknown flag, a missing argument) emit an envelope instead of a
Rich-boxed stderr display.

```console
$ mighty-colab --json sessions
{"schema_version": "1", "cli_version": "3b3ffcd", "command": "sessions", "status": "ok", "exit_code": 0, "sessions": []}

$ mighty-colab --json exec -s nonexistent-session <<< "print(1)"
{"schema_version": "1", "cli_version": "3b3ffcd", "command": "exec", "status": "error", "exit_code": 1, "reason": "session_not_found"}
```

Every envelope carries `schema_version`/`cli_version` (so a stale install is visible in the
output itself, not just `--help`) plus a `status` of `"ok"` / `"job_raised"` / `"error"`, an
`exit_code`, and a `reason` when something didn't succeed. The design detail that matters
most for automation: **the CLI process's own exit code stays `0` under `--json` whenever it
mechanically completed its transaction, even if the remote job raised.** The job's own
outcome lives in the envelope body instead. This exists because of a real incident: a script
that finished successfully and then called `sys.exit(0)` was misread by the old exit-code
path as a failure (IPython reports `SystemExit` as an error-type kernel output), and a
Makefile recipe tore the session down with its only result still on it. Under `--json`,
`sys.exit(0)`/`None`/`False` always normalizes to `status: "ok"`.

Query commands (`status`, `exec`, `run`, …) error on "not found"; desired-state commands
don't — `stop --json` on an already-absent session reports `status: "ok", reason:
"already_stopped"`, because that's the strongest available evidence nothing is still
billing, and an unconditional teardown-on-every-path pattern depends on it staying that way.

A worked example composing an entire session lifecycle with nothing but `--json` and `jq` —
`new` → `exec` → `exec-async` → `log --tail --since-offset` → `stop` — is in
[`integration/repro_json_jq_lifecycle/test.sh`](integration/repro_json_jq_lifecycle/test.sh).

> [!NOTE]
> `--json` is not yet wired into the embedded MCP server (below) — `mighty-colab mcp` tool
> calls still return plain text. Structured MCP output is a known, deliberate gap; see
> [`docs/07_mcp_server.md`](docs/07_mcp_server.md).

---

## Background execution: `exec-async`

`exec` blocks the caller for the entire remote run. For anything long — training,
data downloads, a multi-minute install — that's a held-open connection an agent's own
context or an MCP request/response cycle may not survive.

```bash
mighty-colab --json exec-async -s trainer -f train.py --timeout 3600
# -> returns almost immediately: {"status": "started", "pid": ..., "log_path": "..."}

mighty-colab --json log -s trainer --tail --since-offset 0   # non-blocking peek, poll again with the previous next_offset
mighty-colab log -s trainer -f                                # or block and stream live, if you can afford to wait
```

(`--json` is a global flag — it must come *before* the subcommand, not after.)

Only one job runs at a time per session (a finished job never blocks a restart). Under
`--json`, the background worker writes its terminal result to a `<log_path>.json` sidecar
file that survives the session being stopped, so a caller that comes back later — even after
`stop` — can still read the final outcome. `log --tail` is the piece purpose-built for MCP:
`log -f`/`--follow` blocks a single tool call for the job's entire, potentially unbounded
duration and returns only once, at the very end — which a request/response tool-call model
can't represent. `--tail` is a bounded, non-blocking read instead, and `--since-offset`
avoids re-reading the whole log on every poll.

`--timeout` still fully applies to the underlying run — pass something generous (e.g.
`3600`) for anything that goes quiet for a while; see [Why this exists](#why-this-exists).

---

## Agent essentials

- **`--auth=adc` before the subcommand, always, for headless use**: the default,
  `oauth2`, is an interactive browser consent flow. `--auth` is a *global* flag and must
  precede the subcommand: `mighty-colab --auth=adc new -s x`.
- **`mighty-colab skill`** prints a full agent operating manual (source:
  [`skills/colab-operator/SKILL.md`](skills/colab-operator/SKILL.md)) — mental model,
  authentication setup, every gotcha above with the exact remediation, and a recovery
  section. **`mighty-colab readme`** prints this file. Point an agent at `skill` first; it's
  written for exactly this purpose and stays current with the CLI's own command registry.
- **`mighty-colab run`** is the ephemeral-job shape (`new` + `exec` + teardown in one
  command, works as a shebang line) and already has correct native-Python semantics:
  `sys.argv`, `__name__ == "__main__"`, and CPython `sys.exit()` conventions. `exec -f` gets
  the same script prelude, including a synthetic `__file__` — see
  [`docs/02_execution_and_interactive.md`](docs/02_execution_and_interactive.md) (§2) and
  [`docs/05_run_command.md`](docs/05_run_command.md) for what does and doesn't exist on the
  remote filesystem.

---

## Everything else this fork adds

Commands and behavior the official `colab` CLI doesn't have. Base commands not listed here
work exactly as upstream documents them — run `mighty-colab <command> --help` for options.

| Command | Description |
| --- | --- |
| `mighty-colab exec-async [-s NAME] [-f FILE] [--output-log PATH]` | Run `exec` detached; returns immediately, tracked via `status`/`log` |
| `mighty-colab log -s NAME [-f \| --tail [--since-offset N]]` | Follow (blocking) or peek (non-blocking) a background job's live output |
| `mighty-colab reinstall [-s NAME] [-r FILE \| PKG...]` | `install`, then restart the kernel on success — so an already-imported package's new version actually takes effect |
| `mighty-colab adopt ENDPOINT [-n NAME] [--keep-alive]` | Bring a runtime started outside the CLI (e.g. the Colab web UI, or a different agent process) under this process's local session tracking |
| `mighty-colab adopt --orphanage [--keep-alive]` | Adopt every orphaned server-side assignment at once |
| `mighty-colab mcp` | Start a stdio MCP server exposing these commands as tools for AI agents |
| `--json` (global) | Structured JSON envelopes on `exec`/`run`/`exec-async`/`log --tail`/`new`/`stop`/`sessions`/`status` — see above |
| `--debug` (global) | Opt into `DEBUG`-level logging, including third-party library chatter (default: `INFO`) |

Sixteen upstream defects were also fixed in this fork (exit codes, leaked kernels, unlocked
concurrent state, and more) — see [`CHANGELOG.md`](CHANGELOG.md) and
[`docs/AGENT_USABILITY_LEARNINGS.md`](docs/AGENT_USABILITY_LEARNINGS.md) for the full list
with attribution.

`adopt` exists because `mighty-colab`'s commands have different scopes: `sessions`/`status`
query the backend directly and see every assignment on the account, while `log`/`exec`
operate through *this process's own* local tracking
(`~/.config/colab-cli/sessions.json`). A session started by a different agent process is
invisible to the second set even though `status` already shows it — `adopt` closes that gap,
and it's also the recovery path for a stale runtime proxy token (they expire roughly
hourly) without tearing down and reallocating a VM.

---

## MCP Server Configuration

`mighty-colab` embeds an MCP server, scanned live from its own command registry, exposing
non-interactive commands as tools. Since the package is on PyPI, `uvx` can run it directly:

```json
{
  "mcpServers": {
    "mighty-colab": {
      "command": "uvx",
      "args": ["mighty-colab", "mcp"],
      "env": {
        "UV_WORKING_DIR": "/Optional/Path/To/Working_Dir"
      }
    }
  }
}
```

See [`docs/07_mcp_server.md`](docs/07_mcp_server.md) for which commands are exposed, how
global flags (`--auth`, `--config`) get added to `args`, and the current `--json`-over-MCP
gap noted above.

---

## Quick Start

```bash
mighty-colab --auth=adc new
echo "print('Hello from Google Colab!')" | mighty-colab --auth=adc exec
mighty-colab --auth=adc stop
```

> [!NOTE]
> When only one session is active, `-s`/`--session` can be omitted — the CLI resolves it
> automatically (and under `--json`, a failed auto-resolve still returns a proper envelope).

---

## Deep Dive Documentation

* [Session Management & Keep-Alive Architecture](docs/01_session_management.md)
* [Interactive & Non-Interactive Execution Design (`exec`, `exec-async`, `--json`)](docs/02_execution_and_interactive.md)
* [File Management & Jupyter Contents API](docs/03_file_management.md)
* [Authentication Providers & VM Automation](docs/04_automation_and_utility.md)
* [Ephemeral Job Runner Design (`run`)](docs/05_run_command.md)
* [SSH-over-WebSocket Runtime Access](docs/06_ssh_access.md)
* [MCP Server Design](docs/07_mcp_server.md)
* [What broke driving this from an AI agent, and what we changed](docs/AGENT_USABILITY_LEARNINGS.md)

To view interactive walkthroughs of eleven real-world automated scenarios, check out the
[Demo Walkthroughs](docs/demos.md).

---

## Contributing

This fork isn't currently accepting external pull requests — see
[`CONTRIBUTING.md`](./CONTRIBUTING.md). Ideas and pain points are welcome on
[Discussions](https://github.com/danbarua/mighty-colab/discussions).
