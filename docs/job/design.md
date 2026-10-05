# Design: `job` — Agent job supervisor

`mighty-colab job plan|apply|status|destroy` and `mighty-colab jobs list|prune`
run an unattended job on a Colab VM. The code is in `src/colab_cli/job/` and
`src/colab_cli/commands/job.py`. This document describes the current
implementation and its known gaps. The other documents in `docs/job/`:

- [`spec.md`](spec.md): the spec file, field by field.
- [`usage.md`](usage.md): the command guide.
- [`store-and-cleanup.md`](store-and-cleanup.md): local job records and what `jobs prune` deletes.
- [`mcp.md`](mcp.md): the MCP resources and notifications built on this supervisor.
- [`chronology.md`](chronology.md): dated changes and findings, each with its evidence.

`run` stays the shebang (`new` + text-into-kernel + `stop`). `job` is the unit of work an unattended agent actually has: code, deps, data, artifacts, accelerator policy, two clocks, teardown.

## Motivation

An agent composing `new` → `reinstall` → `exec-async` → `log --tail` → `stop` meets the same failures every time (`../AGENT_USABILITY_LEARNINGS.md`): output-gap `--timeout`, text-not-a-file `__file__`, Jupyter upload ceilings, interactive VM auth, teardown skipped on a failing `exec`, exit 0 with no verdict.
Those steps form a state machine, and the state machine belongs in code, not in a skill.

## Non-goals

- Notebooks, Drive, `colab auth`, SSH-over-WSS.
- Growing `run` / changing upstream command flags.
- Terraform reconciliation (loop apply until the world matches). `job` takes only the plan/apply/destroy command shape.
- Framework instrumentation (PyTorch hooks, JAX callbacks). Workloads here are hand-rolled JAX more often than not.
- Consumer `import mighty_runtime` as a requirement. There is no stall kill, so there is no `pulse()` either.
- CLI-side GCS signed URL minting from ordinary user ADC (no private key; `signBlob` needs a service account).
- A DAG of jobs. GCS/HTTP is the queue between jobs.
- Mid-run `install`/`reinstall` issued by apply itself.

## Layers

```
agent
  └─ job plan / apply / status / destroy     # local CLI, --json envelopes
        ├─ Colab control plane               # assign, unassign, keep-alive, Contents API
        └─ remote Python
              ├─ kernel                      # IDLE babysitter; short launch RPC only
              ├─ python -m mighty_runtime.runner
              │     └─ python -m mighty_runtime.shim <entry>
              │           # runpy's the entry in this process; real __file__, argv, sys.path[0]
              └─ python -m mighty_runtime.watchdog
```

`mighty_runtime` is a Python package placed on the VM disk. It is not a Jupyter plugin. The kernel is remote Python that can spawn Python. Transport (websocket, Contents API) does not leak into the agent contract or the skill.

## User surface

| command | effects | analogue |
|---|---|---|
| `job plan SPEC_FILE [--out PATH] [--no-probe]` | writes local records and optionally probes data URLs; no VM | `terraform plan -out` |
| `job apply [PLAN_FILE] [--job-id ID] [--timeout S] [--leave-up] [--async]` | allocates and drives a VM; `--async` runs it as a detached process | `terraform apply plan` |
| `job status JOB_ID [--poll] [--interval S]` | reads the local record and, when possible, the VM | refresh |
| `job destroy JOB_ID [--cancel-only] [--wait S]` | cancels and/or unassigns | `terraform destroy` |
| `jobs list [--running \| --done]` | lists local job records | local inventory |
| `jobs prune [--dry-run]` | deletes local records that are safe to delete | — |

The MCP server exposes `job_plan`, `job_status`, `job_destroy`, `jobs_list`, and `jobs_prune` as tools. It excludes `job_apply`, which blocks for the job's whole run.

`provision` is a phase of apply, not another command.

Apply consumes a plan file directly or retrieves one by `--job-id`. Generated plans replace credential-bearing URL queries with canonical identities plus markers; apply hydrates them from the adjacent owner-only sidecar before allocation. It verifies the plan hash, including source-spec path, credential markers, and the source-file lock (relative path, size, SHA-256). Added, removed, renamed, or changed source files fail before assignment. Staging uploads only files covered by that lock.

`apply --async` starts a detached `job apply` with the same arguments, writes its output to `apply.log` in the job directory, and returns the job ID, PID and log path. The detached process performs every check a foreground apply does.

## Spec

The accepted fields are defined by `JobSpec`; unknown fields are rejected. This schema-valid baseline shows the implemented names and concrete enum values:

```yaml
name: train-cls0
ignore_warnings: false
accelerator:
  prefer: [A100, T4]
  accept_cpu: false
code:
  kind: bundle
  root: .
  entry: train.py
  args: ["--epochs", "3"]
deps: [torch==2.4.1]
budgets:
  wall_clock: 3600
retry:
  when: [retry_same]       # `when`, not `on`: YAML 1.1 reads bare `on:` as true
  max_attempts: 1
  budget_seconds: 14400
  mode: recreate
on_offload_fail: leave_up
on_run_fail: offload_anyway
```

Optional `data[]` entries contain `url`, `dest`, `sha256`, and `size_bytes`. Optional `artifacts[]` entries contain `path`, `url`, `required`, and `size_bytes`. `budgets.artifact_sync_interval_seconds` re-uploads declared artifacts during the run. `control.result` and `control.log` each accept paired `put_url` and `get_url` values. `code.kind` is only `file` or `bundle`; there is no `git`, `checkpoints`, or `credentials` field.

For a GCS-backed `control.result`, first sign PUT, PUT a fresh `{}` placeholder using `Content-Type: application/octet-stream`, and then sign GET for the same unique object. The runner replaces the placeholder with its terminal result. `{}` is not a verdict. When the VM result is unavailable, `status` and `destroy` read the GET URL as a bounded fallback and absorb only a terminal result.

Signed query strings are credentials. Generated `spec.json`, `plan.json`, explicit `--out` plans, remote manifests, envelopes, events, and validation diagnostics expose only canonical URL identities and opaque references. Full URLs remain in the caller-owned source spec and an adjacent owner-mode `<plan>.mighty-colab-secrets.json` sidecar. Apply sends them to an owner-mode remote handoff only after all public files; the launch kernel opens and unlinks it, then passes the inherited descriptor to an isolated runner. The runner clears inherited URL variables before consumer launch. Recovery confirms deletion or forcibly releases the assignment.

This boundary prevents durable disclosure, diagnostic reflection, and ordinary launch-time inheritance by the consumer. It does not defend against hostile same-UID code that runs before credential upload: a dependency install hook, `.pth` file, or existing process can persist and inspect the later handoff or launch process through `/proc`. Requirements, their build/install hooks, and the single-user job VM are therefore trusted inputs. Dependency installation completes before credential upload, and `-I -S` prevents accidental installed-package imports in the runner bootstrap; neither mechanism is a privilege boundary against malicious dependencies.

`apply` runs exactly one attempt. The only executable policy values are `retry.when: [retry_same]`, `max_attempts: 1`, `mode: recreate`, `on_run_fail: offload_anyway`, and no `control.log`; planning rejects other values instead of accepting inactive behavior. `retry.budget_seconds` is used only for control-URL expiry validation. There is no retry, resume, checkpoint, or control-log implementation.

Relative data destinations and artifact paths resolve under `/content/jobs/<id>`. Absolute paths are accepted only after canonical resolution below `/content`; launcher-owned paths below the job directory are reserved.

### Budgets

`wall_clock` is enforced by the watchdog. On breach it writes intent, sends SIGTERM to the shim's process group, waits through the grace period, and escalates to SIGKILL. `retry.budget_seconds` controls control-URL expiry validation only; there is no retry loop for it to bound.

There is no stall kill. A healthy JAX/XLA compile can print nothing for a long time, and killing on quiet stdout, as `exec --timeout` does, kills such runs. The consumer does not import `mighty_runtime`, so there is no progress signal that needs no cooperation from it. The watchdog reports telemetry and inactivity; `wall_clock` is the only kill that requires nothing from the consumer.

## Remote process tree

The durable workload is a runner process, not the launch kernel:

```
kernel launch RPC
  └─ python -I -S -c <isolated runner bootstrap, inherited secret fd>
       └─ python -m mighty_runtime.shim <entry>
watchdog process (sibling)
```

Before launch, the local stage phase uploads `mighty_runtime`, user code, and query-free manifests through the Contents API. `kind: file` uploads only the entry file. `kind: bundle` walks the root and uploads files individually, excluding `.git`, `.venv`, `__pycache__`, `.pyc`, the active source spec, secret sidecars, and reserved atomic-secret temporaries. Each user file is copied once from an `O_NOFOLLOW` descriptor into an immutable local snapshot; that same snapshot is scanned and uploaded, closing the scan/upload race. Arbitrary content is rejected when an HTTP(S) query uses a recognized credential key. YAML-shaped mappings are parsed regardless of filename extension and reject any query-bearing value in `url` or `*_url` fields, covering custom signers while allowing ordinary query URLs in source code.

`Orchestrator.launch()` calls `ColabRuntime.execute_code(..., timeout=120)`. When the plan declares any transfer URL, sealing and launch both require the private handoff; absence fails before a consumer starts, and the runner independently enforces `--secrets-required`. The kernel opens and unlinks the handoff, then starts the runner with `start_new_session=True`, `python -I -S -c`, and an inherited descriptor rather than an argv/environment URL. Passing no output hook does not create a separate non-interactive protocol: the vendored client still uses its interactive execution loop internally. The 120-second limit covers the execute reply; kernel HTTP/WebSocket startup retains its shorter defaults.

The runner creates `launch.json` with `O_EXCL`, starts the shim in its own session/process group, and remains its parent so it can `waitpid()`. The shim sets the entry's real `sys.argv`, `__file__`, and `sys.path[0]`, then uses `runpy.run_path(..., run_name="__main__")`. The consumer runs with `PYTHONUNBUFFERED=1`. A duplicate runner sees the existing live launch identity and exits without starting a second consumer; the launch RPC does not promise to return the original runner PID.

The runner maps normal exits, exceptions, signals, cancellation intent, wall-clock expiry, and descendant-survival checks into `result.json`. `cancel.json` has one shape whoever writes it, `{"intent": "cancelled", "by": <requester>, "at": <time>}`: `wall_clock` (the runner or the watchdog at the deadline), `job destroy` or `job apply --timeout`. One that cannot be parsed still counts as a cancel, by an unknown requester. Both runner and watchdog consume an externally written `cancel.json`, send SIGTERM to the shim process group **and** to processes tagged with `MIGHTY_JOB_ID`, and escalate to SIGKILL after the grace period. The process group is not a containment boundary, because a `setsid` grandchild leaves it; the job tag is. The runner carries the tag too, so each sweep excludes the other supervisor process: the runner excludes the watchdog, and the watchdog excludes the runner (its pid from `launch.json`), because after a cancel or the deadline the runner is the process that reaps the workload, uploads artifacts and writes `result.json`. Tagged kills are skipped unless pid+starttime+boot_id still match, so a reused PID is not signalled. Any `OSError` from `killpg` means nothing is left to signal (BSD returns `EPERM`, not `ESRCH`, for an empty group). A process in state `Z` (exited, not yet reaped) or `X` counts as gone in every identity and descendant check: the launch kernel never reaps the runner, so a killed runner stays a zombie with its original start time while the VM lives. `succeeded` requires Linux `/proc` escapee detection and an empty tagged set after that reap; otherwise the workload is `unknown` or `failed`. `destroy --cancel-only` writes cancel intent without unassigning. The runner then attempts declared artifact PUTs and, when configured, `control.result.put_url`. An optional artifact that is absent does not fail offload, but any artifact PUT recorded as `failed` makes scalar `offload: failed`, irrespective of `required`.

Besides the verdict, `result.json` records:

- `signal_name`, named on the VM, because Linux and the supervisor's platform number some signals differently (SIGBUS is 7 on Linux, 10 on macOS);
- `inputs`: one record per staged input up to the first failure, with `dest`, `url_id`, `status`, `bytes` and `sha256`, and an `error` on the failed one;
- `oom_kills`: how much `/proc/vmstat`'s `oom_kill` counter rose during the run (null where it is unreadable), with the kernel's last `Killed process` lines from `dmesg` in `oom_log`. On Colab the job's cgroup reports `memory.max` as `max` and its `memory.events` did not count an OOM kill that `/proc/vmstat` did; the kernel logged that kill as `Memory cgroup out of memory`, so the limit that fired belongs to an enclosing cgroup. `dmesg` is readable there;
- `offload_error` when the offload manifest itself cannot be read;
- `runner_warnings`: non-fatal supervisor problems, each also logged to `runner.log`: a probe that failed, an unreadable `cancel.json` or `exception.json`, a watchdog that did not start, a periodic sync that kept failing.

The envelope keeps `inputs` (left out when empty) and adds each warning as a `runner: ...` hint. A failed result PUT to `control.result` is logged with its status, reason and response body. An exception in the runner's own wait loop is recorded as `runner_error` and its traceback logged.

With `budgets.artifact_sync_interval_seconds` set, the runner also re-uploads each declared artifact on that cadence during the run, inside its wait loop. It skips a file whose size and mtime are unchanged since the last successful sync or changed during a one-second check, and logs each successful sync to `runner.log`. A failed sync is logged with its status and reason, and a path whose syncs failed is reported in `runner_warnings` with the count and the last reason; an early 403 there predicts that the end-of-run upload will fail too. A failed sync never stops the run. The end-of-run offload still runs and is the one recorded in `result.json`.

The watchdog is a sibling process. It enforces wall clock and reports telemetry every 30 seconds in `watchdog.json`, including `runner_alive`. When `launch.json` gives no usable pid, `runner_alive` is null and `runner_identity_error` says why: liveness is unknown, and the supervisor does not take it for a dead runner. `gpu_error` says why nvidia-smi gave no reading (not found, a timeout, or its exit code and stderr, where a driver fault shows up), and `disk_path` is the path whose free space was measured: the job directory, else `/`. A `watchdog.json` that cannot be written is logged to `runner.log` and does not stop the deadline kill. The supervisor's poll hint shows the GPU error and the identity error. Job provision pre-flights the TFE keep-alive ping, records success or a tolerated failure on the shared `SessionState`, then starts the keep-alive daemon after persisting that session. A missing OAuth scope in the pre-flight releases the VM and fails provisioning with `fix_human`. Cleanup stops the daemon on release or confirmed absence and leaves it running when the VM is deliberately left up. `job status` respawns the daemon when it finds it dead on a job whose cleanup is pending. `mighty-colab status -s job-<job-id>` and `sessions` expose the same keep-alive health summary as sessions created by `new` or `run`.

The local apply supervisor polls `result.json` and `watchdog.json`, and copies `runner.log` on every healthy poll tick. When `watchdog.json` reports `runner_alive: false` and a second read still finds no `result.json`, or when the poll has never read the runner's records and `launch.json` is still absent 120 seconds after the launch RPC returned and has stayed absent for 90 seconds, apply records `workload: unknown`, a terminal offload, and `retry_same`, with a reason that names which, and releases the VM instead of waiting for its local deadline. The 90-second window is longer than `JobTransport`'s once-a-minute token refresh on a 404, because an expired proxy token also answers 404; and once the runner's records have been read, a later absence is never taken as a verdict. `job status` additionally reads `launch.json` identity and `watchdog.json` `runner_alive` so a dead runner without `result.json` becomes `workload: unknown`. Stage, poll, cancel, record copy, and recovery share one `JobTransport`: Contents requests carry connect/read deadlines, assignment re-resolution is bounded by the same timeout, and a timed-out write is confirmed before retry. Exhausted transport stalls stay `degraded` unless the assignment is proven gone.

`done` is decided from `result.json`, never from the runner's output stream closing: an escaped descendant inherits the runner's stdout pipe and can hold it open long after the verdict exists.

`waitpid()` cannot always provide a Python exception. `os._exit()`, SIGKILL/OOM, and native crashes can produce `workload: failed` with exit/signal information and no exception.

The shim records a Python exception in `exception.json`. A type outside builtins is module-qualified (`torch.OutOfMemoryError`). A traceback over 6000 characters keeps its first 2000, which for a chained exception hold the original cause, and its last 4000, with a line saying how many characters were left out; the full traceback goes to `runner.log`. A message over 2000 characters is cut the same way. A `sys.exit()` with a non-zero code keeps the traceback to the call.

## Runtime-proxy token expiry

Contents requests go through the runtime proxy with a token that expires about 60 minutes after it is issued. After expiry the proxy answers 401 or 404 while the assignment, the VM and its files are intact, so a 404 does not prove that a file or the VM is gone. Control-plane calls (`assign`, `unassign`, keep-alive, the assignment listing) use the user's own credentials and are not affected.

`JobTransport` handles expiry for every Contents operation `job` makes. On a 401 or 404 it lists assignments. If the endpoint is absent, the result is `session_lost`. If it is present, the transport takes the fresh token and proxy URL from the listing, rebuilds its Contents client, persists the session record, and retries once. A 404 on a read triggers this refresh at most once a minute, and a 404 after a successful refresh means the path is absent. When the listing itself fails, the result is `degraded`, never `session_lost`. Taking both the token and the URL covers both token expiry and a changed proxy URL; the live evidence has not separated the two (see `chronology.md`).

`control.result` is a second copy of the verdict for the case where the VM really is gone; it is not a substitute for the refresh. Its GET (`fetch_control_result`) follows the same public-destination policy as the plan's probe and the runner's transfers (`urlopen_public`). The keep-alive daemon, not the watchdog, keeps the idle VM assigned.

## Data plane

`job` accepts caller-supplied HTTPS GET/PUT URLs. The VM uses `urllib` for GETs and `http.client` for PUTs; it has no GCS client or service-account-key mode.

`plan` uses a one-byte ranged GET only for `data[]` URLs. It does not issue HEAD and does not mutate artifact or control destinations. Every probe result other than 206, or 416 for an empty object, is a `ranged_get_failed` error, classified through `verdict.transfer_retry_class` like a data GET on the VM, with the first 300 bytes of an error body redacted by `redact.redact_url`, which the runner shares: it replaces the URL and its query string wherever either appears verbatim, then applies the general patterns. A GCS `SignatureDoesNotMatch` body quotes the canonical request (the query re-encoded, without the signature) after about 380 bytes, beyond what is kept; the signature is never in the body. A measured size that differs from `size_bytes` is `data_size_mismatch` (fix_code). A size measured for an input with no `size_bytes` goes into the plan's `probed_size_bytes`, is used by `verify`'s disk check, and is reported as an `info` diagnostic; it is never sent to the runner as an expected size, so an object replaced after planning does not fail staging.

`planner.expiry_problems` parses recognizable signature expiry fields on all URL fields and serves plan, apply's preflight and `verify`. Data and artifact URLs must cover `wall_clock` + 15 minutes, plus `INSTALL_ALLOWANCE_SECONDS` (the install kernel call's limit, 55 minutes) when `deps` is not empty and install has not run. Control URLs must cover the later of `retry.budget_seconds` + 15 minutes and that data deadline. `apply` checks before assignment, with the allowance; `verify` checks again after install, without it, and fails with `refresh_urls`. After install, control URLs also get only the data deadline: `retry.budget_seconds` is counted from apply, and the result PUT is due at the end of the run. For GCS control channels, planning also requires the paired PUT and GET URLs to identify the same bucket and object.

The public-host check resolves DNS once per URL per plan and rejects a destination unless every IPv4 and IPv6 answer is global unicast (`url_host_not_public` names the addresses); a host that does not resolve is `url_host_unresolved` (fix_code) with the DNS error, and is not probed. A URL with no host, or a host or port that does not parse, is `url_malformed` (fix_code); a URL that is not `https` gets only the scheme error. Mixed public/non-public answers fail closed. Each Contents-independent GET/PUT connects to an address from that lookup with the original hostname as SNI/Host, so DNS cannot be rebound between check and connect. Redirect targets are resolved and checked the same way. HTTPS remains required.

The runner streams each data GET and artifact PUT in 1 MiB / 64 KiB chunks while computing SHA-256. Source staging uploads each file through the Contents API. Plan rejects any source file over the 250 MB Contents limit and warns when the aggregate source payload exceeds 250 MB; apply refuses an oversized source file again before assignment. Verify reports source, input, output, and free-space totals and refuses when their sum exceeds 80% of free disk.

## Plan

`job plan` never calls `assign`, but it is not side-effect-free: it writes redacted `spec.json` and `plan.json` records below the job store, writes a redacted `--out` plan when requested, creates adjacent owner-mode secret sidecars when query credentials exist, and performs ranged GET probes unless `--no-probe` is set. It writes generated records even when diagnostics contain warnings or errors.

Errors make `plan` exit non-zero and make `apply` refuse the saved plan. Warnings make apply refuse unless the embedded spec has `ignore_warnings: true`. Plan checks:

- spec validity: unknown fields, PEP 508 `deps`, and no query-credential URLs in `deps` or `code.args`;
- accelerator names;
- code-entry containment and existence;
- the source lock, built once here and stored on the plan: one that cannot be built is `source_unreadable` (fix_code for a layout problem such as a symlink, naming the file; fix_human for an unreadable file), unless a missing or escaping entry is already reported;
- source size: an error for a file over 250 MB, a warning for an aggregate over 250 MB;
- destinations: containment below `/content`, reserved paths, collisions;
- URLs: HTTPS, a public host after DNS resolution, signed-URL expiry, and paired GCS control-object identity;
- data ranged GETs and measured sizes;
- unimplemented retry and policy settings;
- `budgets.artifact_sync_interval_seconds`: an error when not positive, warnings when there are no artifacts or the interval is not smaller than `wall_clock`;
- missing `size_bytes` on artifacts, and on inputs the probe did not measure (warnings).

A spec that will not load is reported with the YAML problem and its position (not PyYAML's own text, which quotes the source line), or the OS error. A plan that will not load names each invalid field without its value, the OS error, or the JSON position. `apply` refuses on the plan's own errors and warnings before checking the source lock, and with `--json` its refusal envelope carries those diagnostics, or the expiry problems. When `apply --async` refuses or crashes before writing an envelope, `job status` and `jobs list` point at the job's `apply.log` and quote its last line.

Plan does not resolve dependencies, validate ADC scopes, analyse sibling imports for `kind: file`, check PUT/GET object equivalence for non-GCS URLs, or probe artifact and control destinations.

The job ID is `<name>-<UTC timestamp>-<six random hex characters>`. `spec_hash` on the stored plan is `plan_hash`: canonical modeled spec (URL identities and credential-presence markers), source-spec path, and the source-file lock. Re-signing the same object does not change the spec identity. Apply hydrates from the owner-only sidecar, revalidates URL expiry, the plan hash, and the on-disk source bytes before assignment.

## Apply phases

The phase order is:

```
plan.json
  -> provision   assign; persist endpoint/session in envelope; persist the session; start keep-alive
  -> install     install pinned dependencies: uv, then pip if uv fails
  -> restart     restart the launch kernel
  -> verify      probe dependencies, device, and declared input disk need
  -> stage       upload runtime, source files, and stage/offload manifests
  -> run         launch runner/watchdog; runner performs data GET, consumer run,
                 artifact PUT, and optional control-result PUT
  -> offload     absorb the runner's artifact results locally
  -> cleanup     copy VM records locally, then unassign; or report already
                 absent, or leave up
```

`install` and `restart` run only when `deps` is non-empty. `install` precedes `stage`, so a bad dependency pin fails before source upload and disk is measured after installation. Data GET is executed by the remote runner during `run`, not by the local stage phase.

`provision` walks `accelerator.prefer` in order, then CPU when `accept_cpu` is true. A GPU request granted as CPU is released and the next preference tried; if that release fails, provisioning stops with the endpoint kept so cleanup retries the release. An account at its concurrent-assignment limit (412) fails with `fix_human` and the response body. Each failed assign is classified by `verdict.assign_retry_class`: 400 (no quota or entitlement for that accelerator), 401 and 403 are `fix_human`; 408 and 429 `retry_same`; 5xx and any other status `retry_different`; no response `retry_same`; any other exception, such as a response that does not parse, `do_not_retry`. When every candidate fails, the job takes the strongest class (`fix_human` over `retry_different` over `retry_same`). The envelope's `provision_attempts` lists each candidate with its outcome, status, the error with the assign URL's query removed, and a JSON body excerpt or a note that a body of another type (Colab's HTML error page) was not kept; it is kept whenever any candidate was not granted and left out on a first-try grant.

`install` (`colab_cli/job/install.py`) runs one kernel call that tries `uv pip install --system` and, if uv fails or is absent, `pip install -v --upgrade-strategy only-if-needed`. Each installer has its own 25-minute budget and a closed stdin; the kernel call's own timeout outlasts both, so an installer timeout is never mistaken for a lost connection. Each installer writes into `install.log` on the VM, between a header (installer, version, command, and the index configuration that installer reads: its environment variables, and `pip.conf` for pip, credentials redacted with `redact.py`'s patterns) and a footer (exit code, timeout, seconds). Each attempt is classified from its output as `resolution`, `build`, `auth`, `transient`, `timeout` or `unknown`, using rules built from output captured on Colab (`tests/fixtures/installer_failures.json`, produced by `integration/capture_installer_failures`). pip reports an index answering 403, 429 or 500 only as "No matching distribution found", while uv names the status, so the job's retry class combines all attempts: `auth` gives `fix_human`, otherwise `transient` gives `retry_same`, otherwise `fix_code`. pip counts as transient only once its retries are exhausted. The reason names the packages and each installer's exit status, class and key lines, including pip's failed-build stderr, within 1500 characters. The envelope keeps the attempts as `install_attempts` when the install failed or fell back to pip; a first-try uv success is a one-line hint and the field is left out. A failure of the install step itself, before any installer reported, is `do_not_retry` with the kernel's error.

Apply's own restart uses an explicit 60-second timeout; any failure is `retry_same`, and the reason says whether it timed out or names the error. `verify` fails with `fix_code` when a declared dependency is not importable after the restart, and with `retry_different` when a GPU was granted but none is visible or the declared payloads exceed 80% of free disk.

Apply tries one attempt. `RetryClass` is advice for the next caller action, not an automatic retry engine. Planning rejects non-default `retry.when`, `max_attempts`, and `mode` values. The classes are `fix_code`, `fix_human`, `retry_same`, `retry_different`, `refresh_urls`, and `do_not_retry`. Not every outcome has one: a workload cancelled by `job destroy` (with or without `--cancel-only`), and a failed release after a successful workload, leave `retry_class` null.

`colab_cli/job/verdict.py` turns the runner's result into `reason` and `retry_class`:

| runner's verdict | `retry_class` | `reason` |
|---|---|---|
| `wall_clock` kill | `fix_code` | `wall_clock budget of <n>s reached; the workload was stopped by SIGTERM (15)`, or that it did not exit within 5 s of SIGTERM and was killed by SIGKILL |
| a signal with no cancel request | `fix_code` | `the workload was killed by SIGKILL (9) with no cancel request`, then the OOM evidence: how many times the kernel's out-of-memory killer ran, with its last `Killed process` line, or that no OOM kill happened during the run |
| an exception | `fix_code` | `the workload exited 1: <type>: <message>`; a message over 500 characters is cut, with a note of how much was left out |
| a non-zero exit without an exception | `fix_code` | `the workload exited <n> without a recorded exception; its output is in runner.log` |
| tagged processes survived | `fix_code` | `the workload exited 0 but <n> tagged process(es) survived containment: <pids>` |
| cancelled by a requester | none (`--timeout` sets `retry_same`) | `cancelled by <requester>; <how the workload stopped>` |
| `unknown` (the runner could not decide) | `do_not_retry` | `the runner could not reach a verdict: <error>` |

An OOM kill of another process during a failed run, such as a DataLoader worker, is added after the exception the same way.

A data GET during staging and an artifact PUT at offload are classified from the runner's record. With a response: 401 and 403 are `refresh_urls`; 404 is `fix_code` for a GET (the object is not at that URL) and `refresh_urls` for a PUT; 408, 429 and 5xx are `retry_same`; any other 4xx, 413 included, is `fix_code`. Without a response, by the record's `category`: `network` (DNS, a refused or reset connection, a timeout, TLS, an upload whose response could not be read) is `retry_same`; `checksum` and `size` (the bytes do not match `data[]`) and `local` (a disk or file error on the VM) are `fix_code`; `blocked` (the URL resolved to a non-public address) is `fix_human`; `setup` (the runner's own manifest or credential channel is unusable) is `do_not_retry`; anything else is `retry_same`. A staging failure before any input was fetched is a `setup` fault, `do_not_retry`. Several failed uploads take the strongest class, in the order `do_not_retry`, `fix_human`, `fix_code`, `refresh_urls`, `retry_same`: a spec change means a new plan with new URLs, and new URLs cover a retry. When the workload failed or was stopped too, its class decides and the reason names both. An unreadable offload manifest is `do_not_retry`.

A kernel execute call that raises is classified by phase. A transport failure (the kernel client's "Connection was lost.", "You must first start a kernel", heartbeat or reply timeouts, websocket and HTTP errors) is `retry_same`; anything else keeps its type and message and is `do_not_retry`. A call that returns a `KeyboardInterrupt` error output is treated the same way, as `kernel interrupted during <phase>`: Jupyter interrupts a busy kernel before shutting it down or restarting it, so the running cell ends with that error instead of the call raising. A lost or interrupted reply to the launch call does not fail the job: the runner is detached, so `poll` decides from its files, and cleanup checks the credential handoff file because no pid proves the launch kernel consumed it. Kernel error outputs keep their exception name and value even with an empty traceback, without ANSI codes. `payload_bundle`'s own refusals during stage are `fix_code` with their message, and an unreadable local source file is `fix_human`; a Contents transport failure during stage is `retry_same`, or `retry_different` when the assignment is gone, and its reason names the file, the error with credentials removed, the retries spent, and a token refresh that failed. A failed keep-alive pre-flight ping is recorded in a hint and tolerated; a keep-alive daemon that cannot start fails provisioning with `fix_human`, because without it Colab reclaims the idle VM mid-run. A result `poll` cannot absorb ends the poll with the parse error and the raw verdict fields in the reason.

Unexpected exceptions are caught unless `--debug` is active, and the reason records `internal supervisor failure in <phase>: <type>: <message>`. The supervisor persists and emits a terminal envelope: pre-run failures become `workload: failed`; failures during or after run without a remote verdict become `unknown`; terminal remote verdicts are preserved. The endpoint is persisted in the envelope before keep-alive starts. Apply claims an exclusive lock on the job ID before assignment; a live second owner fails before `assign`, a dead owner is taken over, and a job that already has an endpoint is refused.

A local supervisor that stops early never leaves the VM without someone responsible for it:

- Apply's deadline is, by default, `wall_clock` + 600 s after launch, so provisioning and a long install do not eat into the run's own `wall_clock`. An explicit `--timeout N` is a budget for the whole apply call from its start (an agent's tool-call limit). When the deadline passes with no verdict, apply writes the cancel intent, waits up to 300 seconds for the runner's result (stopping early if the runner is dead or never started), absorbs it if it arrives, and releases the VM. `failed_phase` is `run`, `retry_class` is `retry_same` unless the result says otherwise, and the reason names the deadline and what the wait found. The run has failed to produce a verdict in time whatever the runner reports after the cancel. `request_cancel` and `deadline_reason` in `orchestrator.py` do the cancel and compose the reason for apply and for `job status --poll` alike.
- SIGTERM and SIGHUP are handled, where Python's default for both exits with no cleanup at all: an agent harness ends a tool call that ran too long with SIGTERM, and a closed terminal sends SIGHUP. Before launch they release the VM, like Ctrl-C. After launch, whoever started `apply` may not come back, so `apply` hands the job to a detached `job status <id> --poll` (stdio in `status-poll.log` in the job directory), the same detached spawn `job apply --async` uses. It is started only after `apply` has cleared its supervisor identity, so it treats the job as orphaned: it collects the result when the runner writes it and releases the VM. A second signal during the cleanup the first one started is ignored, and the reason names the signal. SIGKILL cannot be handled; a job whose `apply` was killed that way is recovered by `job status --poll` (below), so an agent driving a long job should use `job apply --async`, whose detached process a tool-call limit does not reach.
- Ctrl-C before the runner is launched releases the VM: `workload: cancelled`, `retry_class: retry_same`, and the reason names the phase apply was in.
- Ctrl-C after launch leaves the detached run going. Apply records `supervisor: interrupted`, leaves `cleanup` pending, not `left_up`, closes its local kernel client and exits; the keep-alive daemon keeps the VM assigned. Because the supervisor is gone and cleanup is pending, `job status --poll` treats the job as orphaned: it absorbs the result when the runner writes it and releases the VM. `job destroy` stops the job and releases the VM at once. The envelope's hints give both commands; they and the interruption's reason are dropped once a later result is absorbed and the VM released.

If the credential handoff could not be confirmed deleted, apply releases the VM in every case.

## Envelope, `done`, `ok`

Four state fields have closed enumerations:

| field | non-terminal | terminal |
|---|---|---|
| `workload` | `pending` \| `running` | `succeeded` \| `failed` \| `cancelled` \| `unknown` |
| `offload` | `pending` \| `running` | `ok` \| `skipped` \| `not_required` \| `failed` |
| `cleanup` | `pending` \| `running` | `released` \| `already_absent` \| `left_up` \| `failed` |
| `supervisor` | `running` \| `degraded` | `finished` \| `interrupted` |

`done` is true only when all four fields are terminal. `ok` is calculated independently:

```
ok = workload == succeeded
     and offload in {ok, not_required}
     and cleanup in {released, already_absent, left_up}
```

Therefore `ok` can be true while `done` is still false; consumers must poll `done` before interpreting `ok`. `left_up` counts as `ok` but still bills. `cleanup: failed` means release was not confirmed and the endpoint may still bill.

`failed_phase` names the phase whose failure decided the outcome. It is set once, at the first failure, and later failures do not change it: a failed release after a failed run keeps `run`. It is null when nothing failed: success, cancellation (including `job destroy` of a running job and Ctrl-C), or a job still running. It takes these values:

- the phase of `apply`'s own step that failed: `provision`, `install`, `restart`, `verify`, `stage`, or `run` when the launch call fails; an unexpected exception records the phase apply was in;
- `stage` when the runner's staging fails (a data GET or its sha256 check);
- `run` when the workload fails or ends `unknown`, the runner dies or never starts, the assignment is lost, or apply's `--timeout` passes with no verdict, whether `apply`'s poll or `job status` finds it;
- `offload` when the workload succeeded and an artifact upload failed or a required artifact was not produced;
- `cleanup` when nothing before it failed and the release failed, the VM was left up with surviving descendants, or `job status` or `job destroy` forced a teardown of a job without a verdict because the transfer credential deletion could not be confirmed.

`phase` is the last phase reached, which is `cleanup` once the VM has been released. An agent decides its next action from `failed_phase` and `retry_class`; `reason` is text for a person and is not a stable format. The human output of `job status` prints `failed_phase` as `failed in:`.

The outer JSON `status` and `exit_code` describe the CLI invocation. Job state remains nested: a successful `job status` query that reports a failed workload exits zero with outer `status: ok` and nested `ok: false`, while `job apply` exits one when the workload or cleanup it performed fails. Expected preflight and not-found errors emit one validated base envelope with an actionable message.

`not_required` means the spec declared no artifacts. `skipped` means the runner never reached offload: a stage failure, a dead runner, a runner that never started, or a lost assignment. A failed workload still attempts its declared artifacts. Because a skipped offload is not a failed one, `on_offload_fail: leave_up` does not keep the VM up for these cases. Nor does it for a required artifact that was never produced, which fails offload with `fix_code`: `leave_up` exists so a file that failed to upload can be rescued from the VM, and only an attempted upload that failed (an artifact recorded `failed`) leaves a file to rescue. `Orchestrator.leave_up_requested` owns that rule; `apply` combines it with `--leave-up` and overrides both when the transfer credential's deletion was not confirmed, and `cleanup` keeps the VM only when told to. Per-artifact results are preserved; any recorded upload failure makes scalar offload fail, including a failed optional upload. A required artifact that was not produced fails offload with `fix_code`; a failed upload is classified by its status or category, as above.

A failed artifact record carries `error`: the exception type, its message, its `category`, and for an HTTP response the status and the first 300 bytes of the body. A failed input's record in `inputs` carries the same `error`; for a data GET the body is read from urllib's `HTTPError`, and a size or sha256 mismatch names the planned and received values. Query strings are removed from every text field. The runner sends artifact PUTs through `http.client` and reads the response after a send error: a proxy that rejects an upload from its headers and closes the connection (Cloudflare answers a body over 100 MB with 413) is recorded as that status and body, not as the broken pipe urllib would report. When the response cannot be read after the failed send, the record's exception is `UploadCutShort`, with no status; its reason names the send error (for example `BrokenPipeError: [Errno 32] Broken pipe`) and the error from reading the response. An artifact whose URL cannot be resolved from the credential handoff records the resolution error the same way. Query strings and URL userinfo (`user:token@`) are removed from every URL in error text, including relative request targets, by `runtime_payload/redact.py`, which the runner and the local supervisor share. Each failure also writes one flushed `[runner] artifact upload failed path=... http_status=... exception=... reason=...` line to `runner.log`, and the envelope's `reason` names each failed artifact with its cause, for example `artifact offload failed: /content/out/adapter.tar (HTTP 413 Payload Too Large)`.

Every unassign goes through one function, `release_assignment`: a 404 is `already_absent` only when the assignment listing confirms the endpoint is gone; a 404 for an endpoint that is still listed, or with a listing that fails, is `cleanup: failed` with both details. Any other error is `cleanup: failed` with a hint carrying the HTTP status, the error and the start of a JSON response body. When releasing a CPU VM granted for a GPU request, or a VM whose keep-alive pre-flight failed, does not succeed, provisioning stops with the endpoint still in the envelope, so cleanup retries the release. An unconfirmed credential deletion forces teardown and adds a hint; it does not replace the workload's reason or retry class.

Every release of a job VM first copies the VM's records into the local job directory: `apply` cleanup (including after setup and bootstrap failures), `destroy`, the forced teardown after an unconfirmed credential deletion, and `status --poll` orphan cleanup. The copy reads `runner.log`, `install.log`, `result.json`, `exception.json`, `watchdog.json`, `launch.json`, `cancel.json` and both manifests, never anything under `mighty_runtime/`. Absent files are skipped. Copying stops when the session is lost, and no new read starts after 120 seconds, so an unreachable VM delays its own release by at most that budget plus one read's transport timeout. The envelope gets a hint naming what was copied and where, or why copying stopped. `install` writes each installer's output to `install.log` on the VM, so it survives a dropped kernel connection. The runner logs its terminal summary line before writing `result.json`, and the consumer runs with `PYTHONUNBUFFERED=1`, so `runner.log` is complete when the supervisor sees the result.

`destroy` on a running job writes the cancel intent and then waits up to `--wait` seconds (default 300) for the runner to stop the workload, upload artifacts and write `result.json`, polling every 5 seconds. It stops waiting early when the runner is dead, never started, or the assignment is gone. It then absorbs the result, copies the records, and unassigns. `--wait 0` releases at once. When no result arrives, a hint says why: the wait ran out, the runner is dead or never started, the assignment is gone, or reading the VM failed. A result that cannot be absorbed does not stop the release; a hint keeps the parse error and the raw verdict fields. A job with no local session is released without a copy, and a hint says the records were not copied. When the `job apply` that owns the job is still running (`job apply --async`), that supervisor absorbs the result, copies the records and releases the VM; `destroy` waits for its envelope to show a terminal cleanup and reports it without unassigning a second time. If the supervisor left the VM up, failed to release it, or did not finish within `--wait`, `destroy` releases it from the supervisor's latest envelope and adds a hint saying which.

`job status --poll` recovers an orphaned job. It identifies the original supervisor by PID, process start time, and boot identity. If that process is gone, it absorbs a complete `result.json` when present, or classifies a dead runner from `launch.json` identity plus `watchdog.json` `runner_alive`, then finishes cleanup. A live runner is left running, but not forever: for an orphaned job with a recorded launch time, `status --poll` has the same deadline as apply's default (launch + `wall_clock` + 600 s) and then cancels, waits up to 300 s for the result and releases, with the requester `job status --poll`. A plain `job status` never cancels: it answers at once and only adds a `past its deadline:` hint naming `--poll` and `destroy`. A job orphaned before launch has no launch time and no deadline, which is logged. A user's explicit apply `--timeout` is not stored, so the detached poll cannot honour it. The orphan release keeps the VM by the same rule as apply (`keep_vm`): `--leave-up` was given (stored in the envelope as `leave_up`), or an upload failed under `on_offload_fail: leave_up`. Cleanup failure preserves the remote workload verdict. Deliberate `left_up` is not auto-destroyed. A concurrently running healthy supervisor is never scrubbed.

Apply's poll and `status --poll` both watch for a watchdog that stops writing: successive reads of `watchdog.json` are compared, and when its `ts` has not changed for 5 minutes (timed with the local monotonic clock, never against the VM's) the reason and a `watchdog stalled:` hint say so; the watchdog has stopped, or cannot write its record, so whether the runner is alive is unknown. A `launch.json` without a valid identity is reported with its fields, not as a transport failure. A transport failure's reason gives its cause (`JobTransport.last_problem`, credentials removed) and what the last assignment listing showed, or that the listing itself failed. When the VM is lost, the runner is dead or never started, or a deadline cancel gets no result, apply reads `control.result` like `status` does, and a failure to read it is a hint with the signed URL redacted.

Every envelope carries the result schema version, the CLI version resolved before launch, and `runtime_payload_version`, a `sha256:` identity derived from the exact Python files shipped as `mighty_runtime`. The runner writes the same two provenance values into terminal on-VM and off-VM `result.json` records, and result absorption copies the producer values back into the local envelope. Results and envelopes that carry these fields use result schema 2; plans and other runner records remain schema 1. Result absorption is transactional: it accepts schema 1 for old records, requires both producer fields for schema 2, promotes a legacy envelope from the producer's explicit schema 2, rejects unknown result schemas, and leaves the envelope unchanged when any terminal field is invalid. Current readers accept old result-schema-1 envelopes, default a missing runtime version to an empty string, and preserve local provenance when an old remote result omits it. Envelopes also carry phase, requested/actual accelerator, ordered string hints, timestamps, and relevant result details.

Envelopes are read with unknown fields forbidden, so a CLI version older than a field cannot read an envelope that carries it. `install_attempts`, `provision_attempts` and `inputs` are left out when empty; a transfer `error` always carries `category`. `failed_phase` is written in every envelope, as null when nothing failed. `jobs list`, `jobs prune` and the MCP job listings show an envelope they cannot read as `envelope unreadable (...)` instead of failing.

The local files are:

```
~/.config/colab-cli/jobs/<id>/
  spec.json
  plan.json
  plan.json.mighty-colab-secrets.json   # when the spec has signed URLs
  envelope.json
  events.jsonl
  supervisor.json      # PID, process start time, and boot identity while apply runs
  apply.lock           # held while apply runs
  apply.log            # output of `job apply --async`
  runner.log           # copied every poll and before release
  install.log, result.json, exception.json, watchdog.json, launch.json,
  cancel.json, offload.manifest.json, stage.manifest.json
                       # copied from the VM before release, when present
```

The remote files include:

```
/content/jobs/<id>/
  mighty_runtime/
  src/
  spec.json
  plan.json
  stage.manifest.json
  offload.manifest.json
  launch.json
  watchdog.json
  runner.log
  install.log          # installer output, when deps are declared
  result.json
  exception.json       # optional
  cancel.json          # optional intent
```

The local JSON writes use atomic replacement, but the store has no cross-process lock or compare-and-swap. Event appends are plain appends.

## Testing strategy

The unit suite covers model validation, plan diagnostics without reflected inputs, redacted plan/spec persistence with owner-only hydration, canonical URL identity and credential-marker hashing, source-bundle credential rejection against immutable upload snapshots, expiry revalidation, isolated descriptor handoff and unlinking, interrupted-recovery deletion/forced teardown, healthy-supervisor race exclusion, runner exit/cancel behavior, duplicate remote launch, transport refresh, phase transitions, installer classification against captured output, CLI parsing, and envelope truth tables.

The live repro scripts under `integration/` (see `integration/README.md`) cover launch-kernel restart (`repro_job_kernel_restart`), signed-URL redaction (`repro_job_signed_url_redaction`), job-owned keep-alive (`repro_job_keep_alive`), supervisor crash recovery (`repro_job_crash_recovery`), cancel-only (`repro_job_cancel_only`), a runner that never starts (`repro_job_never_started`), a VM lost mid-run (`repro_job_vm_gone`), a run past the token expiry (`repro_job_token_boundary`), install outcomes (`repro_job_install_outcomes`), and a kernel interrupted during install (`repro_job_install_kernel_interrupted`). `integration/capture_installer_failures` records the installer output the classifier is tested against. Live runs without a repro script are listed with their dates in `chronology.md`.

These paths are covered by unit tests only: a websocket drop during install (one during `verify` is recorded in the chronology: `retry_same`, VM released), an unassign that fails while the endpoint is still listed, and escaped-descendant handling (a Linux `/proc` test of the shipped payload; no live Colab escapee run is recorded). The early-413 recovery for bodies over Cloudflare's 100 MB limit is covered by loopback TLS tests, not a live upload.

## Known gaps

- **No retry, recreate or resume.** `apply` makes one attempt and planning rejects any other retry policy; `retry_class` is advice only. A consumer that needs resume builds checkpoint chaining into its own spec generator, and a transient failure such as an account at its assignment limit needs a manual re-plan and re-apply. Tracked in [#69](https://github.com/danbarua/mighty-colab/issues/69).
- **No `control.log`.** Its presence is a plan error.
- **Spec construction is manual.** Signed URLs, sha256 locks, artifact wiring and resume chaining are written by hand or by each consumer's own generator. Tracked in [#68](https://github.com/danbarua/mighty-colab/issues/68).
- **Signed URLs in caller-owned files.** Caller-owned source specs and the owner-mode `.mighty-colab-secrets.json` sidecars contain full URLs and require credential handling.
- **Long runs.** The longest recorded runs are 96 minutes (A100) and 70 minutes (CPU, `repro_job_token_boundary`). No recorded run has crossed a second token expiry, and the recorded runs do not show which read met the expired token.
- **Keep-alive after a killed `apply`.** The keep-alive daemon can die when the local `job apply` process is killed ([#54](https://github.com/danbarua/mighty-colab/issues/54)). `job status` respawns it; nothing else does, so a job whose supervisor is killed and that nobody polls can have its idle VM reclaimed.
- **Crash window at provision.** A crash between `assign` returning and the first envelope write leaks an assignment with no local handle.
- **Optional uploads.** A failed PUT for an optional artifact fails scalar `offload`.
- **Kernel replacement.** The explicit `restart-kernel` path is verified while a detached consumer runs; a platform-initiated kernel replacement or crash is not.
- **`restart-kernel` during install.** `restart-kernel -s job-<id>` cannot reach the job's kernel while install runs: the kernel id reaches the session record only after the first execute call returns.
- **`jobs prune` does not check `apply.lock` or `supervisor.json`.** See `store-and-cleanup.md`.
