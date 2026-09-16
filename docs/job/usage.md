---
log:
2026-09-16: Added `docs/job/mcp.md`: the MCP notification layer that lets an agent driving `job apply --async` learn a job finished without polling.
2026-09-15: Split `jobs list`/`jobs prune` out of `job` into a new sibling group. Moved this guide into `docs/job/usage.md`.
2026-09-13: Pointed spec authors to `docs/job/spec.md` for the field list, signed-URL prerequisites, and everyday examples.
2026-09-12: Fixed GCS control-result URL pairing and automatic fallback (#27), added result/envelope provenance under schema 2 (#26, later hardened), and fixed CLI help/error/exit-code consistency (#24).
2026-09-11: First version, written the same day `job` was implemented and live-verified end to end: CPU and T4 GPU runs, install/restart/verify, workload failure with cleanup, signed GCS data/artifact/control-result paths, launch-kernel restart, cancel-only termination, and job-owned keep-alive (#17). Also closed secret handling (#18), crash recovery (#16), source-file locking (#20), exclusive job-ID claims (#19), transport deadlines (#22), URL validation (#25), descendant cleanup (#23), and transfer streaming (#21).
---

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
returns. The kernel then goes idle and is no longer load-bearing. Your run's
outcome is read back by polling files over the Contents API. Dropping the connection after launch does not kill the consumer; recovery
and cleanup still have the limits below.

That is the whole idea. The detached consumer survives a dropped launch-kernel connection, but the current v0 supervisor has important limits:

- Source staging is not resumable and has weaker timeout/token-refresh handling than result polling.
- Caller-owned source specs and generated owner-mode `.mighty-colab-secrets.json` sidecars contain full signed URLs. Generated records, remote manifests, diagnostics, and kernel history contain only canonical identities and credential references.

Use `mighty-colab sessions` after every interrupted run and explicitly destroy any endpoint you no longer need.

## Six commands, two groups

```bash
mighty-colab job plan SPEC_FILE [--out PATH] [--no-probe]
mighty-colab job apply [PLAN_FILE] [--job-id ID] [--timeout S] [--leave-up]
mighty-colab job status JOB_ID [--poll] [--interval S]
mighty-colab job destroy JOB_ID [--cancel-only]

mighty-colab jobs list
mighty-colab jobs prune [--dry-run]
```

`job` takes a spec/plan/job_id -- one job at a time. `jobs` (mirrors
`terraform`/`kubectl`'s singular-vs-plural convention) operates on the
local record collection as a whole, and doesn't touch the VM.

`plan` never allocates a VM. It writes redacted `spec.json` and `plan.json` records, writes a redacted explicit `--out` path, and creates an adjacent mode-0600 `.mighty-colab-secrets.json` sidecar when query credentials exist. Keep that sidecar beside the plan: `apply` validates and hydrates it before allocation. By default planning also performs one-byte ranged GET probes of declared data URLs; `--no-probe` disables those reads. Plans are written even with warnings or errors; `apply` refuses errors and refuses warnings unless the spec sets `ignore_warnings: true`.

`apply` accepts either a plan-file positional argument or `--job-id`. `--timeout` bounds the local supervisor, not the watchdog wall clock. `--leave-up` keeps the VM after completion. `destroy --cancel-only` writes cancellation intent that runner and watchdog consume, but deliberately does not unassign the VM; failure to write the intent is an error. `jobs list` reads local job records. `jobs prune` deletes the ones that are unambiguously safe (unapplied plans, confirmed-terminal-and-released) and reports what it skipped and why; see `docs/job/store-and-cleanup.md` for the exact rule and the on-disk layout.

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
you locally. `if __name__ == "__main__":` works. With `kind: bundle`, sibling
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

v0 performs one attempt. Planning rejects non-default `retry.when`, `max_attempts`, and `mode` values until retry/recreate/resume exist. Unexpected supervisor exceptions receive `do_not_retry`; some cancellation, offload, and cleanup failures still have no `retry_class`.

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

Source staging still uses the Contents API. `kind: bundle` uploads each included file separately, not as one archive. The 250 MB check is per source file, runs during apply after VM allocation, and has no aggregate bundle ceiling. `kind: file` uploads only the entry. Data GET and artifact PUT currently buffer each complete object in VM memory; size datasets and checkpoints accordingly.

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

**Artifacts are attempted even when your run fails.** `on_run_fail: offload_anyway` is the only implemented value; planning rejects `skip` rather than silently ignoring it. A missing optional artifact does not fail offload, but a failed PUT currently fails scalar offload even when that artifact is optional.

`sha256` must be exactly 64 hexadecimal characters. It is worth the trouble: it is the only thing that distinguishes your dataset from a truncated copy, and a silently truncated input produces a result that looks plausible and is wrong.

The plan records each source file's relative path, size, and SHA-256. Apply refuses added, removed, renamed, or changed files before assignment and stages only those locked bytes. Re-run `job plan` after every source change.

## Things that will bite you

**The ~60 minute wall.** The runtime-proxy token expires about an hour in, and
expiry shows up as `401`/`404` — which looks exactly like "the VM is gone." It
is not. We confirmed this by direct experiment: at t+61min a job's files 404'd,
the assignment was still listed, and re-adopting made the *same files* readable
again. They had been intact the whole time. `job status` refreshes the token
for you. If you are writing your own polling loop against `exec`, you must do
the same, or you will conclude a healthy VM died.

**A GPU you asked for and did not get.** Upstream `new` maps an unrecognised
accelerator name onto A100, and capacity pressure can hand back a CPU box.
`job` refuses that: a GPU request satisfied by a CPU is unassigned and reported
as `retry_different`. If you genuinely want CPU, say `accept_cpu: true`.

**Dependencies that aren't live.** A `pip install` of a package already
imported at kernel boot does nothing until the interpreter restarts. `job`
restarts after installing and then re-probes against `sys.modules` — the
question is what your code will actually import, not what pip reported.

**`retry.when`, not `retry.on`.** YAML 1.1 resolves a bare `on:` key to boolean
`true`, so the field is named `when`.

**Escaped descendants.** The runner and watchdog scan for their job tag and
terminate tagged `setsid` processes with identity-checked SIGTERM/SIGKILL. The
runner refuses `succeeded` when `/proc` detection is unavailable or a tagged
process survives the reap.

## If the supervisor dies

Closing the laptop after the launch RPC normally leaves the detached consumer running, but v0 does not implement full supervisor takeover.

```bash
mighty-colab job status <id> --poll
```

This command observes remote result, launch, and watchdog records. With `--poll` it continues until a remote verdict, a dead runner, a lost assignment, or a never-started orphan can be classified. For an orphaned supervisor it then finishes pending cleanup; a live runner or a healthy concurrent supervisor remains untouched. The returned envelope can therefore still have `done: false` when the job is legitimately running.

After an interrupted apply, run `status --poll`; it uses the control-result GET fallback when the VM result is unavailable. Then inspect the account and destroy the allocation explicitly.

## Cost discipline

Normal apply paths attempt cleanup in a `finally` block, including unexpected exceptions before and during run. `--leave-up`, an interrupted local supervisor, and `on_offload_fail: leave_up` can leave an allocation. Hard process death can also leave a non-terminal record; local state is not proof of release.

When in doubt:

```bash
mighty-colab job destroy <id>    # exits 0 if the local record says already gone
mighty-colab sessions            # server-side assignment inventory
```

`destroy --cancel-only` is different: it asks the runner to stop but intentionally keeps the VM. Full destroy reads any remote result before unassignment and preserves that workload verdict; when no verdict is available, it records `unknown` rather than inventing `cancelled`. A cleanup failure means release was not confirmed, not that the VM is certainly live; `sessions` is the final check.

## Known gaps

Be aware of these before trusting a long run:

- No GPU run has yet outlived the approximately 60-minute proxy refresh boundary.
- Caller-owned source specs and generated `.mighty-colab-secrets.json` sidecars still contain full signed URLs and require credential handling.
- Retry/recreate/resume and `control.log` are not implemented; planning rejects non-default policy values.

Verified live on 2026-09-11: CPU and T4 GPU runs end to end; install/restart/verify with a real dependency pin; the workload failure path with cleanup; proxy access recovery after the approximately 60-minute failure; explicit public launch-kernel restart while a detached consumer continued; cancel-only termination while the assignment remained live, followed by full teardown; signed GCS data GET, artifact PUT, and control-result PUT; job-owned TFE keep-alive through idle leave-up and destroy; and supervisor crash recovery via `status --poll` after killing apply during run. These runs do not verify platform-initiated kernel replacement or the gaps above.
