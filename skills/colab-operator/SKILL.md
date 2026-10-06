---
name: colab-operator
description: Operate Google Colab VMs with the `mighty-colab` CLI. Use when asked to create or stop GPU/TPU sessions, run Python or shell on a Colab VM, run a long unattended training or batch job (`job`), move files, set up the environment (packages, auth, Drive), check compute units, or export session history.
---

# Skill: Colab Session Operator

Operate Google Colab VMs with the `mighty-colab` CLI: provision GPU and TPU
sessions, run Python and shell on the VM, run long jobs unattended, move
files, and capture work as notebooks. `mighty-colab` and upstream's `colab`
are separate commands that can both be installed; always invoke
`mighty-colab`.

## Installation

If `mighty-colab` is not installed, install it with
`uv tool install mighty-colab` or `pip install mighty-colab`.

## When to activate

- Creating, inspecting or stopping TPU, GPU or CPU sessions.
- Running Python or shell on a Colab VM.
- Running a training or batch job that takes longer than one tool call, or
  that must keep running when the local process ends.
- Moving files between the local machine and the VM.
- Setting up the environment (packages, VM-side auth, Drive).
- Checking the account's compute units.
- Exporting session history as a Jupyter notebook.

## Mental model (read this first)

- **A session is a live Jupyter kernel on a rented, billable VM.**
  `mighty-colab new` allocates the VM, and `mighty-colab stop` releases it.
  A session bills until it is stopped or Colab reclaims it. The keep-alive
  daemon stops after 24 hours, and Colab documents no reclaim deadline, so
  always stop a session when the work is done.
- **Kernel state persists across `exec` and `repl` calls in one session.**
  Imports, variables and functions survive between separate
  `mighty-colab exec` commands, so build state up step by step instead of
  re-importing on every call. Only `stop` and `restart-kernel` reset it.
- **The working directory is `/content`.** Every `exec`, `repl` and `run`
  changes to it first; prefer absolute paths (`/content/...`). For
  `ls`, `rm`, `upload` and `download`, the default `ls` path is `content`.
- **Each command does one thing and exits.** `mighty-colab new` also starts a
  detached keep-alive daemon, which you do not manage. `--no-keepalive`
  starts none; upstream states that Colab now keeps runtimes alive based on
  kernel activity and active connections.
- **Four ways to run code:**

  | command | session | when the local process ends | use for |
  |---|---|---|---|
  | `exec` | an existing one | the run stops | short steps that build kernel state |
  | `exec-async` | an existing one | the run continues; follow it with `log` | long steps in a session you keep |
  | `run` | creates one, stops it after | the run stops | one script, start to finish, in one call |
  | `job` | creates one, releases it after | the run continues on the VM | long or unattended work with inputs, outputs and a recorded verdict |

  Use `job` for real work: it does the babysitting. Use `exec-async` for long
  steps while you iterate in a session you keep, and `exec` for short steps
  that build kernel state. `run` behaves like upstream's command.

## Machine-readable output (`--json`)

- Pass `--json` before the subcommand, with the other global flags:
  `mighty-colab --auth=adc --json exec -s x -f step.py`.
- With `--json`, stdout carries exactly one JSON object, the envelope, and
  the human-readable text goes to stderr.
- `--json` works on `exec`, `run`, `exec-async`, `log`, `new`, `stop`,
  `sessions`, `status`, `usage`, and every `job` and `jobs` subcommand. On
  any other command it prints a warning and the command runs normally.
- Every envelope has `status`, `exit_code` and `command`. A failure adds
  `reason`, a stable code such as `session_not_found`, `session_lost`,
  `auth_scope_missing`, `accelerator_rejected`, `worker_terminated` or
  `unhandled_error`. It adds `http_status` when an HTTP response caused the
  failure, and `message` or `hint` when the command reports more detail.
- Decide from `status`, `exit_code` and `reason`, not from the text.
- `exec --json` exits 0 even when the code raised. The envelope then has
  `status: "job_raised"` and the cell's `exit_code`. `run --json` exits with
  the script's exit code.

## Authentication (the most common blocker)

- The global flag is `--auth={adc,oauth2}`, and the **default is `oauth2`**,
  an interactive browser flow. **Pass `--auth=adc` for agent and headless
  use.** It goes before the subcommand: `mighty-colab --auth=adc new -s x`.
- **ADC setup**: log in with all four scopes. `gcloud` ignores the scopes
  the CLI asks for, so they must be on the login command:
  ```bash
  gcloud auth application-default login \
    --scopes=openid,\
  https://www.googleapis.com/auth/cloud-platform,\
  https://www.googleapis.com/auth/userinfo.email,\
  https://www.googleapis.com/auth/colaboratory
  ```
- **oauth2 setup**: `mighty-colab --auth=oauth2 <any command>` starts a
  browser consent flow on first use and caches the token at
  `~/.config/colab-cli/token.json`. It needs a human; prefer ADC for agents.
- **Check auth in one call**: `mighty-colab sessions` is read-only.
  `mighty-colab whoami`, a hidden debugging command, prints the active
  email, scopes, audience and expiry. A 401 from `sessions` or `new` usually
  means the credentials lack `userinfo.email`; log in again with the command
  above.
- **`new` and `run` ping keep-alive once before they continue**, unless
  `--no-keepalive` is given. A
  missing-scope answer (403 `SCOPE_NOT_PERMITTED`) releases the new VM, so
  nothing is left billing, and reports `auth_scope_missing` with the fix.
  Follow that message instead of retrying.
- **`mighty-colab auth` is not CLI authentication.** It injects GCP
  credentials into the VM's kernel, for BigQuery or GCS calls from notebook
  code. It does not fix a CLI 401 or 403.

## Workflow

### Provision

- `mighty-colab new -s <name>` creates a CPU session. Add `--gpu A100` or
  `--tpu v6e1` for an accelerator. **Always pass `-s <name>`**: without it,
  the name is a random 6-character hex string, which makes later commands
  ambiguous.
- Supported `--gpu`: `T4`, `L4`, `G4`, `H100`, `A100`. Supported `--tpu`:
  `v5e1`, `v6e1`.
- **Gotcha**: an unrecognized `--gpu` value falls back to **A100** without a
  warning. A 400 from `new` with an accelerator (`accelerator_rejected`)
  means the account has no quota or entitlement for it: use `--gpu T4`, or
  omit the flag for CPU. `mighty-colab usage` lists the GPUs and TPUs the
  account may request.
- `--no-keepalive` starts no keep-alive daemon for the session.

### Adopt orphaned sessions

- When `mighty-colab sessions` shows an assignment marked `[?]`, no local
  record tracks it (for example, it was started from the Colab web UI).
  Claim it with `mighty-colab adopt <ENDPOINT>`, using the endpoint string
  that `sessions` prints.
- `mighty-colab adopt --orphanage` claims every `[?]` assignment at once.
- `-n/--name <name>` sets the local name; the default is the endpoint
  string. A name that already tracks a *different* endpoint is refused.
- `adopt` starts no keep-alive daemon unless you pass `--keep-alive`, for
  example for a runtime whose browser tab is gone.
- Running `mighty-colab adopt <ENDPOINT>` again for a session tracked under
  the same name refreshes its runtime proxy token, which expires about
  hourly. It is safe to repeat, and it fixes a stale-token 401.
- To release an orphan: `mighty-colab adopt <ENDPOINT>`, then
  `mighty-colab stop -s <ENDPOINT>`.

### Execute

- `mighty-colab exec -s <name> -f <script.py>` sends a local script to the
  kernel and runs it; no upload is needed. It waits for the script; for
  anything long, use `exec-async` below, or `job`.
- **`exec -f` sends the file's text, not a file.** `sys.argv`,
  `__name__ == "__main__"` and `__file__` are set as for
  `python script.py`, but `__file__` is a sentinel
  (`<mighty-colab-exec:basename>`), not a real path, so `open(__file__)`
  fails. `mighty-colab run` works the same way.
- **`--timeout` (default 30 s, on `exec`, `run` and `exec-async`) limits the
  time between outputs, not the whole run.** A script that computes silently
  for longer raises `TimeoutError` even when it is healthy. Pass a generous
  value, such as `--timeout 3600`, for anything that goes quiet.
- **Background execution**: `mighty-colab exec-async -s <name> -f script.py`
  runs the script detached and returns almost at once. Follow the output with
  `mighty-colab log -s <name> -f`, or poll it with `log --tail`. A session
  runs one `exec-async` job at a time; a second one is refused while the
  first runs, and a finished job never blocks a new one. It needs a file or
  piped code, not a TTY, and the same generous `--timeout`.
- **`--output-log <path>`** (on `exec-async`) writes the raw log to that path
  instead of `~/.config/colab-cli/history/<session>.exec.log`, creating the
  parent directory. `mighty-colab status` shows the path as `Log: <path>`.
- **Piped code**: `echo "print(1)" | mighty-colab exec -s <name>` or
  `cat script.py | mighty-colab exec -s <name>`.
- **Notebooks**: `mighty-colab exec -s <name> -f nb.ipynb` runs each code
  cell and writes the results to `<basename>_output.ipynb`. A
  `# @title Foo` first line labels the cell in the progress output.
- **Plots and images**: PNG and JPEG outputs are saved; pass
  `--output-image <path>` on `exec` or `repl` to choose where.
- **Shell**: `echo "cmd" | mighty-colab console -s <name>` runs shell
  commands. The output contains terminal-control bytes; use `grep -a` to find
  a line. `exec` is faster when you do not need a real shell.
- **Never run `repl`, `console`, `auth` or `drivemount` interactively from an
  agent**: they wait for a TTY and hang. `repl` and `console` accept piped
  stdin and exit on EOF; `auth` and `drivemount` need a human.

### One script, start to finish (`mighty-colab run`)

- `mighty-colab run [--gpu T4] [--tpu v6e1] [--keep] [-s NAME] script.py [args...]`
  is `new` + `exec` + `stop` in one command. It runs the script with
  `sys.argv` and `__name__ == "__main__"` set as for `python script.py args`,
  then releases the VM, unless `--keep` is given.
- **Exit codes propagate**: `run` exits 0 after `sys.exit()` or
  `sys.exit(0)`, N after `sys.exit(N)`, and 1 after `sys.exit("msg")`.
- **Streams are separate**: `run`'s own `[colab] ...` lines go to stderr and
  the script's output to stdout, so `run job.py > out.txt` captures only the
  script's output.
- It works as a shebang: `#!/usr/bin/env -S mighty-colab run --gpu T4`.
- A script path that does not exist fails before a VM is allocated.
- The run stops when the local process ends. Use `job` for work that must
  outlive the call.

### Unattended jobs (`mighty-colab job`)

`job` runs a script on a VM it provisions, as a process detached from the
kernel, so the run continues when the local process ends. It installs
dependencies, stages code and data, uploads artifacts, releases the VM, and
records the outcome in an envelope.

1. **Write a spec** (YAML). Paths in `code` are relative to the spec file.
   ```yaml
   name: my-experiment
   accelerator:
     prefer: [A100, L4, T4]   # tried in order; nothing else is substituted
     accept_cpu: false
   code:
     kind: bundle
     root: ./src              # becomes sys.path[0] on the VM
     entry: train.py          # relative to root
     args: ["--epochs", "50"]
   deps:
     - torch==2.4.1
   budgets:
     wall_clock: 7200         # seconds; the run is killed after this
   data:                      # optional: downloaded before the run
     - url: https://storage.googleapis.com/BUCKET/x.npy?X-Goog-Signature=...
       dest: /content/data/x.npy
       sha256: <64 hex characters>
       size_bytes: 104857600
   artifacts:                 # optional: uploaded after the run
     - path: /content/out/model.pt
       url: https://storage.googleapis.com/BUCKET/runs/model.pt?X-Goog-Signature=...
       required: true
   ```
   Inputs and outputs move over signed URLs (GET for `data`, PUT for
   `artifacts`), for example from `gcloud storage sign-url`. Every URL must
   resolve to public addresses.
2. **Plan**: `mighty-colab --auth=adc --json job plan job.yaml`. The envelope
   has the `job_id`, and `diagnostics` lists every problem; an error refuses
   the plan. `--no-probe` skips the network checks of the URLs.
3. **Apply**: `mighty-colab --auth=adc --json job apply --job-id <id> --async`
   returns at once with the process id and the path of its log.
   - `--leave-up` keeps the VM after the run.
   - `--timeout N` limits the whole call to N seconds. The default deadline
     is `wall_clock` + 600 s after launch; at the deadline, the run is
     cancelled and the VM released.
   - `--no-keepalive` starts no keep-alive daemon.
4. **Wait**: `mighty-colab --auth=adc --json job status <id> --poll` waits
   until the job ends. A plain `job status <id>` answers at once. For a job
   whose `apply` died, `job status --poll` collects the result and releases
   the VM.
5. **Read the envelope** (the `job` object in `job status --json`):
   - `done` is true when the four outcome fields are final: `workload`
     (`succeeded`, `failed`, `cancelled`, `unknown`), `offload`, `cleanup`
     (`released`, `already_absent`, `left_up`, `failed`) and `supervisor`.
     `ok` is true when the workload succeeded, the artifacts uploaded, and
     the VM was released or deliberately left up.
   - When something failed, `failed_phase` names the phase, and
     `retry_class` states what to do next: `fix_code`, `fix_human`,
     `retry_same`, `retry_different`, `refresh_urls` or `do_not_retry`.
     `reason` and `hints` explain it; `artifacts[]` and `inputs[]` carry
     each transfer's error with its HTTP status and response body.
   - `compute_units_at_provision` and `compute_units_at_release` are the
     account's balance at those two moments.
6. **Stop**: `mighty-colab job destroy <id>` stops the run and releases the
   VM; `--cancel-only` stops the run and keeps the VM.

`mighty-colab jobs list` lists the local job records, and
`mighty-colab jobs prune --dry-run` shows which finished records it would
delete. The full reference is
https://github.com/danbarua/mighty-colab/blob/main/docs/job/usage.md and
https://github.com/danbarua/mighty-colab/blob/main/docs/job/spec.md.

### Automate

- `mighty-colab auth -s <name>`: VM-side GCP credentials (interactive; an
  agent cannot run it).
- `mighty-colab drivemount -s <name> [PATH]`: mounts Drive at
  `/content/drive` (interactive; an agent cannot run it).
- `mighty-colab install -s <name> pkg1 pkg2` installs with
  `uv pip install --system` when `uv` is on the VM, and with `pip`
  otherwise. It also accepts `-r requirements.txt`.
- **`mighty-colab reinstall`** runs `install` and then restarts the kernel,
  if the install succeeded. Use it when the package may already be imported
  (for example when upgrading `jax` or `torch`): Python caches imports in
  `sys.modules`, so a plain `install` has no effect until the kernel
  restarts.

### Inspect and report

- `mighty-colab help` (or `help <cmd>`) lists and explains the commands.
- `mighty-colab sessions` lists the account's assignments and prunes local
  records whose assignment is confirmed gone. Orphans show as `[?]`.
- `mighty-colab status [-s <name>]` shows the hardware, the keep-alive
  health, the last execution, and a background job's log path.
  `LAST-KNOWN-LOCAL BUSY/IDLE` comes from local bookkeeping, not from a
  kernel query.
- `mighty-colab usage` shows the account's compute-unit balance, its hourly
  consumption, its number of assignments, and the GPUs and TPUs it may
  request.
- `mighty-colab log -s <name> [-n 20] [-t TYPE]` shows recent structured
  events; read it when a task fails.
- `mighty-colab log -s <name> -f` follows a running `exec-async` job's
  output until it finishes.
- `mighty-colab log -s <name> --tail [-n N]` prints that output once and
  exits. Use it instead of `-f` from a caller that cannot wait on an
  unbounded call, such as an MCP client; with `--json`, pass
  `--since-offset` with the previous `next_offset` to read only new output.
- `mighty-colab log -s <name> -o summary.ipynb` exports the session as a
  notebook (`.md`, `.txt` and `.jsonl` work by suffix).
- `mighty-colab url -s <name>` prints a browser URL that attaches the Colab
  web UI to the session (`--open` opens it).
- `mighty-colab skill` and `mighty-colab readme` print this skill and the
  README.

## MCP Server

`mighty-colab mcp` starts a stdio MCP server that exposes the CLI's
non-interactive commands as tools, for a *different* MCP client (for
example Claude Desktop). A tool whose command supports `--json` returns the
envelope as structured content beside the text. Job records are resources:
`job://<id>` (the envelope), `job://<id>/logs`, `job://<id>/files/<name>`,
`jobs://`, `jobs://running` and `jobs://done`. A `job://<id>` subscription
sends a notification when the workload leaves `pending` and when the job is
done. Do not run `mighty-colab mcp` from a shell you are using: it waits on
stdio and blocks the shell.

## Safety

- **Always `mighty-colab stop -s <name>` when done**; an idle VM still
  bills. `run` without `--keep` releases its VM even when the script fails.
  A `job` releases its VM when it ends, unless `--leave-up` was given or an
  artifact upload failed (check `cleanup` in its envelope).
- Local state lives under `~/.config/colab-cli/`: `sessions.json`,
  `settings.json`, `history/*.jsonl` and `jobs/<job_id>/`. Do not edit it by
  hand.
- **Isolate parallel or agent runs** with the global `--config <path>` flag
  (for example `mighty-colab --config /tmp/agent.json new -s work`). The
  keep-alive daemon and `job apply --async` receive `--auth` and `--config`
  automatically.

## Recovery

- **"Session not found", 404 or 401 on `exec`**: when the assignment still
  exists, `exec` refreshes the saved token. When Colab confirms the
  assignment is gone, the local record is removed and the command reports
  `session_lost`; run `sessions`, and create a new session with `new`. When
  the check is inconclusive, the record is kept and the command reports
  `session_access_failed`; retry the command.
- **An execution timeout or a wedged kernel**:
  `mighty-colab restart-kernel -s <name>` keeps the VM and resets the
  kernel; or `stop` and then `new`.
- **`exec --timeout N` can keep a local CPU core at about 100% and hang after
  the deadline**; the remote session is unaffected. `kill -9` the local
  process, then reattach with `mighty-colab status -s <name>` or `exec`.
- **The keep-alive daemon stopped**: `log` shows `keep_alive_stopped` with
  its reason and last error. A 401 or 403 there means the credentials or
  their scopes; see Authentication.
- **A job's `apply` was killed**: run `mighty-colab job status <id> --poll`;
  it collects the result and releases the VM when the run ends.
