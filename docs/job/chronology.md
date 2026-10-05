# Job chronology

Dated changes to `job` and findings about how it behaves on Colab, newest
first. Each entry states what changed or what was learned and cites its
evidence. Evidence, strongest first: a repro script under `integration/`,
shipped code (a PR or commit), a recorded live run. `PR #N` is a pull
request and `issue #N` an issue in `danbarua/mighty-colab`.

The other documents in `docs/job/` describe the current system. This one
records what changed and what each claim rests on.

### 2026-10-05: A missing output no longer keeps the VM billing

A required artifact that was never produced (the run crashed, was OOM- or
`wall_clock`-killed, or wrote the file elsewhere) fails offload, and the
default `on_offload_fail: leave_up` kept the VM up and billing although
there was nothing on it to rescue. `leave_up` now keeps the VM only after an
attempted upload failed. `cleanup` also stopped recomputing the decision on
its own, which could keep a VM up after apply had overridden `leave_up`
because the transfer credential's deletion was not confirmed.

Evidence: unit tests in `tests/test_job_orchestrator.py`.

### 2026-10-05: Plan, apply's preflight and provision say why they refuse; URL expiry counts install

Plan reported a host that does not resolve as a private address, flagged
only 403 and 404 from the data probe (as `fix_human`, with no body),
discarded the size the probe measured, and turned an unbuildable source lock
into an empty one that apply then refused with "re-run job plan". A spec or
plan that would not load gave only an exception type, and `apply --json`
refusals carried no diagnostics. Provision kept each failed accelerator as
`str(e)[:200]`, which included the assign URL's query, dropped earlier
failures when a later candidate was granted, and called every failure
`retry_different`. Now each of these names its cause: `url_host_unresolved`;
every probe result classified by the transfer table with the response body;
`data_size_mismatch` and a measured size used for disk planning;
`source_unreadable` naming the file; the YAML problem and position; the
invalid plan field without its value; refusal envelopes with diagnostics;
`provision_attempts` with each assign classified by status.

URL expiry was checked against `wall_clock` + 15 minutes before
provisioning, though install can take 55 minutes first. With `deps`, plan
and apply now add that allowance, `verify` checks again after install, and
control URLs must cover the data deadline as well as `retry.budget_seconds`.

Evidence: `integration/repro_job_plan_diagnostics` (real URLs, no VM: a
size mismatch, a measured size, a 404 and a GCS 403 `SignatureDoesNotMatch`
with their bodies, an unresolvable host, an expired signature, a symlink in
a bundle, the install allowance). Its first run found that apply checked
the source lock before the plan's own errors, hiding `source_unreadable`
behind "plan has no source lock"; fixed in the same change. Provision
classification is covered by unit tests built from the assign failure
recorded on 2026-10-04; there is no on-demand trigger for a failed assign.

### 2026-10-04: Finding: a websocket drop during `verify`, seen live, is `retry_same` and releases the VM

In a run of `repro_job_timeout_and_interrupt`, the kernel websocket dropped
15 seconds after provisioning (the websocket client logged `'NoneType'
object has no attribute 'sock' - goodbye`). `apply` recorded `failed_phase:
verify`, `retry_class: retry_same` and the reason `kernel connection failed
during verify: RuntimeError: Connection was lost.`, and released the VM.
This is the first recorded occurrence of the transport-failure path that PR
#76 covers with unit tests; there is still no on-demand trigger for it.

Evidence: a recorded live run on 2026-10-04.

### 2026-10-04: Every runner-side failure says what happened, and its retry class follows from it

A `wall_clock` kill reported `signal 15` with `reason: null`: nothing read the
result's `cancel_intent` or `runner_error`. An unrequested SIGKILL, a plain
exception and a runner fault had no reason either. Every staging failure was
`fix_human` with a generic hint, because the runner kept only a category word
for the failed input, and every upload failure was `retry_same`. Now
`colab_cli/job/verdict.py` derives both from the result: a `wall_clock` kill
names the budget and is `fix_code`; a signal nobody requested is `fix_code`
with the kernel's OOM evidence; an exception gives its module-qualified type
and message; a transfer is classified by HTTP status (401/403
`refresh_urls`, 404 `fix_code` on a GET and `refresh_urls` on a PUT,
408/429/5xx `retry_same`, other 4xx `fix_code`) or, with no response, by the
runner's category. The runner records each staged input in `inputs`, a
category, status and redacted body for every failed transfer, the signal's
name, and non-fatal problems as `runner_warnings`. The shim keeps the head of
a long traceback as well as its tail. The watchdog says why it has no GPU
reading, and reports liveness as unknown, not dead, when `launch.json` is
unreadable.

Evidence: `integration/repro_job_runtime_detail`;
`integration/repro_job_timeout_and_interrupt` (case 4, which printed
`cancelled signal 15 | None` before).

### 2026-10-04: Finding: an OOM kill on Colab shows in `/proc/vmstat`, not in the job's cgroup

On a CPU VM (13 GB, no swap) the Jupyter kernel and its children are in cgroup v2
`/../../jupyter-children`, whose `memory.max` is `max`. A process
allocating 256 MiB at a time was killed with SIGKILL at about 11 GB resident:
`memory.events` `oom_kill` stayed 0 and `/proc/vmstat` `oom_kill` went from
0 to 1. `dmesg` is readable and logged `Memory cgroup out of memory: Killed
process <pid> (python3) ... anon-rss:11671412kB`, so the limit that fired
belongs to an enclosing cgroup. The runner therefore counts OOM kills from
`/proc/vmstat` and quotes the kernel's line.

Evidence: `integration/repro_job_runtime_detail` (case 2); a probe run on
2026-10-04.

### 2026-10-04: A local supervisor that stops early no longer leaves the VM billing

`apply --timeout` passing with no verdict used to record `left_up` and leave
the VM billing until `job destroy`; so did Ctrl-C, even before launch, with
the false reason "the VM job is unaffected" and a "reattach with `job
status`" hint for a command that skips `left_up` jobs. Now `--timeout`
cancels the runner, keeps its result and releases the VM; Ctrl-C before
launch releases the VM; Ctrl-C after launch leaves cleanup pending so `job
status --poll` collects the result and releases it. SIGTERM (an agent
harness ending a long tool call) and SIGHUP, whose Python default exits with
no cleanup, release the VM before launch; after launch `apply` hands the job
to a detached `job status --poll`, which releases the VM when the job ends,
because whoever started it may not come back.

Evidence: `integration/repro_job_timeout_and_interrupt`.

### 2026-10-04: Finding: the watchdog killed its own runner on a cancel or at the deadline

The launch sets `MIGHTY_JOB_ID` in the runner's environment, so the runner
counts as a tagged process, and the watchdog's escapee sweep excluded only
the watchdog itself. On a cancel it sent the runner SIGTERM, which the
runner does not handle; at the `wall_clock` deadline it escalated to SIGKILL
five seconds later. Whether a cancelled or deadline-killed job kept its
artifacts and `result.json` was a race between the runner and its own
watchdog: a timeout cancel ended `unknown` ("the runner is dead and wrote no
result.json") in one live run and `cancelled` in others. The sweep now
excludes the runner.

Evidence: `integration/repro_job_timeout_and_interrupt` (cases 1 and 4);
unit tests for both watchdog sweeps.

### 2026-10-04: Finding: the job repros' session checks could pass with a VM still listed

The checks were `mc sessions | grep -q ENDPOINT` under `set -o pipefail`.
`grep -q` exits at the first match, `mighty-colab sessions` can then die of
SIGPIPE, and pipefail turns the match into a failure: a "still listed" check
failed wrongly, and a "no longer listed" check could pass wrongly. The
checks now grep the captured output. Each earlier run's release was also
confirmed by `mighty-colab sessions` reporting no active sessions.

Evidence: `integration/repro_job_timeout_and_interrupt` (the false failure).

### 2026-10-04: Finding: an interrupted `apply` could not exit

Interrupted after launch, `apply` wrote a correct envelope and then hung in
interpreter shutdown: the local kernel client's websocket threads are not
daemons and had not been closed. The run went on for 2 h 45 m with the VM
billing before it was noticed. `apply` now closes the client (`detach`)
before it exits, and the repro bounds every wait so a stuck process fails it.

Evidence: `integration/repro_job_timeout_and_interrupt`; a `sample` of the
stuck process showed the main thread in `wait_for_thread_shutdown`.

### 2026-10-03: The envelope records the phase that failed

`JobEnvelope.failed_phase` names the phase whose failure decided the outcome,
set once at the first failure and null when nothing failed. `job status`
prints it as `failed in:`. Semantics are in `design.md` ("Envelope, `done`,
`ok`").

Evidence: commit 8aa3df9 (unit tests in `tests/test_job_orchestrator.py` and
`tests/test_job_cli.py`).

### 2026-10-03: Finding: a failed job's `phase` reads `cleanup`

After a failure `apply` runs cleanup, which sets `phase` to `cleanup`, so the
failed step was recorded only in the text of `reason`. Resolved by
`failed_phase` (commit 8aa3df9).

Evidence: PR #76 description.

### 2026-10-03: Finding: `restart-kernel` cannot reach the job's kernel during install

The job's kernel id reaches the session record only after the first execute
call returns, and the install call is that first call. Still open.

Evidence: PR #76 description; `Orchestrator._execute_code` persists the
kernel identity after the call returns.

### 2026-10-03: Finding: signed-URL expiry does not count install time

`apply` checks data and artifact URLs against now + `wall_clock` + 15
minutes before provisioning. Install alone can take about 53 minutes (two
25-minute installer budgets plus probes) before the run starts. Still open.

Evidence: PR #76 description; `planner.revalidate_expiry` and
`install.INSTALL_KERNEL_TIMEOUT`.

### 2026-10-03: Dependencies install with uv, then pip, with classified failures

`install` tries `uv pip install --system`, then pip if uv fails. Each
installer writes into `install.log` on the VM, and each attempt is classified
(`resolution`, `build`, `auth`, `transient`, `timeout`, `unknown`). The
envelope keeps failed and fallback attempts as `install_attempts`.

Evidence: `integration/repro_job_install_outcomes` (uv first-try success;
bad pin is `fix_code` in both installers; unresolvable host is `retry_same`);
PR #76.

### 2026-10-03: Kernel-call failures are classified per phase

A kernel execute call that raises is a `KernelCallError` for its phase:
transport failures and cells ended by `KeyboardInterrupt` are `retry_same`,
anything else `do_not_retry`. A lost launch reply no longer fails the job.
Stage refusals are `fix_code`, an unreadable local source file `fix_human`.
A keep-alive daemon that cannot start fails provisioning. URL userinfo is
redacted. `jobs list`, `jobs prune` and the MCP job listings tolerate an
unreadable envelope. A websocket drop during install has no on-demand
trigger and is covered by unit tests only.

Evidence: `integration/repro_job_install_kernel_interrupted` (kernel shut
down mid-install: "kernel interrupted during install", `retry_same`,
`install.log` kept, VM released); PR #76.

### 2026-10-03: Finding: pip hides package-index HTTP statuses

pip reports an index answering 403, 429 or 500 only as "No matching
distribution found", the same as a missing package; uv names the status. The
install verdict therefore combines all attempts. For a failed build, the end
of pip's `-v` output is boilerplate; the cause is above
`error: subprocess-exited-with-error`. Recorded with uv 0.12.15 and pip
24.1.2 on a Colab CPU VM.

Evidence: `integration/capture_installer_failures`, output in
`tests/fixtures/installer_failures.json` (PR #76).

### 2026-10-03: A job runs past the runtime-proxy token expiry under `job apply`

A 70-minute CPU job under `job apply` ran past the ~60-minute token expiry,
succeeded, and was released only after the workload ended (4261 s for a
4200 s workload). The run does not show whether a token-expiry 404 reached
one of `poll`'s reads.

Evidence: `integration/repro_job_token_boundary` (PR #75).

### 2026-10-03: Never-started runner and lost VM, live

A runner that exits before writing `launch.json` was declared never started
("no launch.json 219s after launch") and its VM released 240 s after `apply`
started. A VM released out of band mid-run was noticed after 52 s and
recorded as `cleanup: already_absent` from an unassign 404 confirmed by the
assignment listing; a second release was also `already_absent`. An unassign
that fails while the endpoint is still listed cannot be produced on demand
and is covered by unit tests only.

Evidence: `integration/repro_job_never_started`,
`integration/repro_job_vm_gone` (PR #75).

### 2026-10-03: No VM is left billing without a handle or a reason

Every unassign goes through `release_assignment`; a 404 is `already_absent`
only when the assignment listing confirms it. `apply`'s poll releases the VM
when the watchdog reports the runner dead or when `launch.json` never
appears. A zombie runner counts as dead. A stage failure records
`offload: skipped`, so `on_offload_fail: leave_up` no longer keeps the VM. A
keep-alive scope error or a refused CPU grant keeps the endpoint when its
release fails. A forced teardown keeps the workload's reason and retry
class.

Evidence: PR #74, with live CPU runs: a stage failure released in 39 s with
`offload: skipped`; a SIGKILLed runner recorded as `workload: unknown` and
released 52 s after `apply` started.

### 2026-10-03: Finding: a killed runner stays a zombie and looked alive

The launch kernel never reaps the runner, so a SIGKILLed runner stays in
`/proc` state `Z` with its original start time, and the identity check
reported it alive. In a live run, a runner killed at t=0 was still reported
`runner_alive: true` at t=1805 s and `apply` waited out its deadline.

Evidence: PR #74 (live run; reproduction in a Linux container with the real
`ident.py`; Linux-only unit test).

### 2026-10-03: VM records are copied before every release; `destroy --wait`

Every release copies `runner.log`, `install.log`, `result.json` and the other
runner records into the local job directory first. `destroy` on a running job
waits for the runner (or for a live `apply` supervisor) before release.
Failed artifact records carry the exception, HTTP status and a body excerpt.
Artifact PUTs use `http.client`. The consumer runs with
`PYTHONUNBUFFERED=1`.

Evidence: PR #73, with live CPU runs: a bad pin's `install.log` copied before
release; `destroy` with a live `apply --async` supervisor waited 24 s and
reported `cancelled`; after that supervisor was SIGKILLed, `destroy` had the
runner's result after 9 s; an artifact refused by a Cloudflare-proxied
destination recorded `http_status: 404` and its body.

### 2026-10-03: Finding: urllib reports Cloudflare's early 413 as a broken pipe

Cloudflare answers a request body over 100 MB with 413 from the headers and
closes the connection. For bodies over about 1 MB, urllib then raises only
`URLError: Broken pipe`. Reading the response after the send error with
`http.client` recovered the 413 and its body in every loopback run with
bodies of 20 MB and up; smaller bodies sometimes fall back to the send
error. Not run through Cloudflare with a body over 100 MB.

Evidence: PR #73 (loopback TLS tests on macOS and in a Linux container).

### 2026-09-16: Three A100 runs crossed the token boundary

Three A100 runs of 87, 88 and 96 minutes completed past the ~60-minute
proxy-token expiry, with continuous polling, `workload: succeeded` and
`cleanup: released`.

Evidence: recorded live runs (PR #70 description).

### 2026-09-16: Finding: users need retry and resume

A consumer's spec generator (`08_overlap_bench/scripts/build_job_spec.py`)
reimplements checkpoint-resume chaining itself, and a concurrent-A100
assignment limit (`fix_human`) needed a manual re-plan and re-apply.

Evidence: issue #68, issue #69.

### 2026-09-16: Cleanup pulls `runner.log` once more before release

The final log lines written on the terminating poll tick were lost; cleanup
now copies `runner.log` immediately before release.

Evidence: PR #67.

### 2026-09-16: MCP `resources/list_changed` and `job://<id>/logs`

Evidence: PR #66.

### 2026-09-15: MCP job resources and notifications

`job://<id>` resources with terminal-only subscribe (PR #58),
`jobs://running` and `jobs://done` (PR #61), subscribe fixes (PR #62,
PR #64), and a notification when the workload leaves `pending` (PR #65).

Evidence: PR #58, PR #61, PR #62, PR #64, PR #65.

### 2026-09-15: `runner.log` on every poll; periodic artifact sync

The supervisor copies `runner.log` on every healthy poll tick.
`budgets.artifact_sync_interval_seconds` re-uploads declared artifacts during
the run.

Evidence: PR #59, PR #60.

### 2026-09-15: `job apply --async`; `job status` respawns keep-alive

`apply --async` runs apply as a detached process. `job status` respawns a
dead keep-alive daemon. Issue #54 (the daemon dies with a killed local
`apply`) remains open.

Evidence: PR #57.

### 2026-09-15: `jobs list` and `jobs prune`

`jobs list` and `jobs prune` operate on the local record collection. The same
PR added `deps` validation, handled IPv6-first DNS answers (issue #52), and
fixed `status --poll`.

Evidence: PR #53.

### 2026-09-13: Result provenance (schema 2)

Envelopes and results carry `cli_version` and `runtime_payload_version`.

Evidence: PR #44 (issue #26).

### 2026-09-13: Envelopes and exit codes aligned

Evidence: PR #38 (issue #24).

### 2026-09-13: Transfers stream; oversized payloads rejected before allocation

Evidence: PR #37 (issue #21).

### 2026-09-13: Tagged `setsid` descendants are terminated before the verdict

Evidence: PR #36 (issue #23).

### 2026-09-13: Assignment control-plane requests are bounded; keep-alive health

Evidence: PR #49 (issue #33), PR #50 (issue #10).

### 2026-09-12: Inactive controls rejected; control-result pairing and fallback

Non-default retry policy, `control.log` and `on_run_fail: skip` are plan
errors instead of accepted and ignored. Retry itself is not implemented.
Planning rejects a GCS `control.result` whose PUT and GET URLs name different
objects, and `status` and `destroy` recover a terminal result through the
control-result GET URL before classifying a job without one. A live
signed-GCS run recovered `workload: succeeded` this way.

Evidence: PR #40 (issue #27).

### 2026-09-12: Destinations must be public after DNS and redirects

Evidence: PR #35 (issue #25).

### 2026-09-12: Apply claims the job ID before assignment

Evidence: PR #34 (issue #19).

### 2026-09-12: Stage, restart and assignment refresh are bounded

Evidence: PR #32 (issue #22).

### 2026-09-11: Plans lock source path, size and digest

Evidence: PR #31 (issue #20).

### 2026-09-11: Orphaned apply recovered with terminal cleanup

`status --poll` absorbs the remote result and finishes cleanup after `apply`
is killed during `run`.

Evidence: `integration/repro_job_crash_recovery`; PR #30 (issue #16).

### 2026-09-11: Jobs own the TFE keep-alive daemon

Evidence: `integration/repro_job_keep_alive`; PR #29 (issue #17).

### 2026-09-11: Signed URLs kept out of generated state

Evidence: `integration/repro_job_signed_url_redaction`; PR #28 (issue #18).

### 2026-09-11: Cancel-only keeps the assignment

`destroy --cancel-only` terminated the workload while the assignment stayed
live; a full destroy then released it.

Evidence: `integration/repro_job_cancel_only`; commit fdb5443 (PR #15).

### 2026-09-11: A detached consumer survives a launch-kernel restart

Public `restart-kernel` restarted the launch kernel; the consumer kept its
PID and session, continued writing progress, and exited 0.

Evidence: `integration/repro_job_kernel_restart`; commit 16ef4f0 (PR #15).

### 2026-09-11: Signed GCS data, artifact and control-result paths, live

A signed GCS data GET (with sha256 check) and artifact PUT recovered the
artifact byte-identical. A second CPU run's runner replaced a pre-created
`{}` control-result object with its terminal result.

Evidence: commit bed6685 (PR #15); recorded live runs.

### 2026-09-11: T4 GPU run and workload failure path, live

Requested T4, granted T4, verify passed, a torch CUDA matmul exited 0, the
watchdog reported GPU telemetry, the VM was released. A `KeyError` in the
workload surfaced as `workload: failed`, exit 1, `retry_class: fix_code`,
with `cleanup: released`.

Evidence: commit 09ffe5c (PR #15); recorded live runs.

### 2026-09-11: Finding: Contents reads fail at ~61 minutes while the VM is intact

A quiet CPU job's `launch.json` was readable for 56 minutes, then returned
404 at 61 minutes while the assignment was still listed. Re-adopting the same
endpoint made the same file readable again, so the files were intact and the
VM was not recycled. Re-adopting both mints a fresh proxy token and
re-resolves the proxy URL, so the run does not separate token expiry from URL
rebinding; issue #3's documented token TTL makes token expiry the leading
explanation. `JobTransport` takes both the new token and the new URL, which
covers either cause. Write activity does not prevent the failure: a
continuously writing job in a field report hit the same 401/404.

Evidence: `integration/spike_job_runner/token_lifetime_spike.py` and
`token_discriminator_spike.py`; commits 4f9687d and bde7156 (PR #15);
issue #3.

### 2026-09-11: Implementation of `job plan|apply|status|destroy|list`

Live CPU runs of `job apply` completed end to end with `done: true`,
`ok: true` and the VM released, after fixes for three defects the unit
suite had not caught; install, restart and verify ran with a real
dependency pin.

Evidence: commits 5eb9aaf and 4f9687d (PR #15); recorded live runs.

### 2026-09-11: Finding from the launcher spike: the detached-runner loop works

A short kernel call started a detached runner and returned in 3.9 s; the
kernel stayed idle while the workload ran, and every verdict was read back
through the Contents API. Exit, exception, `os._exit`, SIGKILL, wall-clock,
sibling-import and duplicate-launch cases behaved as designed. The spike
also found three defects that shaped the runner:

- A `setsid` grandchild outlived a `succeeded` verdict and was invisible to
  the process-group scan: the process group is not a containment boundary.
  Closed by the job-tag sweep (PR #36).
- `killpg` on an empty group returns `EPERM` on BSD, not `ESRCH`; catching
  only `ProcessLookupError` killed the runner in its kill path and left no
  `result.json`. Any `OSError` now means nothing is left to signal.
- An escaped descendant inherits the runner's stdout pipe, so stream EOF can
  arrive long after the verdict. `done` comes from `result.json`, never from
  the stream closing.

The design document was drafted on 2026-09-09, according to the former
`design.md` frontmatter, and first committed with the spike.

Evidence: `integration/spike_job_runner/` (`local_check.py`, 12 cases;
`live_spike.py`, one CPU session, 163 s); commit 56bdc42 (PR #15).
