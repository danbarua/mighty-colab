---
log:
2026-09-16: First version.
---

# `job` MCP resources and notifications

`docs/job/design.md` is the design record for the job supervisor itself.
This is the client-facing surface built on top of it: the MCP resources
and push notifications `src/colab_cli/mcp_server.py` exposes so an agent
can watch a job without polling `job status` in a loop.

Mainstream coding-agent harnesses mostly treat MCP as tools-only; resource
subscriptions and server-pushed notifications are supported by the SDK but
rarely exercised end to end by a real client. This one is, and it's
load-bearing for the `job apply --async` workflow: the whole point of
`--async` is that nothing blocks waiting on the job, so *something* has to
tell the agent when it's worth looking again.

## Resources

| URI | Content | Subscribable |
|---|---|---|
| `jobs://` | every local job record, same rows as `mighty-colab jobs list --json` | no |
| `jobs://running` | not-yet-done records, same rows as `jobs list --running --json` | no |
| `jobs://done` | finished records, same rows as `jobs list --done --json` | no |
| `job://<id>` | that job's `JobEnvelope` JSON, same as `job status --json` | yes |
| `job://<id>/logs` | the locally synced `runner.log` for that job | no |

The three `jobs://*` resources and every `job://<id>/logs` resource share
one row-building/filter function with the CLI (`_job_list_rows`), so they
can never drift thinner than `jobs list` or from each other. `jobs://*`
content changes on every job's every phase transition and every prune —
far too often to notify on — so these are read-on-demand only; a client
that calls `resources/subscribe` on one is accepted silently (see below)
rather than told no, but no watch task is created and no notification
ever fires for it.

`job://<id>/logs` reads the exact file `Orchestrator.poll()` and `status
--poll` already pull to `store.job_dir(job_id)/runner.log` on every
healthy poll tick, plus once more from `cleanup()` immediately before
the VM is released. No new sync mechanism; this just exposes what's
already on disk. Empty text, not an error, if the job exists but
nothing has synced yet.

## Notifications

Two independent mechanisms:

**`notifications/resources/list_changed`** (`JobListWatcher`, one
instance per server connection). Fires when the *set* of local job
records changes — a new `job apply`/`plan` from a completely separate
process, or a `jobs prune`. Closes the real gap a naive client has: one
that auto-subscribes to every resource discovered via `resources/list`
only ever discovers jobs that existed at connect time. Without this, a
job created mid-session runs to completion with nobody watching it.

Baseline is connect-time, not job-creation-time: `JobListWatcher.start()`
records the current job set on the connection's first `resources/list`
call and only fires for jobs added or removed after that. A job that
already existed when the client connected is never announced this way —
it was already in that first `resources/list` response, so there is
nothing new to report. Not a limitation to work around; call
`resources/list` once at connect time to get the baseline, then rely on
`list_changed` for anything after.

**Per-job `resources/updated`** (`JobResourceSubscriptions`, one
background task per subscribed `job://<id>` URI, 2s poll of the local
envelope). Fires **at most twice** per job, never once per poll tick:

1. When `workload` first leaves `pending` (staging/install succeeded or
   failed outright) — the same problem `--poll`'s `never_started` fix
   solved for the CLI path, now solved for a subscriber that would
   otherwise burn tool calls on `jobs list --running` just to learn
   whether the job even started.
2. When the envelope reaches `done` (all four fields terminal) — the
   thing an agent actually blocks on.

A job that fails before ever leaving `pending` (e.g. staging itself
fails) collapses these into one notification, not two — the transition
check only runs on a tick where the job is not yet done.

**Already-done jobs are a deliberate silent no-op, not an error.**
Subscribing to a `job://<id>` that's already terminal at subscribe time
creates no watch task and fires nothing: `done=True` never changes again
(envelopes are immutable once terminal), so there is no future transition
to report, and firing anyway would force every client through "is this
actually new, or just a reconnect echo?" on every reconnect for every
already-known-terminal job it's subscribed to. A client that wants an
already-done job's current state just `read()`s it.

**Subscribing to a non-`job://<id>` resource is a silent no-op, not an
error.** A client can't tell which listed resources support subscription
without trying, so a subscribe on `jobs://` is expected. Raising would
accomplish nothing: an MCP client's own subscription bookkeeping tracks
the request as sent regardless of the server's response, so an error
here is invisible to the client and pure log noise on the server.

## Known gaps

- **No streaming variant.** This server negotiates protocol version
  <= 2025-11-25 (`resources/subscribe`/`resources/unsubscribe` +
  `notifications/resources/updated`), not the 2026-07-28
  `subscriptions/listen` streaming form. Fine for terminal-only
  notifications; would need revisiting for finer-grained progress.
- **`jobs://*` aggregate resources are not subscribable by design** (see
  above) — an agent that wants live progress across *all* jobs still has
  to poll one of the `jobs://*` resources on demand. Per-job subscribe
  is the only push-based path.
