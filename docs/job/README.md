# `job`: unattended runs on Colab

`job` is an application built on the session commands. You describe a run in
a YAML spec. `job` then provisions the VM, installs the dependencies, stages
the code and data, and launches the run as a process detached from the
kernel. When the run ends, `job` uploads the artifacts, releases the VM, and
records what happened and what to do next. Nobody has to watch it.

## `exec-async` or `job`?

`exec-async` and `job` both start long work without holding the caller open.
They differ in how much they do for you.

| | `exec-async` | `job` |
|---|---|---|
| The VM | runs in a session you created and must stop | provisioned for the job, and released when it ends |
| Where the run executes | inside the kernel, driven by a local background process | in a process on the VM, detached from the kernel |
| A dropped connection | can end the run, and the result is lost | does not affect the run after launch |
| Dependencies, inputs, outputs | you install packages and move files | the spec declares them; `job` installs, downloads and uploads them, and checks each input's size and SHA-256 |
| The outcome | you read the log and decide | an envelope records the outcome, the phase that failed, a retry class and the evidence |
| Best for | long steps while you iterate in a session you keep | runs that must finish, upload and clean up with nobody watching |

## Quickstart

1. Write a spec. Paths in `code` are relative to the spec file.
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
   ```
   Inputs (`data`) and outputs (`artifacts`) move over signed URLs; see
   [`spec.md`](spec.md).
2. Plan it. `plan` validates the spec, locks each source file's size and
   SHA-256, and allocates nothing:
   ```bash
   mighty-colab --auth=adc --json job plan job.yaml
   ```
   The envelope has the `job_id` and lists every problem in `diagnostics`.
3. Apply it. With `--async`, `apply` returns at once:
   ```bash
   mighty-colab --auth=adc --json job apply --job-id <id> --async
   ```
4. Wait for the outcome. `--poll` keeps polling until the job ends; without
   it, `job status` answers at once:
   ```bash
   mighty-colab --auth=adc --json job status <id> --poll
   ```

`job destroy <id>` stops a run and releases its VM, and `jobs list` lists the
local job records.

## What the envelope reports

- **Four outcome fields:** `workload` (did the code succeed), `offload` (did
  the artifacts upload), `cleanup` (was the VM released or left up) and
  `supervisor` (did the process that started the job finish). `done` is
  true when all four are final. `ok` is true when the workload succeeded,
  the artifacts uploaded, and the VM was released or deliberately left up.
- **Where it failed and what to do:** `failed_phase` names the phase, and
  `retry_class` is one of `fix_code`, `fix_human`, `retry_same`,
  `retry_different`, `refresh_urls` and `do_not_retry`.
- **The evidence:** `reason` and `hints` explain the outcome. `inputs[]`,
  `artifacts[]`, `install_attempts[]` and `provision_attempts[]` keep each
  failure's HTTP status, response body excerpt, exit code or installer
  output. Signed URLs never appear in these records.
- **Cost:** the account's compute-unit balance when the VM was granted and
  when it was released.

The job directory, `~/.config/colab-cli/jobs/<job_id>/`, keeps the VM's
records (`runner.log`, `install.log`, `result.json`, `watchdog.json`)
copied before the VM is released.

## Current limits

- `apply` makes one attempt; `retry_class` is advice only. Retry and resume
  are tracked in [#69](https://github.com/danbarua/mighty-colab/issues/69).
- Specs, signed URLs and SHA-256 locks are written by hand or by your own
  generator. A staging layer is tracked in
  [#68](https://github.com/danbarua/mighty-colab/issues/68).
- The source spec you write and the mode-0600 `.mighty-colab-secrets.json`
  beside the plan contain full signed URLs; protect them like credentials.
- The longest recorded runs are 96 minutes on an A100 and 180 minutes on a
  CPU. Whether a GPU VM stays assigned without the keep-alive daemon has not
  been tested.

## Documentation

| document | what it covers |
|---|---|
| [`usage.md`](usage.md) | running jobs: the commands, reading the result, failure modes and cost |
| [`spec.md`](spec.md) | every spec field, plan refusals, and how to sign URLs |
| [`design.md`](design.md) | the architecture, the phases and the rules each one follows |
| [`store-and-cleanup.md`](store-and-cleanup.md) | the local job records and what `jobs prune` deletes |
| [`mcp.md`](mcp.md) | job tools and `job://` resources over MCP |
| [`chronology.md`](chronology.md) | each change and finding, with its evidence |
