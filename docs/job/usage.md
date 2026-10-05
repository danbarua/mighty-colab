# Running a job

`job` exists for one situation: **you want to start a long computation on a
Colab VM and not babysit it.**

If you are sitting at a terminal watching output scroll past, `exec` and `run`
are better tools and you should keep using them. `job` is for the case where
the thing that starts the run is not the thing that collects the result — an
agent, a cron, a laptop that will close.

## The one thing to understand first

`exec` runs your code *inside the Jupyter kernel*. When the websocket drops,
the kernel kills its children, and your training run dies with it. On a flaky
link that makes anything longer than a couple of minutes unusable, and the
failure looks like `[colab] Error: Connection lost.` with no verdict at all.

`job` makes **one short kernel call** that starts a detached process and
returns. The kernel then goes idle and the run no longer depends on it. Your
run's outcome is read back by polling files over the Contents API. Dropping
the connection after launch does not kill the consumer.

The steps before launch still depend on the connection:

- Source staging is not resumable. A transport failure during stage fails the job with `retry_same`, or `retry_different` when the assignment is gone.
- Caller-owned source specs and generated owner-mode `.mighty-colab-secrets.json` sidecars contain full signed URLs. Generated records, remote manifests, diagnostics, and kernel history contain only canonical identities and credential references.

Use `mighty-colab sessions` after every interrupted run and explicitly destroy any endpoint you no longer need.

## Six commands, two groups

```bash
mighty-colab job plan SPEC_FILE [--out PATH] [--no-probe]
mighty-colab job apply [PLAN_FILE] [--job-id ID] [--timeout S] [--leave-up] [--async]
mighty-colab job status JOB_ID [--poll] [--interval S]
mighty-colab job destroy JOB_ID [--cancel-only] [--wait S]

mighty-colab jobs list [--running | --done]
mighty-colab jobs prune [--dry-run]
```

`job` takes a spec/plan/job_id -- one job at a time. `jobs` (mirrors
`terraform`/`kubectl`'s singular-vs-plural convention) operates on the
local record collection as a whole, and doesn't touch the VM.

`plan` never allocates a VM. It writes redacted `spec.json` and `plan.json` records, writes a redacted explicit `--out` path, and creates an adjacent mode-0600 `.mighty-colab-secrets.json` sidecar when query credentials exist. Keep that sidecar beside the plan: `apply` validates and hydrates it before allocation. By default planning also performs one-byte ranged GET probes of declared data URLs; `--no-probe` disables those reads. Plans are written even with warnings or errors; `apply` refuses errors and refuses warnings unless the spec sets `ignore_warnings: true`.

`apply` accepts either a plan-file positional argument or `--job-id`. `--timeout` bounds the local supervisor (default: `wall_clock` + 600 seconds). When it passes with no verdict, apply cancels the job, waits up to 300 seconds for its result, and releases the VM; the watchdog's `wall_clock` kill still applies on the VM. `--leave-up` keeps the VM after completion. `--async` starts `apply` as a detached background process and returns at once with the job ID, the process ID and the path of its log (`apply.log` in the job directory); follow it with `job status --poll`.

`destroy --cancel-only` writes cancellation intent that runner and watchdog consume, but deliberately does not unassign the VM; failure to write the intent is an error. A full `destroy` of a running job writes the same intent and waits up to `--wait` seconds (default 300) for the runner to stop the job, upload artifacts and write its result before release. If the job's `job apply` is still running, `destroy` waits for that supervisor to release the VM instead of releasing it a second time.

`jobs list` reads local job records; `--running` and `--done` filter on `done`. `jobs prune` deletes the ones that are safe to delete (unapplied plans, confirmed-terminal-and-released) and reports what it skipped and why; see `docs/job/store-and-cleanup.md` for the exact rule, its one known gap, and the on-disk layout.

Under `--json`, every job command emits a validated envelope for normal results and expected errors. Job state is nested under `.job` and the convenience `.done`/`.ok` fields are copied to the outer wrapper. Outer `status` and `exit_code` describe the CLI invocation; nested job fields describe the workload. Thus a failed apply exits one with outer `exit_code: 1`, while a successful status query reporting that failed workload exits zero with outer `status: ok` and nested `ok: false`.

### Chaining them from a script

`plan` mints the job id, so take it from the envelope rather than scraping
the human-readable line:

```bash
JID=$(mighty-colab --json job plan spec.yaml | jq -r .job_id)
mighty-colab job apply --job-id "$JID"
mighty-colab --json job status "$JID" | jq '{done, ok}'
```

### Exit codes

This distinction is the one a scripted consumer most often gets wrong:

| command | exits non-zero when |
|---|---|
| `job plan` | the spec has errors (allocates nothing either way) |
| `job apply` | **the job did not succeed** (`ok: false`) |
| `job status` | the *query* failed. A successfully-reported failed job exits **0** |
| `job destroy` | teardown actually failed. Already-gone exits 0 |

So `job apply ... && evaluate.py` is safe: a run that raised will not
advance. But `job status ... && evaluate.py` is **not** — `status`
succeeding only means it got an answer. Branch on `ok` from the envelope:

```bash
mighty-colab --json job status "$JID" | jq -e .ok >/dev/null && evaluate.py
```

## A minimal spec

The full field list, plan refusals, and signed-URL steps are in `docs/job/spec.md`.

```yaml
name: my-experiment

accelerator:
  prefer: [A100, L4, T4]    # tried in order; nothing is substituted silently
  accept_cpu: false

code:
  kind: bundle
  root: ./src               # becomes sys.path[0] on the VM
  entry: train.py           # always relative to root
  args: ["--epochs", "50"]

deps:
  - torch==2.4.1

budgets:
  wall_clock: 7200
```

Paths in `code` resolve relative to **the spec file**, not your working
directory, so the same spec works from anywhere.

## Your script does not have to change

There is no SDK to import, no heartbeat to emit, no callback to register.
`runpy` executes your entry with a real `sys.argv`, a real `__file__`, and
`sys.path[0]` set to its own directory, which is what `python train.py` gives
you locally. `if __name__ == "__main__":` works. The script runs with `PYTHONUNBUFFERED=1`, so its output reaches `runner.log` as it is printed. With `kind: bundle`, sibling
modules below `root` are uploaded and sibling imports work. With `kind: file`,
only the entry file is uploaded; undeclared sibling modules are absent.

Your exit code is the verdict. Raise to fail, return 0 to succeed.

**A silent job is not a suspicious job.** Liveness is observed from outside by
a watchdog process; you are never punished for not printing. This is why there
is no stall timeout — "stdout went quiet" is exactly what a healthy JAX or XLA
compile looks like, and killing on it is how you lose good runs.

## Reading the result

Four fields answer separate questions:

| field | what it answers |
|---|---|
| `workload` | did your code succeed, fail, get cancelled, or become unknown |
| `offload` | did declared artifacts upload |
| `cleanup` | was release confirmed or was the VM left up |
| `supervisor` | is the original supervisor terminal |

The booleans are not synonyms:

- **`done`** — all four fields are terminal.
- **`ok`** — workload succeeded, offload is `ok` or `not_required`, and cleanup is `released`, `already_absent`, or `left_up`.

`ok` does not include the supervisor field, so it can be true while `done` is false. Poll `done`; interpret `ok` only after the state is terminal. `left_up` counts as `ok` but still bills.

Examples:

- `workload: succeeded` + `cleanup: failed` means the result may be fine, but release was not confirmed and the VM may still bill. Run `job destroy` and check `sessions`.
- `workload: failed` + `cleanup: released` is a cleanly reported workload failure with no confirmed allocation left behind.

### Where it failed: `failed_phase`

`failed_phase` names the phase whose failure decided the outcome: `provision`, `install`, `restart`, `verify`, `stage`, `run`, `offload` or `cleanup`. It is null when nothing failed, including a cancelled job, a job still running, and an interrupted local supervisor. `phase` is only the last phase reached, which is `cleanup` once the VM has been released, so do not read `phase` as the failure. `run` covers a failed workload, a runner that died or never started, and a lost assignment; `offload` means the workload succeeded and an upload failed; `cleanup` means everything before it succeeded and the release failed. The full rule is in `docs/job/design.md`. `job status` prints it as `failed in:`.

Decide what to do next from `failed_phase` and `retry_class`. `reason` is text for a person and its wording changes; do not parse it.

### `retry_class` tells you what to do next

When present, treat it as advice for the next action, not an automatic retry promise:

| value | meaning |
|---|---|
| `fix_code` | your spec or script is wrong; unchanged retry should fail again |
| `fix_human` | credentials, quota, or grants need human action |
| `retry_same` | the failure was classified as transient |
| `retry_different` | change accelerator or machine shape |
| `refresh_urls` | re-sign URLs and re-plan |
| `do_not_retry` | do not retry unchanged |

`apply` makes one attempt and never retries by itself. Planning rejects non-default `retry.when`, `max_attempts`, and `mode` values. Unexpected supervisor exceptions receive `do_not_retry`. A workload cancelled by `job destroy`, and a failed release after a successful workload, have no `retry_class`.

Some outcomes you will see:

- a run killed at `budgets.wall_clock` is `fix_code`: raise the budget, checkpoint, or make it faster. The reason starts `wall_clock budget of <n>s reached`.
- a run killed by a signal nobody requested is `fix_code`. When the kernel's out-of-memory killer did it, the reason says so and quotes the kernel's `Killed process` line.
- a failed data download or artifact upload: 401 or 403 is `refresh_urls`; 404 is `fix_code` for a download and `refresh_urls` for an upload; 408, 429 and 5xx are `retry_same`; other 4xx (413 included) and a size or sha256 mismatch are `fix_code`; no response at all is `retry_same`.
- when the run failed and an upload failed too, the run's class decides, and the reason names both.
- when no accelerator in `prefer` can be assigned: no quota or entitlement (400) or a credentials problem (401/403) is `fix_human`, a capacity error (5xx) is `retry_different`, no response is `retry_same`. `provision_attempts` in the envelope lists each accelerator tried.
- a plan error from the data probe: 401/403 is `refresh_urls`, 404 is `fix_code`, a transient failure is `retry_same` (re-run `job plan`).

## Data and artifacts

Large data belongs behind HTTPS GET URLs, not in the source bundle. The runner downloads each declared item and verifies `size_bytes` and `sha256` when present:

```yaml
data:
  - url: https://storage.googleapis.com/bucket/x.npy?X-Goog-Signature=...
    dest: /content/data/x.npy
    sha256: 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
artifacts:
  - path: /content/out/model.pt
    url: https://storage.googleapis.com/bucket/runs/model.pt?X-Goog-Signature=...
    required: true
```

Use a full 64-hex-character SHA-256 digest. Relative destinations resolve below `/content/jobs/<id>`; absolute destinations must remain below `/content`.

Source staging uses the Contents API. `kind: bundle` uploads each included file separately, not as one archive. `plan` reports an error for any source file over 250 MB and a warning when the whole source tree exceeds 250 MB; `apply` refuses an oversized source file again before assigning a VM. `kind: file` uploads only the entry. The runner streams each data GET and artifact PUT to and from disk in chunks, so object size is limited by disk, not memory; `verify` refuses a job whose declared source, input and output sizes exceed 80% of free disk.

Signed URLs never enter generated spec/plan records, remote data/artifact manifests, validation diagnostics, kernel source/history, or the consumer process's environment and argv. Full URLs remain in the caller-owned source spec and the mode-0600 plan sidecar. Apply uploads them only after public payload staging into a mode-0600 handoff; seal, launch, and the isolated runner fail closed when a declared transfer requires a missing handoff. The launch kernel opens and unlinks it, and `python -I -S -c` consumes the inherited descriptor before starting the consumer. Interrupted recovery confirms deletion or forcibly releases the assignment. Keep source specs and sidecars private. Requirements, install hooks, existing same-UID processes, and the single-user VM must be trusted: these protections prevent persistence and accidental inheritance, not deliberate credential theft by code that runs before upload.

### Off-VM result backstop

`control.result` is optional defence-in-depth: the runner PUTs its terminal
`result.json` to an object that remains readable if the VM later disappears.
GCS cannot sign a GET for an object that does not exist, so create a fresh
placeholder **before** signing the GET URL:

```bash
OBJECT=gs://your-job-bucket/runs/$JOB_ID/result.json
SIGNER=your-job-signer@your-project.iam.gserviceaccount.com
REGION=US

PUT_URL="$(gcloud storage sign-url "$OBJECT" \
  --impersonate-service-account="$SIGNER" --region="$REGION" \
  --http-verb=PUT --duration=8h --format='value(signed_url)')"
printf '{}\n' | curl --fail --silent --show-error -X PUT \
  -H 'Content-Type: application/octet-stream' --data-binary @- "$PUT_URL"
GET_URL="$(gcloud storage sign-url "$OBJECT" \
  --impersonate-service-account="$SIGNER" --region="$REGION" \
  --http-verb=GET --duration=8h --format='value(signed_url)')"
```

Put those values under `control.result.put_url` and `.get_url`. Do not add
`--headers` to `sign-url`: the runner sends
`Content-Type: application/octet-stream`; signing a different header turns the
later PUT into a bare 403. The object name MUST be unique per job, and `{}` is
only a placeholder, never a completed verdict. Control URLs must remain valid
for `retry.budget_seconds` plus 15 minutes. When the VM result cannot be reached,
`status` and `destroy` automatically read the GET URL and absorb a terminal JSON
result; `{}` and non-terminal records are ignored. Treat both URLs and the files
containing them as credentials.

New terminal results include `schema_version`, `cli_version`, and
`runtime_payload_version`. The CLI value is fixed before launch and passed to
the runner. The runtime value is a `sha256:` digest over the exact Python
payload copied to the VM, so a payload that differs from its caller remains
identifiable. Local envelopes carry and absorb both values. These records use
result schema 2; plans and other runner records remain schema 1. Current readers
accept old result-schema-1 envelopes and preserve local provenance when an old
remote result omits the provenance fields. A schema-2 result must contain both
producer fields; unknown result schemas are rejected rather than relabeled.
Absorption validates a copy first, so an invalid terminal field leaves the
persisted envelope unchanged.

**Artifacts are attempted even when your run fails.** `on_run_fail: offload_anyway` is the only implemented value; planning rejects `skip` rather than silently ignoring it. A missing optional artifact does not fail offload, but a failed PUT fails scalar offload even when that artifact is optional.

A failed artifact says why. Its record in the envelope's `artifacts[]` carries `error`:

```json
{"exception": "HTTPStatusError",
 "reason": "HTTP 413 Payload Too Large (upload cut short: BrokenPipeError: [Errno 32] Broken pipe)",
 "http_status": 413,
 "body": "<html><head><title>413 Request Entity Too Large</title>...",
 "category": "http"}
```

`body` is the first 300 bytes of the response; `category` is `http` when there was a response, otherwise `network`, `checksum`, `size`, `local`, `blocked`, `setup` or `error` (see `design.md`). When the server closes the connection before its response can be read, `exception` is `UploadCutShort`, `http_status` is `null`, and `reason` names the send error and the error from reading the response. The envelope's `reason` names each failed artifact with its cause, and `job status` prints each artifact's error and response body. `runner.log` gets one `[runner] artifact upload failed` line per failure. A destination behind Cloudflare rejects any request body over 100 MB with 413.

A failed input says why the same way. The envelope's `inputs[]` lists each input staged before the failure, with its `bytes` and `sha256`, and then the one that failed, with `error`. The reason names it, for example `staging failed at inputs/x.npz (https://storage.googleapis.com/bucket/x.npz#1a2b3c4d5e6f): HTTP Error 403: Forbidden. The consumer never started.`, and `job status` prints it as an `input:` line.

`sha256` must be exactly 64 hexadecimal characters. It is worth the trouble: it is the only thing that distinguishes your dataset from a truncated copy, and a silently truncated input produces a result that looks plausible and is wrong.

The plan records each source file's relative path, size, and SHA-256. Apply refuses added, removed, renamed, or changed files before assignment and stages only those locked bytes. Re-run `job plan` after every source change.

## Things that will bite you

**Runtime-proxy token expiry.** The token that Contents requests use expires
about an hour after it is issued, and expiry shows up as `401`/`404` — which
looks exactly like "the VM is gone." It is not: the VM and its files are
intact. `job apply`, `job status` and `job destroy` re-resolve the assignment
on a 401/404, take the fresh token, and retry, so a job runs past the
boundary without intervention. If you write your own polling loop against
`exec` or the Contents API, you must do the same, or you will conclude a
healthy VM died.

**Signed URLs that expire during a long install.** Install runs before your
script and can take up to 55 minutes. When `deps` is not empty, plan and
apply require data and artifact URLs to stay valid for `wall_clock` plus
15 minutes plus those 55 minutes, and `verify` checks again after install.
The error says when the URL expires and how long it must last.

**A GPU you asked for and did not get.** Upstream `new` maps an unrecognised
accelerator name onto A100, and capacity pressure can hand back a CPU box.
`job` refuses that: a GPU request satisfied by a CPU is unassigned and reported
as `retry_different`. If you genuinely want CPU, say `accept_cpu: true`.

**Dependencies that aren't live.** A `pip install` of a package already
imported at kernel boot does nothing until the interpreter restarts. `job`
restarts after installing and then re-probes against `sys.modules` — the
question is what your code will actually import, not what pip reported.

**A dependency that will not install.** `job` installs `deps` with uv and
falls back to pip if uv fails. A failed install says why: `reason` names the
packages and each installer's exit status and key error lines, `retry_class`
is `fix_code` for a pin that cannot be resolved or a package that fails to
build, `fix_human` for an index that refuses credentials (401/403), and
`retry_same` for an index that is unreachable or failing (DNS, connection,
429, 5xx). `install_attempts` in the envelope keeps each attempt's installer,
version, command, index configuration and key lines, and `install.log` in
the local job directory has the full output. An installer that runs past its
25-minute budget is `fix_code`: the usual cause is a source build, and the fix
is a version with a prebuilt wheel. A lost kernel connection during install, or a kernel
interrupted, restarted or shut down while it installs, is `retry_same`.

**`restart-kernel` during install.** `mighty-colab restart-kernel -s job-<id>`
cannot reach the job's kernel while install runs; the session record learns
the kernel's id only after the install call returns.

**`retry.when`, not `retry.on`.** YAML 1.1 resolves a bare `on:` key to boolean
`true`, so the field is named `when`.

**Escaped descendants.** The runner and watchdog scan for their job tag and
terminate tagged `setsid` processes with identity-checked SIGTERM/SIGKILL. The
runner refuses `succeeded` when `/proc` detection is unavailable or a tagged
process survives the reap.

## If the supervisor dies

Closing the laptop after the launch RPC normally leaves the detached consumer running, but `job` does not implement full supervisor takeover. This section covers an `apply` process that was killed or died, or was stopped with Ctrl-C after the runner was launched: each leaves `cleanup` non-terminal, so the commands below can finish the job.

```bash
mighty-colab job status <id> --poll
```

This command observes remote result, launch, and watchdog records. With `--poll` it continues until a remote verdict, a dead runner, a lost assignment, or a never-started orphan can be classified. For an orphaned supervisor it then finishes pending cleanup; a live runner or a healthy concurrent supervisor remains untouched. The returned envelope can therefore still have `done: false` when the job is legitimately running.

The keep-alive daemon that stops Colab reclaiming the idle VM can die with a killed `job apply`. Every `job status` call on a job whose cleanup is still pending respawns the daemon if it is dead and adds a hint saying so. Nothing else respawns it, so poll an orphaned job with `job status` until it finishes.

After a killed apply, run `status --poll`; it uses the control-result GET fallback when the VM result is unavailable. Then inspect the account and destroy the allocation explicitly.

## Cost discipline

Normal apply paths attempt cleanup in a `finally` block, including unexpected exceptions before and during run. Before any release, by `apply`, `destroy` or `status --poll`, the VM's `runner.log`, `install.log`, `result.json`, `watchdog.json`, `launch.json`, `exception.json`, `cancel.json` and manifests are copied into `~/.config/colab-cli/jobs/<id>/`. The envelope's `hints` name what was copied. Read those files after a failure; the VM is gone. `apply` does not wait out its deadline for a runner that cannot finish: when the watchdog reports the runner dead with no result, or no `launch.json` has appeared 120 seconds after launch and none for 90 seconds of polling, it records `workload: unknown` with the reason and releases the VM.

These leave a VM running and billing:

- `--leave-up`.
- `on_offload_fail: leave_up` (the default) when an artifact upload was attempted and failed, so the file can be rescued from the VM. A required artifact that was never produced (the run crashed or was killed first) also fails offload, but the VM is released: there is nothing on it to rescue, and its records are copied first. An unconfirmed deletion of the transfer credential file releases the VM in every case.
- Ctrl-C after the runner was launched. The run continues and the VM stays assigned and billing until the job is released: run `job status <id> --poll` to collect the result and release it when the job ends, or `job destroy <id>` to stop it now. SIGTERM (an agent harness ending a long tool call) or SIGHUP after launch does this for you: `apply` starts a detached `job status --poll` on its way out, which releases the VM when the job ends. (`--timeout`, and any of these signals before launch, release the VM themselves.) A SIGKILL cannot be handled, so drive long jobs with `job apply --async` and `job status --poll`.
- Hard process death of `apply`, which can also leave a non-terminal record; local state is not proof of release.

When in doubt:

```bash
mighty-colab job destroy <id>    # exits 0 if the local record says already gone
mighty-colab sessions            # server-side assignment inventory
```

`destroy --cancel-only` is different: it asks the runner to stop but intentionally keeps the VM. Full destroy reads any remote result before unassignment and preserves that workload verdict; when no verdict is available, it records `unknown` rather than inventing `cancelled`. A cleanup failure means release was not confirmed, not that the VM is certainly live; `sessions` is the final check.

## Known gaps

Be aware of these before trusting a long run:

- Retry/recreate/resume and `control.log` are not implemented; planning rejects non-default policy values.
- Caller-owned source specs and generated `.mighty-colab-secrets.json` sidecars still contain full signed URLs and require credential handling.
- The longest recorded runs are 96 minutes (A100) and 70 minutes (CPU); no recorded run has crossed a second token expiry, at about two hours.
- A platform-initiated kernel replacement or crash is unverified; the explicit `restart-kernel` path is verified.

`docs/job/design.md` lists every known gap, and `docs/job/chronology.md` lists what has been verified live and when.
