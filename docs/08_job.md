---
log:
2026-09-09: Draft architecture for an agent-facing `job` supervisor (`plan`/`apply`/`status`/`destroy`). Does not grow `run`. Remote `mighty_runtime` package; runner is parent of a shim that `runpy`s the entry; caller-supplied HTTPS URLs as the v0 data plane. Six adversarial reviews folded in; remaining `[open]` items and apply-time gaps are listed at the end. Not implemented.
2026-09-11: Spiked the launcher for real (`integration/spike_job_runner/`). Live CPU VM: kernel RPC returns in 3.9s, kernel IDLE, verdict read back via Contents API only, all nine exit/cancel/duplicate cases correct, clean teardown. Confirmed the descendant-escape hole (setsid grandchild outlived a `succeeded` verdict, invisible to the group scan). Caught two runner-cleanup bugs before implementation: `killpg` EPERM on an empty group killed the runner before it wrote `result.json` (spurious `unknown`), and an escapee inheriting the runner's stdout pipe blocks any reader keying off stream EOF. Token lifetime >1h, independent kernel restart, and GPU remain untested — the first is still the falsifier.
2026-09-11: **Corrected a wrong conclusion.** The 77min token spike lost contact at 61min and the first write-up called it "runtime reset under a live assignment" with a "no activity = reclaimed" hypothesis, citing a peer field report as corroboration. Both were wrong. Issue #3 (this repo, 2026-08-12) already documents the real cause: the runtime-proxy token has a TTL the CLI never refreshes, expiry returns **401/404** (so the "404 not 401, therefore not auth" reasoning was invalid), and it reproduces "at ~60 minute intervals" — this run failed at 61. `stop: ok` proved nothing: unassign uses the Gaia token on a different host than the Contents API's proxy token. The field report is evidence *against* the activity hypothesis — their job wrote continuously and still hit 404/401, recovering via `adopt`. Retracted "watchdog is life support"; the real consequence is that the supervisor's poll loop MUST refresh the proxy token (`list_assignments()` returns a fresh one and the CLI discards it). Next experiment replaced: re-adopt at first failure and retry the read, which discriminates in one ~65min session.
2026-09-11: **Falsifier settled by experiment, with one confound named.** `token_discriminator_spike.py` ran the same quiet workload and, at the first Contents failure (t+61min, again), re-adopted instead of concluding: the assignment was still listed, `adopt --keep-alive` returned 0, and the immediate re-read of `launch.json` succeeded. **The files were intact the whole time** — the VM was never recycled and the activity hypothesis is dead. But `adopt` mints a fresh token *and* re-resolves the proxy URL, so the run does not separate token expiry from endpoint rebinding; the script's `VERDICT=TOKEN_EXPIRY` overstates it (caught by `labkit-assistant`, who supplied the original evidence). Issue #3's documented TTL keeps expiry the leading hypothesis. The fix is robust either way: the supervisor re-resolves the assignment and rebuilds its client from the new token *and* URL. Discriminating measurement recorded in the doc for the next long run.
2026-09-11: **Implemented** (`src/colab_cli/job/`, `mighty-colab job plan|apply|status|destroy|list`). Four-field envelope with separate `done`/`ok`; stage+run+offload behind one kernel RPC so a dropped websocket cannot lose the run; `JobTransport` refreshes the proxy token once per 401/404 (rate-limited so routine "result.json not there yet" 404s don't re-resolve every poll). Live CPU runs: `done=True ok=True`, exit 0, VM released. Three defects found by running it rather than reading it — Contents PUT into a non-existent directory returns a bare HTTP 500 that the client attributes to the size limit (now `makedirs` first); the watchdog inherits `MIGHTY_JOB_ID` and reported itself as a surviving descendant on every job (now excluded); `retry.on` is unusable in a YAML spec because YAML 1.1 resolves a bare `on:` key to boolean true (renamed `retry.when`). Still untested: GPU session, independent kernel restart mid-run, and a real signed-URL data plane.
2026-09-11: **Signed-URL data plane live-verified and control backstop repaired.** A live run pulled a sha256-locked GCS input and pushed a byte-identical artifact. It also exposed that `Orchestrator.launch()` did not pass the declared `control.result.put_url` to the runner, so the off-VM result remained its `{}` placeholder while the primary envelope succeeded. Added the missing runner option, retained `cli_version` on the local job envelope, documented GCS's create-before-signing-GET order, and reran a live CPU job: the control object contained `workload: succeeded` / `exit_code: 0`, apply reported `done=true` / `ok=true`, cleanup released the VM, and no session remained.
2026-09-11: **Launch-kernel restart live-verified.** The first attempt exposed that the job session record omitted the active kernel and session identifiers, so the public `restart-kernel` command could not target the kernel that launched the runner; cleanup also left the local kernel client open after the terminal envelope. Both are fixed. `integration/repro_job_kernel_restart/test.sh` now launches a detached CPU workload, restarts its recorded launch kernel through the public command, proves the consumer's process identity is unchanged while progress advances, observes `workload: succeeded` / exit 0, releases the assignment, and verifies the endpoint is absent.
2026-09-11: Audited the implemented contract against source, executable help, tests, and five adversarial domain reviews. Corrected the public CLI and schema, current phase and persistence behavior, plan-hash scope, transport boundaries, signed-URL handling, and supervisor recovery claims. Added the load-bearing implementation gaps that remain.
2026-09-11: Addressed PR #15 peer review: external cancel intent now reaches runner and watchdog, destroy preserves remote verdicts, SHA-256 values are validated, unexpected supervisor exceptions terminalize, control PUT credentials are staged outside kernel history, unsupported policy values fail planning, status absorbs complete remote results, and declared artifact sizes count in the disk gate. Source-byte locking remains tracked by [#20](https://github.com/danbarua/mighty-colab/issues/20).
2026-09-11: **Cancel-only live-verified.** `integration/repro_job_cancel_only/test.sh` observed a running CPU workload, issued the public `destroy --cancel-only`, observed a terminal `workload: cancelled` result while the assignment remained listed, then performed full destroy and verified the endpoint disappeared.
2026-09-11: Fixed and live-verified [#18](https://github.com/danbarua/mighty-colab/issues/18): generated specs, plans, manifests, envelopes, events, diagnostics, and kernel launch history now contain only canonical URL identities and credential references. Full URLs remain only in caller-owned source specs, owner-mode local sidecars, and a short-lived owner-mode VM handoff that the isolated runner inherits by file descriptor and unlinks before consumer launch. Missing required handoffs fail closed; interrupted recovery scrubs or tears down; source bundles reject credential-bearing URLs from immutable snapshots. A live CPU job proved the signed data URL was consumed while the sentinel was absent from output, durable local records, remote files, and kernel history, then released the assignment.
2026-09-11: Fixed and live-verified [#17](https://github.com/danbarua/mighty-colab/issues/17): `job apply` now starts the TFE keep-alive daemon after persisting the job session, propagates auth and `--config`, records daemon pid and last ping, stops the daemon on release or confirmed absence, and leaves it running on `--leave-up`.
2026-09-11: Fixed and live-verified [#16](https://github.com/danbarua/mighty-colab/issues/16): provision persists the endpoint before keep-alive; a dead supervisor's `status --poll` absorbs complete runner results or classifies a dead runner from launch/watchdog identity, then finishes cleanup without overwriting a remote verdict.
2026-09-11: Fixed [#20](https://github.com/danbarua/mighty-colab/issues/20): plans record each source file's relative path, size, and SHA-256; apply refuses added, removed, renamed, or changed files before assignment and stages only locked bytes. Signed-URL query canonicalization is unchanged.
2026-09-11: Fixed [#19](https://github.com/danbarua/mighty-colab/issues/19): apply claims a job ID with an exclusive lock before assignment; a live second owner fails before `assign`; a dead owner's lock is taken over; a job that already has an endpoint is refused.
2026-09-11: Fixed [#22](https://github.com/danbarua/mighty-colab/issues/22): apply stages, polls, cancels, and recovers through one JobTransport; Contents requests and assignment re-resolution use connect/read deadlines; kernel restart has an explicit timeout; timed-out writes confirm before retry; exhausted transport stalls stay degraded unless the assignment is proven gone.
2026-09-11: Fixed [#25](https://github.com/danbarua/mighty-colab/issues/25): untrusted job URLs are resolved; any non-public IPv4/IPv6 answer is rejected, including mixed DNS; each request connects to an address from that lookup; redirects are re-checked; HTTPS remains required.
2026-09-12: Fixed [#27](https://github.com/danbarua/mighty-colab/issues/27): GCS control-result PUT/GET URLs must identify one object; status and destroy use the GET URL as a bounded terminal-result fallback when the VM result is unavailable. Unsupported retry, resume, control-log, and run-failure policy values remain plan errors.




---

# Design: `job` — Agent job supervisor

**Implemented and live-verified in the paths identified below** (2026-09-11). `mighty-colab job plan|apply|status|destroy|list` ships in `src/colab_cli/job/`; usage lives in `docs/09_job_usage.md`. This document describes the current implementation and names its gaps. Long-run evidence proves that the VM, assignment, and files survived the first Contents failure at about one hour; `JobTransport` refreshing assignment metadata restored access. The probe did not distinguish bearer-token expiry from proxy endpoint rebinding. A multi-hour GPU job through that refresh remains untested. Job provision now owns the TFE keep-alive daemon used by `colab new`.

`run` stays the shebang (`new` + text-into-kernel + `stop`). `job` is the unit of work an unattended agent actually has: code, deps, data, artifacts, accelerator policy, two clocks, teardown.

## Motivation

An agent composing `new` → `reinstall` → `exec-async` → `log --tail` → `stop` rediscovers the same wounds every time (`docs/AGENT_USABILITY_LEARNINGS.md`): output-gap `--timeout`, text-not-a-file `__file__`, Jupyter upload ceilings, interactive VM auth, teardown skipped on a failing `exec`, exit 0 with no verdict.

Those steps have a shape. The shape is a state machine. The machine belongs in code, not in a skill.

## Non-goals

- Notebooks, Drive, `colab auth`, SSH-over-WSS.
- Growing `run` / changing upstream command flags.
- Terraform reconciliation (loop apply until the world matches). Steal plan/apply/destroy ergonomics only.
- Framework instrumentation (PyTorch hooks, JAX callbacks). Workloads here are hand-rolled JAX more often than not.
- Consumer `import mighty_runtime` as a requirement. v0 has no stall kill, so no `pulse()` either.
- CLI-side GCS signed URL minting from ordinary user ADC (no private key; `signBlob` needs a service account).
- A DAG of jobs. GCS/HTTP is the queue between jobs.
- Mid-run `install`/`reinstall` issued by apply itself. The explicit public `restart-kernel` path is live-verified; a platform-initiated kernel replacement or crash remains unverified.

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

`mighty_runtime` is a Python package we place on the VM disk. It is not a Jupyter plugin. The kernel is remote Python that can spawn Python. Transport (websocket, Contents API) does not leak into the agent contract or the skill.

## User surface

| command | effects | analogue |
|---|---|---|
| `job plan SPEC_FILE [--out PATH] [--no-probe]` | writes local records and optionally probes data URLs; no VM | `terraform plan -out` |
| `job apply [PLAN_FILE] [--job-id ID] [--timeout S] [--leave-up]` | allocates and drives a VM | `terraform apply plan` |
| `job status JOB_ID [--poll] [--interval S]` | reads the local record and, when possible, the VM result | refresh |
| `job destroy JOB_ID [--cancel-only]` | cancels and/or unassigns | `terraform destroy` |
| `job list` | lists local job records | local inventory |

The MCP server exposes `job_plan`, `job_status`, `job_destroy`, and `job_list`. It deliberately excludes the blocking `job_apply` command.

`provision` is a phase of apply, not another command.

Apply consumes a plan file directly or retrieves one by `--job-id`. Generated plans replace credential-bearing URL queries with canonical identities plus markers; apply hydrates them from the adjacent owner-only sidecar before allocation. It verifies the plan hash, including source-spec path, credential markers, and the source-file lock (relative path, size, SHA-256). Added, removed, renamed, or changed source files fail before assignment. Staging uploads only files covered by that lock.

## Spec (v0)

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

Optional `data[]` entries contain `url`, `dest`, `sha256`, and `size_bytes`. Optional `artifacts[]` entries contain `path`, `url`, `required`, and `size_bytes`. `control.result` and `control.log` each accept paired `put_url` and `get_url` values. `code.kind` is only `file` or `bundle`; there is no `git`, `checkpoints`, or `credentials` field.

For a GCS-backed `control.result`, first sign PUT, PUT a fresh `{}` placeholder using `Content-Type: application/octet-stream`, and then sign GET for the same unique object. The runner replaces the placeholder with its terminal result. `{}` is not a verdict. When the VM result is unavailable, `status` and `destroy` read the GET URL as a bounded fallback and absorb only a terminal result.

Signed query strings are credentials. Generated `spec.json`, `plan.json`, explicit `--out` plans, remote manifests, envelopes, events, and validation diagnostics expose only canonical URL identities and opaque references. Full URLs remain in the caller-owned source spec and an adjacent owner-mode `.mighty-colab-secrets.json` sidecar. Apply sends them to an owner-mode remote handoff only after all public files; the launch kernel opens and unlinks it, then passes the inherited descriptor to an isolated runner. The runner clears inherited URL variables before consumer launch. Recovery confirms deletion or forcibly releases the assignment.

This boundary prevents durable disclosure, diagnostic reflection, and ordinary launch-time inheritance by the consumer. It does not defend against hostile same-UID code that runs before credential upload: a dependency install hook, `.pth` file, or existing process can persist and inspect the later handoff or launch process through `/proc`. Requirements, their build/install hooks, and the single-user job VM are therefore trusted inputs. Dependency installation completes before credential upload, and `-I -S` prevents accidental installed-package imports in the runner bootstrap; neither mechanism is a privilege boundary against malicious dependencies.

`apply` runs exactly one attempt. The only executable policy values are `retry.when: [retry_same]`, `max_attempts: 1`, `mode: recreate`, `on_run_fail: offload_anyway`, and no `control.log`; planning rejects other values instead of accepting inactive behavior. `retry.budget_seconds` remains active for control-URL expiry validation. There is no retry, resume, checkpoint, or control-log implementation.

Relative data destinations and artifact paths resolve under `/content/jobs/<id>`. Absolute paths are accepted only after canonical resolution below `/content`; launcher-owned paths below the job directory are reserved.

### Budgets

`wall_clock` is enforced by the watchdog. On breach it writes intent, sends SIGTERM to the shim's process group, waits through the grace period, and escalates to SIGKILL. `retry.budget_seconds` currently controls control-URL expiry validation only; it does not bound an implemented retry loop.

v0 does not hard-kill on stall. `exec --timeout` already taught us that "stdout went quiet" murders healthy JAX/XLA. The consumer does not import `mighty_runtime`, so there is no zero-cooperation progress signal. The watchdog reports telemetry and inactivity; `wall_clock` is the only zero-cooperation kill.

## Remote process tree

The durable workload is a runner process, not the launch kernel:

```
kernel launch RPC
  └─ python -I -S -c <isolated runner bootstrap, inherited secret fd>
       └─ python -m mighty_runtime.shim <entry>
watchdog process (sibling)
```

Before launch, the local stage phase uploads `mighty_runtime`, user code, and query-free manifests through the Contents API. `kind: file` uploads only the entry file. `kind: bundle` walks the root and uploads files individually, excluding `.git`, `.venv`, `__pycache__`, `.pyc`, the active source spec, secret sidecars, and reserved atomic-secret temporaries. Each user file is copied once from an `O_NOFOLLOW` descriptor into an immutable local snapshot; that same snapshot is scanned and uploaded, closing the scan/upload race. Arbitrary content is rejected when an HTTP(S) query uses a recognized credential key. YAML-shaped mappings are parsed regardless of filename extension and reject any query-bearing value in `url` or `*_url` fields, covering custom signers while allowing ordinary query URLs in source code.

`Orchestrator.launch()` calls `ColabRuntime.execute_code(..., timeout=120)`. When the plan declares any transfer URL, sealing and launch both require the private handoff; absence fails before a consumer starts, and the runner independently enforces `--secrets-required`. The kernel opens and unlinks the handoff, then starts the runner with `start_new_session=True`, `python -I -S -c`, and an inherited descriptor rather than an argv/environment URL. Passing no output hook does not create a separate non-interactive protocol: the vendored client still uses its interactive execution loop internally. The 120-second limit covers the execute reply; kernel HTTP/WebSocket startup retains its shorter defaults. Kernel restart itself currently has no explicit deadline.

The runner creates `launch.json` with `O_EXCL`, starts the shim in its own session/process group, and remains its parent so it can `waitpid()`. The shim sets the entry's real `sys.argv`, `__file__`, and `sys.path[0]`, then uses `runpy.run_path(..., run_name="__main__")`. A duplicate runner sees the existing live launch identity and exits without starting a second consumer; the launch RPC does not promise to return the original runner PID.

The runner maps normal exits, exceptions, signals, cancellation intent, wall-clock expiry, and descendant-survival checks into `result.json`. Both runner and watchdog consume an externally written `cancel.json`, send SIGTERM, and escalate after the grace period; `destroy --cancel-only` writes that intent without unassigning. The runner then attempts declared artifact PUTs and, when configured, `control.result.put_url`. An optional artifact that is absent does not fail offload, but any artifact PUT recorded as `failed` currently makes scalar `offload: failed`, irrespective of `required`.

The watchdog is a sibling process. It enforces wall clock and reports telemetry. Job provision starts the TFE keep-alive daemon after persisting the session, the same daemon `colab new` uses; cleanup stops it on release or confirmed absence and leaves it running when the VM is deliberately left up.

The local apply supervisor polls `result.json` and `watchdog.json`. `job status` additionally reads `launch.json` identity and `watchdog.json` `runner_alive` so a dead runner without `result.json` becomes `workload: unknown`. Stage, poll, cancel, and recovery share one `JobTransport`: Contents requests carry connect/read deadlines, assignment re-resolution is bounded by the same timeout, and a timed-out write is confirmed before retry. Exhausted transport stalls stay `degraded` unless the assignment is proven gone.

`waitpid()` cannot always provide a Python exception. `os._exit()`, SIGKILL/OOM, and native crashes can produce `workload: failed` with exit/signal information and no exception.

## Data plane

v0 accepts caller-supplied HTTPS GET/PUT URLs. The VM uses `urllib`; it has no GCS client or service-account-key mode.

`plan` uses a one-byte ranged GET only for `data[]` URLs. It does not issue HEAD and does not mutate artifact or control destinations. It parses recognizable signature expiry fields on all URL fields. Data and artifact URLs must cover `wall_clock + 15 minutes`; control URLs must cover `retry.budget_seconds + 15 minutes`. For GCS control channels, planning also requires the paired PUT and GET URLs to identify the same bucket and object.


The public-host check resolves DNS and rejects a destination unless every IPv4 and IPv6 answer is global unicast. Mixed public/non-public answers fail closed. Each Contents-independent GET/PUT connects to an address from that lookup with the original hostname as SNI/Host, so DNS cannot be rebound between check and connect. Redirect targets are resolved and checked the same way. HTTPS remains required.

Each staged data item can carry `size_bytes` and an exact 64-hex-character `sha256`; the model normalizes the digest to lowercase and the runner verifies both fields when present. Declared data and artifact sizes both contribute to the free-disk refusal. Each artifact result records status, hash, and byte count when available. PUT failures are not retried automatically. A 403 caused by an expired signature may be labelled `refresh_urls` by some classified paths, but not every phase failure currently receives a `retry_class`.

## Plan

`job plan` never calls `assign`, but it is not side-effect-free: it writes redacted `spec.json` and `plan.json` records below the job store, writes a redacted `--out` plan when requested, creates adjacent owner-mode secret sidecars when query credentials exist, and performs ranged GET probes unless `--no-probe` is set. It writes generated records even when diagnostics contain warnings or errors.

Errors make `plan` exit non-zero and make `apply` refuse the saved plan. Warnings make apply refuse unless the embedded spec has `ignore_warnings: true`. The current planner checks accelerator names, code-entry containment/existence, destination containment below `/content`, reserved/colliding paths, HTTPS and recognized literal private hosts, signed-URL expiry, paired GCS control-object identity, and data ranged GETs. It does not implement several earlier design gates: aggregate bundle/data size, dependency resolution, ADC scope validation, file-mode sibling-import analysis, non-GCS PUT/GET object equivalence, or artifact/control mutation probes.

The job ID is `<name>-<UTC timestamp>-<six random hex characters>`. `spec_hash` on the stored plan is `plan_hash`: canonical modeled spec (URL identities and credential-presence markers), source-spec path, and the source-file lock. Re-signing the same object does not change the spec identity. Apply hydrates from the owner-only sidecar, revalidates URL expiry, the plan hash, and the on-disk source bytes before assignment.

## Apply phases

The implemented phase order is:

```
plan.json
  -> provision   assign; persist endpoint/session in envelope; persist the session; start keep-alive
  -> install     install pinned dependencies
  -> restart     restart the launch kernel
  -> verify      probe dependencies, device, and declared input disk need
  -> stage       upload runtime, source files, and stage/offload manifests
  -> run         launch runner/watchdog; runner performs data GET, consumer run,
                 artifact PUT, and optional control-result PUT
  -> offload     absorb the runner's artifact results locally
  -> cleanup     unassign, report already absent, or leave up
```

`install` precedes `stage`, so a bad dependency pin fails before source upload and disk is measured after installation. Data GET is executed by the remote runner during `run`, not by the local stage phase.

The explicit public `restart-kernel` path is live-verified while a detached consumer runs. A platform-initiated replacement/crash is still unverified. Apply's own restart POST uses an explicit 60s timeout and classifies a stall as `retry_same`.

Apply tries one attempt. `RetryClass` is advice for the next caller action, not an automatic retry engine. Planning rejects non-default `retry.when`, `max_attempts`, and `mode` values until retry/recreate/resume exist. Some errors are classified (`fix_code`, `fix_human`, `retry_same`, `retry_different`, `refresh_urls`, `do_not_retry`); cancellation, offload failure, and cleanup failure do not all receive the earlier table's promised class.

Unexpected exceptions are caught unless `--debug` is active. The supervisor persists and emits a terminal envelope: pre-run failures become `workload: failed`; failures during or after run without a remote verdict become `unknown`; terminal remote verdicts are preserved. The endpoint is persisted in the envelope before keep-alive starts; a crash between `assign` returning and that write can still leak an assignment. Apply claims an exclusive lock on the job ID before assignment; a live second owner fails before `assign`, a dead owner is taken over, and a job that already has an endpoint is refused.

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

`not_required` means the spec declared no artifacts. `skipped` is a terminal schema value but the current runner normally attempts declared artifacts even after failure. Per-artifact results are preserved; any recorded upload failure currently makes scalar offload fail, including a failed optional upload.

`job status --poll` recovers an orphaned job. It identifies the original supervisor by PID, process start time, and boot identity. If that process is gone, it absorbs a complete `result.json` when present, or classifies a dead runner from `launch.json` identity plus `watchdog.json` `runner_alive`, then finishes cleanup. A live runner is left running. Cleanup failure preserves the remote workload verdict. Deliberate `left_up` is not auto-destroyed. A concurrently running healthy supervisor is never scrubbed.

Every envelope carries schema/CLI versions, phase, requested/actual accelerator, ordered string hints, timestamps, and relevant result details. The local files are:

```
~/.config/colab-cli/jobs/<id>/
  spec.json
  plan.json
  envelope.json
  events.jsonl
  supervisor.json      # PID, process start time, and boot identity while apply runs
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
  result.json
  exception.json       # optional
  cancel.json          # optional intent
```

The local JSON writes use atomic replacement, but the store has no cross-process lock or compare-and-swap. Event appends are plain appends.

## Testing strategy

The permanent suite covers model validation, plan diagnostics without reflected inputs, redacted plan/spec persistence with owner-only hydration, canonical URL identity and credential-marker hashing, source-bundle credential rejection against immutable upload snapshots, expiry revalidation, isolated descriptor handoff and unlinking, interrupted-recovery deletion/forced teardown, healthy-supervisor race exclusion, runner exit/cancel behavior, duplicate remote launch, transport refresh, phase transitions, CLI parsing, and envelope truth tables. Live integrations cover CPU and T4 jobs, signed GCS data/artifact/control-result paths, dependency restart/verify, workload failure, token refresh recovery, explicit launch-kernel restart, cancel-only termination with assignment retention, and job-owned TFE keep-alive through idle leave-up and destroy.

The current gaps need regression coverage before their claims can be promoted: streamed transfer; aggregate bundle limits; optional-upload semantics; control log; and a shipped-path setsid escapee case.

## Spike results (2026-09-11, live CPU VM)

Prototype in `integration/spike_job_runner/`. Local known-answer harness
(`local_check.py`, 12 cases) plus a live run (`live_spike.py`) against a real
CPU session, endpoint `m-s-kkb-euw4b2-2meuc156jbvr8`, 163s wall, torn down
clean with no orphans.

**The core question is answered: the loop works.** A short kernel RPC started a
detached runner and returned in **3.9s**; the kernel reported IDLE while the
workload ran; every verdict below was read back **through the Contents API
only**, never `execute_code`.

| claim | result |
|---|---|
| launch RPC returns fast, kernel IDLE | 3.9s, IDLE |
| `succeeded` / exit 0 | ok |
| raised exception → `failed` + `exception.json` | ok |
| `os._exit(7)` → `failed` exit 7, **no** `exception.json` | ok |
| `SIGKILL` → `failed` signal 9, **not** `cancelled` | ok |
| clean `sys.exit(0)` → `succeeded`, no `exception.json` | ok |
| sibling import via `sys.path[0]` | ok |
| `wall_clock` breach → `cancelled` + intent, signal 15 | ok (SIGTERM sufficed; no escalation needed) |
| duplicate launch refused via `O_EXCL launch.json` | ok |

**Confirmed hole, and a fix for half of it:** a `setsid` grandchild outlived a
`succeeded` verdict on the VM and was **invisible to the process-group scan** —
reproduced locally and live. The process group is provably not a containment
boundary.

The shipped Linux job-tag sweep has now executed live. It initially exposed that the watchdog inherited `MIGHTY_JOB_ID` and falsely appeared as a surviving descendant; the watchdog is now excluded. The sweep reports process-group and job-tag survivors separately. A dedicated setsid escapee case through the shipped integration path has not yet been recorded.

**Killing** the escapee is still open: detection is not containment. cgroup
`cgroup.kill` or `PR_SET_CHILD_SUBREAPER` remains required to actually reap
one, and the runner must refuse to report a clean terminal state while a
tagged survivor exists.

**Two bugs the spike caught before implementation**, both in the runner's own
cleanup rather than in the workload:
- `killpg` on an already-empty group returns `EPERM` on BSD, not `ESRCH`.
  Catching only `ProcessLookupError` killed the runner *in its kill path*,
  leaving `launch.json` + `cancel.json` and **no `result.json`** — a spurious
  `unknown`, the one terminal value an agent cannot act on. Fixed: any `OSError`
  means "nothing left to signal", SIGKILL only escalates if SIGTERM did not
  work, and the whole wait loop is wrapped so the verdict always lands.
- An escaped descendant **inherits the runner's stdout pipe**, so a reader
  waiting on stream EOF hangs long after the verdict exists. Reinforces that
  `done` must come from `result.json`, never from "the stream closed".

**Long-run result (2026-09-11, 77min CPU session): the job was lost at ~61min.**

`token_lifetime_spike.py`, quiet workload, polled every 5min:

| t | Contents read of `launch.json` |
|---|---|
| 5 → 56 min | OK (12 consecutive polls) |
| 61 min | **404 File or directory not found** |
| 66, 71, 76 min | 404 / connection aborted |
| end | `result.json` never readable; verdict lost |

**It was almost certainly proxy-token expiry, and the first write-up of this
result (including its "activity keeps the VM alive" hypothesis) was wrong.**
Corrected after `danbarua/mighty-colab` issue #3 was re-read:

- Issue #3, filed 2026-08-12, documents this exact failure: `RuntimeProxyInfo.token`
  has a TTL (`tokenExpiresInSeconds`) the CLI parses and then **never reads or
  refreshes**. On expiry "the next `exec`/`run` gets a **401/404** from the proxy."
  Observed "reproducibly at **~60 minute intervals** on long-running jobs."
  This run failed at **61 minutes**.
- The original reasoning — *"404, not 401, therefore not auth"* — does not hold.
  The proxy returns either. That distinction carried the whole argument and it
  was never valid.
- `stop: ok` proved nothing about the runtime. `unassign` goes to
  `colab.research.google.com` with the **Gaia bearer token**; the Contents API
  goes through the **runtime proxy** with a *different*, expiring token. An
  expired proxy token and a live assignment are exactly what issue #3 describes,
  not a contradiction to explain away.
- So `/content` most likely never vanished. The files were probably intact the
  whole time and simply unreadable through an expired credential.

**Confirmed by direct experiment (2026-09-11, `token_discriminator_spike.py`).**
The inference above is no longer an inference. A second CPU session ran the
same quiet workload and, at the first Contents failure, re-adopted instead of
concluding:

| t | event |
|---|---|
| 0 → 56 min | 12 consecutive `launch.json` reads OK |
| 61 min | **404** on `launch.json` — a file written at t=0 |
| 61 min | assignment **still listed** by `list_assignments()` |
| 61 min | `adopt <ENDPOINT> --keep-alive` → rc=0 |
| 61 min | **immediate re-read: OK** |

**What this establishes, and what it does not.** Established: the files were
intact all along. `/content` never vanished, the VM was never recycled, and a
credential/binding refresh restored access to the *same* endpoint. That kills
the activity hypothesis outright — the discriminating variable was never
write-activity, and the originally-planned 75-minute A/B would have measured
nothing.

**Not established: which half of `adopt` fixed it.** Flagged by
`labkit-assistant`, who supplied the original evidence and did not want to hand
over a second wrong conclusion. `adopt` does two things at once — it mints a
fresh proxy **token** and re-resolves the assignment's proxy **URL**. Token
expiry and endpoint rebinding predict identical observations here, so the
original script's `VERDICT=TOKEN_EXPIRY` label overstates what the run measured; the current script reports `VERDICT=ACCESS_BINDING_REFRESH`. The
endpoint *id* was unchanged (we re-adopted the same one), which rules out
reassignment to a different VM, but not a changed proxy URL.

Issue #3's documented TTL makes token expiry the better-supported reading, and
61 minutes on both runs matches its ~60-minute interval. It remains the leading
hypothesis, not a measurement.

**Why the fix is correct either way:** `JobTransport` rebuilds its
`ContentsClient` from a freshly-resolved assignment, taking the new token *and*
the new URL. It is refresh-and-retry and re-resolve-and-retry in one step, so
both mechanisms are covered. The open question is explanatory, not operational.

**The discriminating measurement, for whoever runs the next long job:** at the
first failure, before re-adopting, capture the stored `(token, url)` and the
pair returned by a fresh `list_assignments()`. Token differs and URL identical
→ expiry. URL differs → rebinding. Retrying the failing read with the old token
against the new URL (and vice versa) separates them outright. Cost: one GET on
a session you already hold.

**The field report is evidence *against* the activity hypothesis, not for it.**
The earlier revision of this section cited it backwards. Their job wrote
continuously to `/content/job.log` and **still** hit a 404/401 that pruned the
local record — writes did not prevent it. What differed is that they *recovered*:
`adopt <ENDPOINT> --keep-alive` restored access with the VM and job intact. This
spike never tried `adopt`; it polled with a stale token and concluded the
filesystem was gone.

What survives from the original conclusions:

1. **The durable off-VM push is demoted to defence-in-depth.** It guards a
   verdict against losing *read access*, not against a vanishing filesystem —
   and token refresh addresses that cause directly and more cheaply. Keep
   `control.result.put_url` (a verdict in a second place is still worth having
   when the VM is genuinely gone), but it is no longer "the only place a
   multi-hour verdict can safely live," and it must not be used to paper over a
   missing refresh.
2. **"The watchdog is life support"** — **retracted.** Unsupported by this run
   and contradicted by the field report, whose continuously-writing job hit the
   same 401/404. The watchdog remains justified as observability; it is not
   established as a survival requirement.

**The real design consequence** is narrower and more actionable: the supervisor's
polling loop **MUST refresh the runtime proxy token** rather than treating a
401/404 as terminal. `list_assignments()` already returns a fresh token on every
call and the CLI discards it (issue #3). A multi-hour `job` that does not refresh
will lose contact with a perfectly healthy VM at the one-hour mark, every time.
This also reclassifies the `transport_degraded` vs `session_lost` question from
"nice to have" to load-bearing.

- **Independent kernel restart. Verified 2026-09-11** against the shipped
  command path in `integration/repro_job_kernel_restart/`: `job` persisted
  the launch kernel identity, public `restart-kernel` restarted that kernel,
  the detached consumer retained the same PID/PPID/session/start identity and
  continued writing progress, and the terminal envelope reported
  `workload: succeeded` / exit 0. Explicit destroy released the assignment;
  the endpoint was absent from the final session listing.
- **GPU session.** **Verified 2026-09-11** against the shipped implementation:
  requested `T4`, granted `T4`, `verify` gate passed, the workload ran a real
  `torch` CUDA matmul and exited 0, watchdog reported live telemetry
  (`Tesla T4, 15360, 14910, 0`), VM released. What remains untested on GPU is a
  *long* run — everything so far finishes inside the token's first hour.
- **Failure path.** **Verified 2026-09-11**: a `KeyError` in the workload
  surfaced off-VM as `workload: failed` / `exit 1` /
  `exception: KeyError: 'missing_key'` / `retry_class: fix_code`, with
  `cleanup: released` and `apply` exiting 1. Teardown ran despite the failure.
- The spike prototype in `integration/spike_job_runner/` has no watchdog
  process (its runner enforces `wall_clock` directly) and no data plane. The
  shipped implementation's signed GCS data GET and artifact PUT were verified
  live, including sha256 input validation and byte-identical artifact recovery.
  A second live CPU run verified `control.result.put_url`: the runner replaced a
  pre-created `{}` object with its terminal `workload: succeeded` / `exit_code:
  0` result, while apply reported cleanup released and no session remained.

## Known gaps

These are current implementation limits, not hypothetical polish:

- **Idle retention:** job provision owns the TFE keep-alive daemon. A multi-hour GPU run through the proxy refresh boundary has not been completed.
- **Crash recovery:** there is still a short window between `assign` returning and the first envelope persist. Apply's own poll loop does not classify a dead remote runner from `launch.json`; `status --poll` does.

- **Memory and size:** data GET and artifact PUT buffer whole objects in RAM. The 250 MB source limit is per file, enforced after allocation; there is no aggregate bundle ceiling.
- **Signed secrets:** generated specs, plans, manifests, envelopes, events, diagnostics, and kernel launch history contain query-free URL identities and opaque credential references only. Caller-owned source specs and generated owner-mode `.mighty-colab-secrets.json` sidecars still contain full URLs and require credential handling.
- **Declared but inactive controls:** planning rejects non-default retry/recreate/resume settings, `control.log`, and `on_run_fail: skip`.
- **Process containment:** tagged escapees are reported, not killed. The dedicated shipped-path setsid case remains unverified.
- **CLI/JSON consistency:** the job-group help summary omits `list`. Some early file/plan read failures still emit stderr rather than a JSON envelope. A failed apply can exit the process with status 1 while its outer JSON wrapper says `exit_code: 0`.
- **Remote provenance:** the runner's off-VM control result does not carry `cli_version`; only the local job envelope does.
