---
name: colab-operator
description: Operate Google Colab environments via the `mighty-colab` CLI. Use when asked to create or manage GPU/TPU sessions, run Python/shell on a remote Colab VM, sync files, automate environment setup (packages, auth, Drive), or export session history.
---

# Skill: Colab Session Operator

Operate Google Colab environments via the `mighty-colab` CLI: provision GPU/TPU sessions, run Python/shell on the VM, sync files, and capture work as notebooks. `mighty-colab` and `colab` are separate binaries that can coexist on the same machine — always invoke `mighty-colab`, not `colab`.

## Installation

If the user does not already have the `mighty-colab` tool installed, it can be acquired
by running `uv tool install mighty-colab` or `pip install mighty-colab`.

## When to activate
- Creating or managing TPU/GPU sessions.
- Running Python or shell on a remote Colab VM.
- Syncing files between local and remote.
- Automating environment setup (packages, auth, Drive).
- Exporting session history as a Jupyter notebook.

## Mental model (read this first)
- **A session == a live Jupyter kernel on a rented VM.** `mighty-colab new` allocates a billable VM; `mighty-colab stop` releases it. Nothing reclaims it automatically except a 24h keep-alive cap, so an unstopped session burns compute units indefinitely.
- **Kernel state persists across `exec`/`repl` calls in the same session.** Imports, variables, and defined functions survive between separate `mighty-colab exec` commands — build up state incrementally, don't re-import everything each call. Only `mighty-colab stop` / `restart-kernel` reset it.
- **Default working directory is `/content`.** Every `exec`/`repl`/`run` `cd`s there first; prefer absolute paths (`/content/...`) for file work. For `mighty-colab ls/rm/upload/download`, the default `ls` path is `content` (VM root).
- **`mighty-colab` is fire-and-forget.** Each command authenticates, does one thing, and exits. A detached background daemon (spawned by `mighty-colab new`) handles keep-alive; you don't manage it.

## Authentication (the #1 thing that blocks agents)
- The global flag is `--auth={adc,oauth2}` and the **default is `oauth2`** (interactive browser consent flow) — **always pass `--auth=adc` explicitly for agent/headless use**. It must come *before* the subcommand: `mighty-colab --auth=adc new -s x`.
- **ADC setup**: re-mint ADC with all required scopes:
  ```bash
  gcloud auth application-default login \
    --scopes=openid,\
  https://www.googleapis.com/auth/cloud-platform,\
  https://www.googleapis.com/auth/userinfo.email,\
  https://www.googleapis.com/auth/colaboratory
  ```
- **oauth2 setup**: `mighty-colab --auth=oauth2 <anything>` triggers a browser consent flow on first use (token cached at `~/.config/colab-cli/token.json`). Requires a client config at `~/.colab-cli-oauth-config.json` (or `-c PATH`). Needs a human; prefer ADC for agents.
- **Verify auth in one shot**: `mighty-colab sessions` (read-only) or `mighty-colab whoami` (hidden debug command — prints the active email, scopes, audience, expiry). A 403 against `colab.pa.googleapis.com` is almost always a missing scope; `whoami` shows it instantly.
- **`mighty-colab new` pre-flights the keep-alive check.** A missing `colaboratory` scope unassigns the fresh VM (no leaked billing) and prints the remediation — follow that message rather than retrying blindly.
- **`mighty-colab auth` is not CLI authentication.** It injects *VM-side* GCP credentials into the kernel (for in-notebook BigQuery/GCS calls); it does not fix a CLI 401/403. That's a scope/identity problem, fixed via the `gcloud` command above.

## Workflow

### Provision
- `mighty-colab new -s <name>` (CPU). Add `--gpu A100` or `--tpu v6e1` for accelerators. **Always pass `-s <name>`** — an omitted name is auto-generated as a random 6-hex string, which makes later commands ambiguous.
- Supported `--gpu`: `T4`, `L4`, `G4`, `H100`, `A100`. Supported `--tpu`: `v5e1`, `v6e1`.
- **Gotcha**: an unrecognized `--gpu` value silently falls back to **A100** (which then usually fails the next step). A `400` on `mighty-colab new` with an accelerator means no quota/entitlement for it — fall back to `--gpu T4` or omit the flag for CPU.
- Accelerator availability is tier-gated; most accounts can only get CPU.

### Adopt orphaned sessions
- If `mighty-colab sessions` shows an assignment marked `[?]` (no local record — e.g. started from the Colab web UI), claim it with `mighty-colab adopt <ENDPOINT>` (same endpoint string `sessions`/`status` print).
- `mighty-colab adopt --orphanage` claims every `[?]` assignment in one pass.
- `-n/--name <name>` sets a friendly local name (defaults to the endpoint string). Reusing a name that already tracks a *different* endpoint errors instead of silently repointing it.
- **No keep-alive daemon by default** on adopt — pass `--keep-alive` to start one (e.g. for a runtime whose creating process/tab is gone).
- Re-running `mighty-colab adopt <ENDPOINT>` for a session already tracked under the *same* name refreshes its runtime proxy token (expires roughly hourly) — safe to re-run, and the fix for a stale-token 401.

### Execute
- **Preferred**: `mighty-colab exec -s <name> -f <script.py>` runs a local script on the remote VM (read locally, sent to the kernel — no manual upload needed).
- **`exec -f` transmits the file's *text* into the kernel, not as a real file** — no local filesystem exists on the VM. `sys.argv`, `__name__ == "__main__"`, and `__file__` (a synthetic `<mighty-colab-exec:basename>` sentinel, not a real path) are set to match `python script.py` semantics. Code doing `open(__file__)` fails on that sentinel — expected, not a bug. `mighty-colab run` (below) gets the same treatment.
- **`--timeout` (default 30s, on `exec`/`run`/`exec-async`) bounds the gap between *outputs*, not the total run.** A script computing silently for longer than that raises `TimeoutError` even if healthy. Pass a generous `--timeout` (e.g. `3600`) for anything that goes quiet for a while.
- **Background execution**: `mighty-colab exec-async -s <name> -f script.py` runs the same as `exec` but detached, returning almost instantly. Follow output with `mighty-colab log -s <name> -f` (below). Only one job at a time per session — a second `exec-async` while one's running is refused (a *finished* job never blocks a restart). Requires a real file or piped code; a live TTY can't be forwarded.
- `exec-async`'s `--timeout` needs the same generous override as above — it's exactly the command used for long, quiet jobs.
- **`--output-log <path>`** (on `exec-async` only) redirects the raw log to any writable location instead of the default `~/.config/colab-cli/history/<session>.exec.log`. Creates the parent directory if needed. `mighty-colab status` shows the active path as `Log: <path>`.
- **Piped code**: `echo "print(1)" | mighty-colab exec -s <name>` or `cat script.py | mighty-colab exec -s <name>`.
- **Notebooks**: `mighty-colab exec -s <name> -f nb.ipynb` runs each code cell and writes results to `<basename>_output.ipynb`. A `# @title Foo` first line labels the cell in progress output.
- **Plots/images**: PNG/JPEG outputs are intercepted. Use `--output-image <path>` on `exec`/`repl` to save to a known location.
- **Shell**: `echo "cmd" | mighty-colab console -s <name>` for batch shell. Output contains terminal-control bytes — filter with `grep -a` for a specific line. `exec` is faster when you don't need a real shell.
- **Never run `mighty-colab repl`, `console`, `auth`, or `drivemount` interactively from an agent** — they expect a TTY and will hang. `repl`/`console` accept piped stdin and exit on EOF; `auth`/`drivemount` require a human.

### Ephemeral one-shot jobs (`mighty-colab run`)
- `mighty-colab run [--gpu T4] [--tpu v6e1] [--keep] [-s NAME] script.py [args...]` = `new` + `exec` + `stop` in one command. Runs the script with `sys.argv`/`__name__ == "__main__"` set like native `python script.py args`, then tears the VM down (unless `--keep`).
- **Exit codes propagate**: `sys.exit()`/`sys.exit(0)` → 0, `sys.exit(N)` → N, `sys.exit("msg")` → 1.
- **Stream separation**: `run`'s own `[colab] ...` chatter goes to **stderr**, the script's output to **stdout** — `run job.py > out.txt` captures only the script's stdout.
- Works as a shebang: `#!/usr/bin/env -S mighty-colab run --gpu T4`.
- A nonexistent script path exits non-zero **before** allocating a VM.

### Automate
- `mighty-colab auth -s <name>` — VM-side GCP creds (interactive; not agent-runnable).
- `mighty-colab drivemount -s <name> [PATH]` — mounts Drive at `/content/drive` (interactive; not agent-runnable).
- `mighty-colab install -s <name> pkg1 pkg2` — installs via `uv pip install --system` if `uv` is on the VM, otherwise `pip`. Also `-r requirements.txt`.
- **`mighty-colab reinstall`** — `install` then restarts the kernel (only if install succeeds). Use this instead of `install` when the package may already be imported (e.g. upgrading `jax`/`torch`): Python caches imports in `sys.modules`, so a plain `install` has no visible effect until the kernel restarts.

### Inspect & report
- `mighty-colab help` (or `help <cmd>`) lists/explains commands, alphabetically.
- `mighty-colab sessions` lists server-side assignments, auto-prunes stale local entries. Orphans show as `[?]` — claim with `adopt <ENDPOINT>` or `adopt --orphanage`.
- `mighty-colab status [-s <name>]` shows hardware, IDLE/BUSY, last execution, and background job log path if tracked.
- `mighty-colab log -s <name> [-n 20] [-t TYPE]` shows recent structured events; useful when a task fails.
- `mighty-colab log -s <name> -f` tails a running `exec-async` job's raw stdout/stderr live, until it finishes. Errors if no job is tracked for that session.
- `mighty-colab log -s <name> --tail [-n N]` prints that same raw output once and exits immediately — no waiting. Use this instead of `-f` when calling through something that can't handle an unbounded blocking call (e.g. an MCP client).
- `mighty-colab log -s <name> -o summary.ipynb` exports the session as a notebook (also `.md`, `.txt`, `.jsonl` by suffix).
- `mighty-colab url -s <name>` prints a browser URL that attaches the Colab web UI to your existing CLI session (add `--open` to launch it).
- `mighty-colab skill` / `readme` print this skill and the README.

## MCP Server
`mighty-colab mcp` starts a stdio MCP server exposing this CLI's commands as tools for a *different* MCP-aware client (e.g. Claude Desktop). Not something to invoke from within this skill — if you already have shell access to `mighty-colab`, running `mighty-colab mcp` yourself blocks on stdio and hangs your session.

## Safety
- **Always `mighty-colab stop -s <name>` when done** — idle VMs burn compute units. `mighty-colab run` (without `--keep`) self-cleans even if the script errors.
- Local state lives in `~/.config/colab-cli/sessions.json` (settings in `settings.json`, history in `history/*.jsonl`). Don't edit by hand.
- **Isolate parallel/agent runs** with the global `--config <path>` flag (e.g. `mighty-colab --config /tmp/agent.json new -s job`). The keep-alive daemon inherits `--auth`/`--config` automatically.

## Recovery
- "Session not found" / 404 / 401 on exec: the backend pruned the VM. `exec`/`repl` clean up local state automatically — run `sessions` and re-create with `new`.
- Execution timeout or wedged kernel: `mighty-colab restart-kernel -s <name>` (keeps the VM, resets the kernel), or `stop` then `new`.
- `exec --timeout N` can peg a local CPU core at ~100% and hang after the deadline passes — the remote session is unaffected. `kill -9` the local process and reattach with `mighty-colab status -s <name>` / `exec`.
- Keep-alive daemon died (`log` shows `keep_alive_stopped reason=consecutive_4xx_errors`): almost always the missing `colaboratory` scope — re-auth per the Authentication section.
