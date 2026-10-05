# `job` MCP resources and notifications

`docs/job/design.md` is the design record for the job supervisor itself.
This document covers the MCP resources and notifications that
`src/colab_cli/mcp_server.py` exposes so an agent can watch a job without
polling `job status` in a loop. `job apply --async` returns before the job
finishes, and these notifications tell the agent when to read the job
again.

## Resources

| URI | Content | Subscribable |
|---|---|---|
| `jobs://` | every local job record, same rows as `mighty-colab jobs list --json` | no |
| `jobs://running` | not-yet-done records, same rows as `jobs list --running --json` | no |
| `jobs://done` | finished records, same rows as `jobs list --done --json` | no |
| `job://<id>` | that job's local `envelope.json`, the same object as `.job` in `job status --json` | yes |
| `job://<id>/logs` | the locally synced `runner.log` for that job | no |
| `job://<id>/files/<name>` | one record file in the job directory: `install.log`, `result.json`, `exception.json`, `watchdog.json`, `launch.json`, `cancel.json`, `events.jsonl`, `apply.log`, `status-poll.log`, `plan.json`, `spec.json` and the rest that exist | no |

All resources read local records only; none of them asks the VM. `job
status` is the call that reads the VM.

`job://<id>/files/<name>` lists every regular file in the job directory
except the envelope and `runner.log` (which have their own URIs),
`apply.lock`, and the plan's `.mighty-colab-secrets.json` sidecar and the
temp files records are written through: those can hold signed URLs and are
never listed or readable. Only listed names can be read; `plan.json` and
`spec.json` carry URL identities, not signed URLs.

## Tool results

Each tool call runs its command once, in-process. A command that has a
`--json` envelope (`exec`, `run`, `exec-async`, `log`, `new`, `stop`,
`sessions`, `status`, and the `job` and `jobs` groups) runs in JSON mode:
the result's `structured_content` is that envelope, the same object
`--json` prints, and its text `content` is the human lines the command
prints (which `--json` moves to stderr) followed by a reading of the
envelope: the `job status` rendering for job envelopes, cell outputs for
`exec` and `run`, diagnostics, list rows, log content, or otherwise the
envelope's fields. Any other tool returns its output as text. A tool that
raises returns its exception type and message, and the traceback is logged.

The three `jobs://*` resources share one row-building function with the CLI
(`_job_list_rows`), so they show the same fields as `jobs list` and each
other. A record whose envelope cannot be read appears in those rows, and in
`resources/list`, as `envelope unreadable (...)`. Reading or subscribing to
`job://<id>` for such a record raises an error.

`jobs://*` content changes on every job's phase transitions and on every
prune, so these resources are read on demand only. A client that calls
`resources/subscribe` on one is accepted without error, but no watch task
is created and no notification fires for it.

`job://<id>/logs` reads the file that `Orchestrator.poll()` and `status
--poll` copy to `store.job_dir(job_id)/runner.log` on every healthy poll
tick, and that cleanup copies once more immediately before the VM is
released. It returns empty text, not an error, if the job exists but
nothing has been copied yet.

## Notifications

Two independent mechanisms:

A watch survives a bad read: an envelope or job list that cannot be read is
logged and read again on the next poll. A notification that cannot be sent
(the client has gone) is logged and ends that watch; the job-list watch can
then be started again.

**`notifications/resources/list_changed`** (`JobListWatcher`, one instance
per server connection, polling the job store every 5 seconds). It fires
when the set of local job records changes: a new `job plan` or `job apply`
from another process, or a `jobs prune`. A client that subscribes to every
resource it discovers through `resources/list` learns from this that a new
`job://<id>` resource exists; without it, a job created after the client
connected would run to completion with no subscription.

The baseline is the job set at the connection's first `resources/list`
call; `JobListWatcher.start()` records it then. Only jobs added or removed
after that are announced. A job that already existed was in that first
`resources/list` response. Call `resources/list` once at connect time to
get the baseline, then rely on `list_changed` for anything after.

**Per-job `resources/updated`** (`JobResourceSubscriptions`, one background
task per subscribed `job://<id>` URI, polling the local envelope every 2
seconds). It fires at most twice per job:

1. When `workload` first leaves `pending`: the runner was launched, or
   `apply` failed before launch.
2. When the envelope reaches `done` (all four fields terminal).

A job that goes from `pending` to `done` between two polls, such as one
that fails in staging, produces one notification, not two: the `pending`
transition is checked only on a poll where the job is not yet done. A
transition that happened before the subscribe is not announced.

**Subscribing to an already-done job creates no watch task and fires
nothing.** A terminal envelope does not change again, so there is no
future transition to report, and a notification on every reconnect would
make the client check each already-known terminal job again. A client that
wants an already-done job's state reads the resource.

**Subscribing to `jobs://`, `jobs://running`, `jobs://done` or a
`job://<id>/logs` resource is accepted without error and does nothing.** A
client cannot tell which listed resources support subscription without
trying, and the MCP client this server has been used with records a
subscription as made whatever the server answers, so an error would have no
visible effect. Subscribing to any other URI that is
not a `job://<id>` resource raises an error.

## Known gaps

- **No streaming variant.** This server negotiates protocol version
  <= 2025-11-25 (`resources/subscribe`/`resources/unsubscribe` +
  `notifications/resources/updated`), not the 2026-07-28
  `subscriptions/listen` streaming form. That is enough for the two
  notifications above; finer-grained progress would need it.
- **`jobs://*` aggregate resources are not subscribable** (see above). An
  agent that wants progress across all jobs reads one of the `jobs://*`
  resources on demand. Per-job subscribe is the only push-based path.
