# Mighty Colab

> **Google Colab runtimes your coding agent can operate without babysitting.**

[![PyPI](https://img.shields.io/pypi/v/mighty-colab)](https://pypi.org/project/mighty-colab/)
[![Python](https://img.shields.io/pypi/pyversions/mighty-colab)](https://pypi.org/project/mighty-colab/)
[![License](https://img.shields.io/github/license/danbarua/mighty-colab)](LICENSE)

`mighty-colab` is a compatibility-first fork of Google's official
[`colab` CLI](https://github.com/googlecolab/google-colab-cli), hardened for
AI agents that provision runtimes, execute code, wait for long jobs, recover
state, and tear everything down without a human watching the terminal.

It installs as a separate binary, so `mighty-colab` and `colab` can coexist.
The upstream commands and flags remain familiar; this fork adds the machine-readable
contracts and failure handling that unattended workflows need.

**[Watch the demo](https://github.com/user-attachments/assets/656226a9-af13-4fdb-8eda-d7de747336a2)**
· **[Read the agent field notes](docs/AGENT_USABILITY_LEARNINGS.md)**
· **[Open the operator skill](skills/colab-operator/SKILL.md)**

> [!NOTE]
> Linux and macOS only. Python 3.12 or newer is required.

## Why Mighty Colab?

A human has peripheral vision. An agent has exit codes, stdout, and a tool-call
deadline.

Mighty Colab was shaped by a multi-month, agent-driven ML research pipeline:
real A100 sessions, hour-long jobs, artifacts written to GCS, and real billing.
That work exposed failure modes a person at a terminal can often notice and
correct, but an unattended agent cannot.

| What goes wrong for an agent | What Mighty Colab provides |
| --- | --- |
| A remote script raises, but the caller cannot reliably tell what happened | Schema-validated `--json` envelopes with separate CLI and remote-job outcomes |
| Training outlives a shell or MCP tool call | `exec-async` returns immediately; `log --tail` provides bounded, incremental polling |
| The agent restarts while the Colab VM keeps running | Server-side session discovery, `adopt`, orphan recovery, and durable result sidecars |
| Cleanup runs on every path, including partial failure | Idempotent `stop` for already-absent sessions; genuine teardown failures stay loud and retryable |
| Multiple agent processes touch the same local state | Locked history plus `--config` isolation for parallel runs |
| Human-friendly output becomes parser-hostile noise | JSON-only stdout, chatter on stderr, stable reason codes, and ANSI-stripped tracebacks |

This is not a speculative “AI-ready” wrapper. Heavy use led to **16 upstream
defect fixes**, plus fixes in this fork's own additions. Fourteen were landed as
a reproducing test followed by the fix. The full, candid account—including the
bugs introduced by this fork—is in
[`AGENT_USABILITY_LEARNINGS.md`](docs/AGENT_USABILITY_LEARNINGS.md).

## Install

```bash
uv tool install mighty-colab
```

Or, with `pip`:

```bash
pip install mighty-colab
```

## A 60-second agent workflow

Assuming Application Default Credentials are configured. Every response below
was captured live against a real CPU runtime — nothing fabricated. Swap in
`--gpu T4` / `--gpu A100` on `new` for accelerated workloads; the envelope
shape is identical either way.

```bash
SESSION=agent-job
echo 'print("hello from mighty-colab")' > job.py

# Allocate a named runtime.
mighty-colab --auth=adc --json new -s "$SESSION"
```

```json
{
  "schema_version": "1",
  "cli_version": "<installed version>",
  "command": "new",
  "status": "ok",
  "exit_code": 0,
  "session": "agent-job",
  "endpoint": "m-s-kkb-use1c1-2ol526ollah5r",
  "variant": "DEFAULT",
  "accelerator": "NONE"
}
```

```bash
# Capture the remote verdict as data, not terminal prose.
RESULT="$(mighty-colab --auth=adc --json exec \
  -s "$SESSION" -f job.py --timeout 3600)"
```

```json
{
  "schema_version": "1",
  "cli_version": "<installed version>",
  "command": "exec",
  "status": "ok",
  "exit_code": 0,
  "blocks": [
    {
      "code": "import sys\nsys.argv = ['job.py']\n__name__ = '__main__'\n__file__ = '<mighty-colab-exec:job.py>'\nprint(\"hello from mighty-colab\")\n",
      "outputs": [
        {
          "output_type": "stream",
          "name": "stdout",
          "text": "hello from mighty-colab\n"
        }
      ],
      "cell_index": null,
      "cell_id": null
    }
  ]
}
```

```bash
# Teardown happens before either verdict is propagated.
CLEANUP="$(mighty-colab --auth=adc --json stop -s "$SESSION")"
```

```json
{
  "schema_version": "1",
  "cli_version": "<installed version>",
  "command": "stop",
  "status": "ok",
  "exit_code": 0,
  "session": "agent-job"
}
```

```bash
# Fail if either the remote job or cleanup failed.
jq -en --argjson job "$RESULT" --argjson cleanup "$CLEANUP" \
  '$job.status == "ok" and $cleanup.status == "ok"'
# -> true
```

Global flags such as `--auth`, `--json`, and `--config` belong **before** the
subcommand. For headless use, pass `--auth=adc` explicitly: the default OAuth2
flow opens a browser and needs a human.

For ADC setup, accelerator availability, recovery procedures, and the commands
an agent must never invoke interactively, run:

```bash
mighty-colab skill
```

That manual is bundled with the installed CLI, so the agent's instructions stay
versioned with the tool it is operating.

## Machine-readable outcomes with `--json`

Every command commonly used in an agent loop—`new`, `exec`, `run`,
`exec-async`, `log --tail`, `status`, `sessions`, and `stop`—can return one
validated JSON envelope on stdout.

```json
{
  "schema_version": "1",
  "cli_version": "<installed version>",
  "command": "exec",
  "status": "job_raised",
  "exit_code": 1
}
```

The important distinction is intentional:

- The **CLI transaction** answers “did the client complete its work?”
- The envelope's `status` and `exit_code` answer “what happened in the remote job?”

That separation avoids treating valid IPython results such as `SystemExit(0)`
as client failures, while still making remote exceptions explicit. Envelopes
also carry version information, stable `reason` values, backend `http_status`
when available, structured outputs, and parse errors. Human-readable
`[colab] ...` chatter moves to stderr, leaving stdout safe for `jq` or another
programmatic caller.

Desired-state operations stay automation-friendly: stopping an already-absent
session returns `status: "ok"` with `reason: "already_stopped"`. Querying a
missing named session is an error. That difference makes unconditional cleanup
safe without hiding a real teardown failure.

See the live, end-to-end
[`new → exec → exec-async → log → stop` lifecycle](integration/repro_json_jq_lifecycle/test.sh)
for a complete `jq`-driven example.

## Long jobs that fit short tool calls

Blocking on training for an hour is a poor fit for an agent harness or an MCP
request/response cycle. Start the job in the background instead:

```bash
mighty-colab --auth=adc --json exec-async \
  -s trainer -f train.py --timeout 3600
```

The call returns in about a second with the PID and log path. Poll without
blocking:

```bash
mighty-colab --auth=adc --json log \
  -s trainer --tail --since-offset 0
```

Use the returned `next_offset` on the next poll to avoid rereading old output.
Only one background job runs per session, and a finished job never blocks the
next one. Its terminal JSON result is written beside the log and survives
session teardown, so an agent can recover the verdict later.

## Recover instead of reallocating

Colab assignments live on the backend, while executable session metadata lives
locally. A runtime created by another agent process—or by the Colab web UI—may
therefore be visible to `sessions` but unavailable to `exec`.

```bash
# Bring one server-side runtime under local management.
mighty-colab adopt <ENDPOINT> -n recovered

# Or recover every orphaned assignment.
mighty-colab adopt --orphanage
```

Re-adopting the same endpoint also refreshes an expired runtime proxy token,
without throwing away the VM and starting over. Add `--keep-alive` when the
original owner is no longer keeping the session alive.

## What this fork adds

The official [`colab` README](https://github.com/googlecolab/google-colab-cli)
is the command reference for the base CLI. Mighty Colab keeps that surface and
adds:

| Addition | Agent benefit |
| --- | --- |
| `--json` | Versioned, validated outcomes instead of scraping prose |
| `exec-async` | Start long work without holding a caller open |
| `log --tail --since-offset` | Bounded, incremental polling for agents and MCP clients |
| `adopt [ENDPOINT]` / `adopt --orphanage` | Recover runtimes created by another process or UI |
| `reinstall` | Install packages and restart the kernel so cached imports really update |
| `mcp` | Expose non-interactive CLI commands as MCP tools |
| `--debug` | Opt into verbose client and transport diagnostics |
| Chunked uploads | Move large files without the single-request failure mode |

The fork also tightens failure behavior around remote exceptions, package
installation, teardown, kernel and websocket cleanup, concurrent history,
stream separation, and large uploads. See the
[`CHANGELOG`](CHANGELOG.md) for the release-by-release detail.

## The four rules agents should know

1. **Always name sessions.** Random names make recovery and multi-session work
   ambiguous.
2. **Always pass `--auth=adc` for headless work.** It is a global flag and must
   precede the subcommand.
3. **Set `--timeout` deliberately.** It limits the gap between outputs, not total
   wall-clock runtime. Quiet compilation or training often needs `3600` or more.
4. **Always stop what you allocate.** A session is a billable VM. Use
   unconditional teardown, or prefer `run` for a one-shot job that should clean
   itself up.

One more execution-model detail matters: `exec -f script.py` sends the local
file's **text** into a live IPython kernel; it does not copy your repository onto
the VM. Mighty Colab supplies `sys.argv`, `__name__`, and an honest synthetic
`__file__`, but code that opens `__file__` or sibling paths still needs the
corresponding files uploaded or cloned remotely.

## CLI, embedded MCP, or in-notebook MCP?

| You want to… | Use… |
| --- | --- |
| Drive Colab from shell scripts, CI, Make, or an external coding agent | `mighty-colab` CLI |
| Expose the same non-interactive workflow to an MCP client | `mighty-colab mcp` |
| Add interactive agent assistance inside a Colab notebook | Google's [`colab-mcp`](https://github.com/googlecolab/colab-mcp) |
| Use the supported upstream command surface without this fork's additions | Google's [`colab` CLI](https://github.com/googlecolab/google-colab-cli) |

Minimal MCP client configuration:

```json
{
  "mcpServers": {
    "mighty-colab": {
      "command": "uvx",
      "args": ["mighty-colab", "--auth", "adc", "mcp"]
    }
  }
}
```

TTY-bound commands are intentionally excluded, and `log --follow` is replaced
by bounded `log --tail` polling. MCP results are currently plain text;
structured MCP output is a known follow-up to the CLI's new JSON contract. See
the [`MCP server design`](docs/07_mcp_server.md).

## Read more

- [What broke under heavy AI-agent usage—and what changed](docs/AGENT_USABILITY_LEARNINGS.md)
- [The bundled Colab operator skill](skills/colab-operator/SKILL.md)
- [Session and keep-alive architecture](docs/01_session_management.md)
- [Execution, background jobs, and JSON output](docs/02_execution_and_interactive.md)
- [Ephemeral jobs with `run`](docs/05_run_command.md)
- [SSH-over-WebSocket runtime access](docs/06_ssh_access.md)
- [Embedded MCP server](docs/07_mcp_server.md)
- [Demo walkthroughs](docs/demos.md)

## Contributing

External pull requests are not currently accepted, so contributions do not sit
unreviewed. Ideas, bug reports, and agent pain points are very welcome in
[Discussions](https://github.com/danbarua/mighty-colab/discussions).

Mighty Colab is licensed under the [Apache License 2.0](LICENSE).
