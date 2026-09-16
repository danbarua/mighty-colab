---
log:
2026-09-16: First version. Written after a live dogfooding session: three
real `job apply --async` runs (one CPU, plus watching two real ~90-minute
A100 jobs from a separate agent's session) proved `resources/list_changed`,
`job://<id>`, `job://<id>/logs`, and the terminal-only `job://<id>`
subscribe/notify path end to end, including the one gap found live (a
stale, pre-existing server connection never receives `list_changed` for a
job it already knew about at connect time -- not a bug, the intended
"only transitions, never discovery" rule applied one layer up from where
it was designed for).
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
healthy poll tick, and — as of the `cleanup()` fix below — once more
immediately before the VM is released. No new sync mechanism; this just
exposes what's already on disk. Empty text, not an error, if the job
exists but nothing has synced yet.

## Notifications

Two independent mechanisms, both necessary, proven separately live this
session:

**`notifications/resources/list_changed`** (`JobListWatcher`, one
instance per server connection). Fires when the *set* of local job
records changes — a new `job apply`/`plan` from a completely separate
process, or a `jobs prune`. Closes the real gap a naive client has: one
that auto-subscribes to every resource discovered via `resources/list`
only ever discovers jobs that existed at connect time. Without this, a
job created mid-session runs to completion with nobody watching it.

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

**Subscribing to a non-`job://<id>` resource is also a silent no-op, not
an error.** Caught live: a client subscribed to `jobs://` because it's
listed alongside subscribable `job://<id>` resources with no way to know
in advance which support it, and raising was a dead end — the observed
client marks the subscription "succeeded" in its own bookkeeping
regardless of what the server actually did, so an error was pure log
noise with no visible effect.

## What live dogfooding proved, and the one gap it found

Three real `job apply --async` runs on the freshly-deployed feature,
plus two ~90-minute real A100 jobs from a separate agent's session
watched through the same server, confirmed:

- A workload-transition notification fires the moment a new job leaves
  `pending`, and a second, distinct notification fires on `done` —
  observed as two separate `[MCP notification]` deliveries for the same
  job, matching the design exactly.
- `job://<id>/logs` returns real synced content mid-run, not just at
  completion.
- `jobs://running` / `jobs://done` correctly partition an accumulated set
  of 13+ jobs with no lag or leakage across the split.
- `resources/list_changed` correctly advertises new jobs to a **freshly
  connected** client (27 resources, including every job's `/logs`
  sub-resource, all present and correct on connect).

**The one real gap found live**: a long-lived server connection that
predates a given job's creation never fires `list_changed` for it,
because `JobListWatcher.start()` captures `self._known` at *connection*
time, not at job-creation time relative to the client's own knowledge.
This is not a bug in the notification logic — it's the intended
"terminal-only, only fire on a transition observed after watching
started" rule (the same rule `JobResourceSubscriptions` applies per-job)
applied one layer up, to the watcher's own startup baseline, where a
stale MCP host connection means the baseline itself is stale. The
practical fix is host-side (reconnect periodically, or after any
"nothing new for a long time" suspicion), not server-side; documented
here so the next person who sees a silent job doesn't re-diagnose it as
a server defect.

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
- **Stale-connection blind spot**, described above: not fixable
  server-side without changing what "the set of jobs this connection
  already knows about" means.
