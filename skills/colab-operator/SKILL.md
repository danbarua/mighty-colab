---
name: colab-operator
description: Operate Google Colab environments via the `mighty-colab` CLI. Use when asked to create or manage GPU/TPU sessions, run Python/shell on a remote Colab VM, sync files, automate environment setup (packages, auth, Drive), or export session history.
---

# Skill: Colab Session Operator

Operate Google Colab environments via the `mighty-colab` CLI: provision GPU/TPU sessions, run Python/shell on the VM, sync files, and capture work as notebooks. `mighty-colab` is a fork of Google's upstream `colab` CLI, published as a distinct binary/package specifically so the two can coexist in the same shell — always invoke `mighty-colab`, not `colab`.

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
- **Kernel state PERSISTS across `mighty-colab exec` / `mighty-colab repl` calls in the same session.** Each invocation reattaches to the *same* kernel (the kernel ID is cached in local state) and only closes the websocket on exit — it does **not** shut the kernel down. So imports, variables, and defined functions survive between separate `mighty-colab exec` commands. Build up state incrementally; don't re-import everything each call. (`mighty-colab stop` and `mighty-colab restart-kernel` are what actually reset it.)
- **Default working directory is `/content`.** Every `exec`/`repl`/`run` `cd`s there first; prefer absolute paths (`/content/...`) for file work. For `mighty-colab ls/rm/upload/download`, absolute `/content/...` paths work and the default `ls` path is `content` (VM root).
- **`mighty-colab` is fire-and-forget.** Each command authenticates, does one thing, and exits. A detached background daemon (spawned by `mighty-colab new`) handles keep-alive; you don't manage it.

## Authentication (the #1 thing that blocks agents)
- The global flag is `--auth={adc,oauth2}` and the **default is `oauth2`** (interactive browser consent flow) — **always pass `--auth=adc` explicitly for agent/headless use**, since the default is not agent-safe on its own. It must come *before* the subcommand: `mighty-colab --auth=adc new -s x`.
- **ADC setup** (most reliable for headless/agent use). The Colab backends need a specific scope set, so re-mint ADC with all four scopes:
  ```bash
  gcloud auth application-default login \
    --scopes=openid,\
  https://www.googleapis.com/auth/cloud-platform,\
  https://www.googleapis.com/auth/userinfo.email,\
  https://www.googleapis.com/auth/colaboratory
  ```
  Why all four: `userinfo.email` (session backend `colab.research.google.com`, else 401), `colaboratory` (RuntimeService `colab.pa.googleapis.com` keep-alive, else 403), `openid`+`cloud-platform` (mandated by gcloud itself; it rejects scope lists missing `cloud-platform`).
- **oauth2 setup**: `mighty-colab --auth=oauth2 <anything>` triggers a browser consent flow on first use (token cached at `~/.config/colab-cli/token.json`). Requires a client config at `~/.colab-cli-oauth-config.json` (or `-c PATH`). The browser step means it usually needs a human; prefer ADC for agents.
- **Verify auth in one shot**: `mighty-colab sessions` (read-only, lists server assignments) or `mighty-colab whoami` (hidden debug command: prints the active email, scopes, audience, and expiry). When any call 403s against `colab.pa.googleapis.com`, the cause is almost always a missing scope — `mighty-colab whoami` shows it instantly.
- **`mighty-colab new` pre-flights the keep-alive RPC** right after allocating. If your token lacks the `colaboratory` scope it unassigns the fresh VM (so you don't leak a billable assignment) and prints the exact remediation. Follow that message rather than retrying blindly.
- **Do NOT confuse `mighty-colab auth` with CLI authentication.** `mighty-colab auth` injects *VM-side* GCP credentials into the running kernel (so notebook code can call BigQuery/GCS); it is orthogonal to how the CLI itself authenticates. Never suggest "run `mighty-colab auth`" to fix a CLI 401/403 — that's a scope/identity problem fixed via the `gcloud` command above.

## Workflow

### Provision
- `mighty-colab new -s <name>` (CPU). Add `--gpu A100` or `--tpu v6e1` for accelerators. **Always pass `-s <name>`** — an omitted name is auto-generated as a random 6-hex string, which makes later commands ambiguous.
- Supported `--gpu`: `T4`, `L4`, `G4`, `H100`, `A100`. Supported `--tpu`: `v5e1`, `v6e1`.
- **Gotcha**: an unrecognized `--gpu` value silently falls back to **A100** (which then usually fails the next step). A `400` on `mighty-colab new` with an accelerator means no quota/entitlement for it on this account — fall back to `--gpu T4` or omit the flag for CPU.
- Accelerator availability is tier-gated; most accounts can only get CPU. Don't assume a GPU/TPU will allocate.

### Adopt orphaned sessions
- If `mighty-colab sessions` shows an assignment marked `[?]` (billed server-side but with no local record — e.g. started from the Colab web UI, or a process that exited before persisting state), claim it with `mighty-colab adopt <ENDPOINT>` (same endpoint string `mighty-colab sessions`/`mighty-colab status` print).
- `mighty-colab adopt --orphanage` claims every `[?]` assignment in one pass instead of one at a time.
- `-n/--name <name>` sets a friendly local name (defaults to the endpoint string itself). Reusing a name that already tracks a *different* endpoint errors instead of silently repointing it — pick another name or `mighty-colab stop -s <name>` first.
- **No keep-alive daemon by default** — adopt assumes whoever created the runtime (e.g. an open Colab browser tab) is already keeping it alive. Pass `--keep-alive` to also start the CLI's own daemon, e.g. for a runtime whose creating process/tab is gone.
- Re-running `mighty-colab adopt <ENDPOINT>` for a session already tracked under the *same* name refreshes its runtime proxy token (expires roughly hourly) — safe to re-run, and the fix for a stale-token 401 instead of tearing down and reallocating. Re-adopting under a *different* name, or an endpoint already owned by a session created via `mighty-colab new`, is a no-op.

### Execute
- **Preferred**: `mighty-colab exec -s <name> -f <script.py>` runs a local script on the remote VM (read locally, sent to the kernel — no manual upload needed).
- **`exec -f` transmits the file's *text* into the existing kernel -- it does not run the file as a script.** No import machinery runs, so `__file__` is never defined, `sys.argv` is unset, and `__name__ != "__main__"`. Nothing from the local filesystem exists on the VM either, until something explicitly puts it there (upload, install, or code that clones/downloads it). Module-scope code that assumes any of this will break — this crashed a real billing run when a driver used `os.path.dirname(__file__)` at import time. Needs `argv`/`__main__` semantics? Use `mighty-colab run` instead (below), which sets both.
- **`--timeout` (default 30s, on `exec`/`run`/`exec-async`) bounds the gap between *outputs*, not the total run.** A script computing silently — a network download, a training epoch, model compilation — for longer than the timeout raises `TimeoutError` even though it's perfectly healthy. Pass a generous `--timeout` (e.g. `--timeout 3600`) for anything that goes quiet for a while; don't rely on the default just because a hand-run session happened to complete.
- **Background execution — built specifically for agents**: `mighty-colab exec-async -s <name> -f script.py` runs the same as `exec` but detached, returning almost instantly regardless of how long the script takes, instead of blocking the caller for the whole run. Submit, then check back — don't hold a tool call open for an hour-long job. Follow output with `mighty-colab log -s <name> -f` (below). Only one job at a time per session: a second `exec-async` while one's still running is refused (checked by pid liveness, so a *finished* job never blocks a restart) — target a different session or wait. Piped stdin works the same as `exec`, but requires a real file or piped code; a live TTY can't be forwarded to a detached process.
- **`exec-async` inherits the `--timeout` gotcha above, and it bites harder here**: this is exactly the command used for long, quiet background jobs (GPU training with sparse logging), so the default 30s is almost always wrong for it — always pass a generous `--timeout` explicitly.
- **`--output-log <path>`** (on `exec-async` only) redirects the raw log to any writable location instead of the default `~/.config/colab-cli/history/<session>.exec.log` — for an agent sandboxed without write access there. Creates the parent directory if it doesn't exist yet. `mighty-colab status` shows the active path as `Log: <path>`, so a caller polling a pid can find it without re-deriving it.
- **Piped code**: `echo "print(1)" | mighty-colab exec -s <name>` or `cat script.py | mighty-colab exec -s <name>`.
- **Notebooks**: `mighty-colab exec -s <name> -f nb.ipynb` runs each code cell and writes results to `<basename>_output.ipynb` next to the input. A `# @title Foo` first line labels the cell in progress output.
- **Plots/images**: PNG/JPEG outputs are intercepted. Use `--output-image <path>` on `exec`/`repl` to save to a known location (otherwise a temp path is printed). Inline terminal-image escapes are auto-suppressed when stdout isn't a TTY, so piped/captured output stays clean.
- **Shell**: `echo "cmd" | mighty-colab console -s <name>` for batch shell. Console wraps bash in tmux, so even piped output contains terminal-control bytes — filter with `grep -a` for a specific line. `exec` is faster when you don't need a real shell.
- **Never run `mighty-colab repl`, `mighty-colab console`, `mighty-colab auth`, or `mighty-colab drivemount` interactively from an agent** — they expect a TTY and will hang. `repl`/`console` accept piped stdin and exit on EOF; `auth`/`drivemount` genuinely require a human at the terminal.

### Ephemeral one-shot jobs (`mighty-colab run`)
- `mighty-colab run [--gpu T4] [--tpu v6e1] [--keep] [-s NAME] script.py [args...]` = `new` + `exec` + `stop` in one command. It provisions a fresh VM, runs the script with `sys.argv` and `__name__ == "__main__"` set like native `python script.py args`, then tears the VM down (unless `--keep`).
- **Exit codes propagate**: an uncaught exception or `sys.exit(N)` in the script makes `mighty-colab run` exit non-zero (CPython semantics: `sys.exit()`/`sys.exit(0)` → 0, `sys.exit(N)` → N, `sys.exit("msg")` → 1).
- **Stream separation**: `mighty-colab run` writes its own `[colab] ...` chatter to **stderr** and the script's output to **stdout** — so `mighty-colab run job.py > out.txt` captures only the script's stdout. (`mighty-colab exec` streams the script's stdout/stderr live to your stdout/stderr.)
- Works as a shebang: `#!/usr/bin/env -S mighty-colab run --gpu T4` makes a `chmod +x`'d `.py` a self-contained "rent a GPU, run, clean up" script. After editing CLI behavior, reinstall before testing shebangs — they resolve `mighty-colab` via `$PATH`, not the editable install.
- A nonexistent script path exits non-zero **before** allocating a VM (no wasted compute).

### Automate
- `mighty-colab auth -s <name>` — VM-side GCP creds, needed before in-VM GCS/BigQuery calls (interactive; not agent-runnable).
- `mighty-colab drivemount -s <name> [PATH]` — mounts Drive at `/content/drive` by default (interactive; not agent-runnable).
- `mighty-colab install -s <name> pkg1 pkg2` — installs via `uv pip install --system` if `uv` is on the VM, otherwise `pip`. Also `mighty-colab install -s <name> -r requirements.txt`.
- **`mighty-colab reinstall`** — same as `install`, but restarts the kernel afterward (only if the install succeeds). Prefer this over `install` whenever the package may already be imported in the session (e.g. upgrading an already-imported `jax`/`torch`): Python caches imports in `sys.modules`, so a plain `install` alone has no visible effect on a package that's already loaded until the kernel restarts. `install` never restarts on its own — it stays a faithful match to upstream `colab install`.

### Inspect & report
- `mighty-colab help` (or `mighty-colab help <cmd>`) lists/explains commands; the listing is alphabetical.
- `mighty-colab sessions` lists server-side assignments and auto-prunes stale local entries. Orphans with no local record show as `[?]` — claim one with `mighty-colab adopt <ENDPOINT>`, or all of them with `mighty-colab adopt --orphanage`.
- `mighty-colab status [-s <name>]` shows hardware, IDLE/BUSY, and last execution.
- `mighty-colab log -s <name> [-n 20] [-t TYPE]` shows recent structured events; invaluable when a task fails (keep-alive errors carry the raw `response_body`).
- `mighty-colab log -s <name> -f` tails a running `exec-async` job's raw stdout/stderr live, until it finishes — a different, real-time view from the structured event history the same command shows without `-f`. Errors if no `exec-async` job is tracked for that session.
- `mighty-colab log -s <name> -o summary.ipynb` exports the session as a notebook (also `.md`, `.txt`, `.jsonl` by suffix).
- `mighty-colab url -s <name>` prints a browser URL that attaches the Colab web UI to your existing CLI session instead of allocating a new VM (add `--open` to launch it).
- `mighty-colab skill` / `mighty-colab readme` print this skill and the README (handy for self-discovery).

## MCP Server
`mighty-colab mcp` starts a stdio MCP (Model Context Protocol) server exposing this CLI's commands as tools for a *different* MCP-aware client (e.g. Claude Desktop) to call. It's an alternate entry point into the same commands documented here — not something to invoke from within this skill. If you already have shell access to `mighty-colab`, don't run `mighty-colab mcp` yourself: it blocks on stdio waiting for a client and will hang your session.

## Safety
- **Always `mighty-colab stop -s <name>` when done** — idle VMs burn compute units. `mighty-colab run` (without `--keep`) self-cleans even if the script errors.
- Local state lives in `~/.config/colab-cli/sessions.json` (settings in `settings.json`, history in `history/*.jsonl`). Don't edit by hand.
- **Isolate parallel/agent runs** with the global `--config <path>` flag to point session state at a scratch file (e.g. `mighty-colab --config /tmp/agent.json new -s job`). The keep-alive daemon inherits `--auth` and `--config` automatically.

## Recovery
- "Session not found" / 404 / 401 on exec: the backend pruned the VM. `mighty-colab exec`/`repl` detect this and clean up local state automatically — run `mighty-colab sessions` and re-create with `mighty-colab new`.
- Execution timeout or wedged kernel: `mighty-colab restart-kernel -s <name>` (keeps the VM, resets the kernel), or `mighty-colab stop` then `mighty-colab new`.
- `mighty-colab exec --timeout N` pegs a local CPU core at ~100% and never exits, even though the remote kernel/VM is fine: known bug in the vendored `jupyter_kernel_client` fork (`googlecolab/jupyter-kernel-client`) — once the deadline passes, `execute_interactive()`'s wait loop clamps to a 0s timeout and spins forever with no deadline-exceeded exit (verified live in the vendored source, not just a report). No fix planned (third-party code, and the fork has issues disabled). Just `kill -9` the local process — the remote session is untouched and can be reattached immediately with `mighty-colab status -s <name>` / `mighty-colab exec`.
- Keep-alive daemon died (`mighty-colab log` shows `keep_alive_stopped reason=consecutive_4xx_errors`): almost always the missing `colaboratory` scope — re-auth per the Authentication section.
