---
log:
2026-09-15: First version. Written after a dogfooding session accumulated 58
local job records with no documented way to prune them, and after
`overlap-pursuit-b-attractor-*` `job plan` retries during that same session
made it clear `(planned, not applied)` records need explaining too.
---

# Job store layout and manual cleanup

`docs/job/design.md` is the design record, `docs/job/usage.md` is the
command guide, `docs/job/spec.md` is the spec-file reference. This is the
one thing none of the three cover: where local job records live on disk,
what each file means, and what is actually safe to delete by hand.

**There is no `job prune` or `job rm` command yet.** `JobStore.list_jobs()`
(`src/colab_cli/job/store.py`) just enumerates every subdirectory under the
store root — nothing expires, nothing gets garbage-collected, and `job list`
will show every job this machine has ever planned or applied, forever. This
doc exists so a manual cleanup doesn't need to be re-derived from source
every time it comes up.

## Where records live

```
~/.config/colab-cli/jobs/<job_id>/
```

(or `<config_path's directory>/jobs/<job_id>/` if `--config` was passed —
`_store()` in `src/colab_cli/commands/job.py` mirrors `StateStore`'s own
default rather than assuming a fixed path.)

Each job gets one directory, one record per file inside it:

| File | Written by | Meaning if present |
|---|---|---|
| `plan.json` | `job plan` (and `job apply`, via `--out`) | The planned spec + resolved source-file hashes. Signed URLs inside are redacted to a marker; the real values live in a sidecar (see below). |
| `spec.json` | `job plan` | Redacted copy of the input spec, same URL-redaction treatment. |
| `envelope.json` | `job apply` / `job status` / `job destroy` | The four-field verdict (`workload`/`offload`/`cleanup`/`supervisor`) plus `done`/`ok`. **Absence of this file is what `job list` renders as `(planned, not applied)`** — `job plan` writes `plan.json`/`spec.json` immediately but never touches `envelope.json`; only `apply`/`status`/`destroy` do. |
| `supervisor.json` | `job apply`, while running | Live supervisor identity (pid/starttime/boot_id), used to tell a dead supervisor from a live one across processes. Cleared (`clear_supervisor_identity`) once apply finishes. |
| `apply.lock` | `job apply`, while running | `flock`-held exclusive lock — a second `apply` on the same job ID fails fast (`ApplyInProgress`) while this is held by a live process, and is taken over if the holder is dead. Removed when the lock releases. |
| `events.jsonl` | `job apply` | Append-only phase-transition log for that one job (distinct from `HistoryLogger`'s per-*session* CLI-invocation log). |

Separately, a plan file passed with `job plan --out <path>` (e.g.
`/tmp/plans/job.json`, anywhere the caller points it — not inside the
store) gets a `<path>.mighty-colab-secrets.json` sidecar holding the real
signed URLs the plan file itself redacted. `.gitignore` already excludes
`**/.mighty-colab-secrets.json` and `plans/` in repos that use that
convention — the store's own `plan.json`/`spec.json` get the same
redaction treatment for the same reason (AGENTS.md's data-plane credential
rules apply here too: full URLs only ever live in caller-owned specs and
this one owner-mode sidecar).

## What `job list`'s summary column means

```
overlap-pursuit-b-1000-verify-20260914T012727Z-8b286e  (planned, not applied)
overlap-pursuit-b-1000-verify-20260914T013829Z-c88780  failed/skipped/released  done=True
overlap-pursuit-b-1000-verify-20260914T020218Z-bdd4c4  succeeded/ok/released  done=True
```

The three-slash field is `workload/offload/cleanup` off the envelope, read
straight off `envelope.json` — `job list` does not re-check the VM. For a
trustworthy live answer for one job, use `job status --poll <job_id>`
instead, which asks the VM.

## What's safe to delete by hand

There is no supported command for this yet — the following is what's true
about the on-disk state, for manually clearing a directory that's grown
past what `job list` is useful for.

- **`(planned, not applied)`** — always safe. `apply` never ran; no VM was
  ever touched. This is the bulk of what accumulates from `job plan`
  iteration during spec authoring (every failed plan, every retry while
  fixing a spec, gets its own job ID and directory).
- **`done=True` with `cleanup: released` or `cleanup: already_absent`** —
  safe. Terminal, no VM, confirmed absent.
- **`done=True` with `cleanup: failed`** — **check `mighty-colab sessions`
  first.** This means the *confirmation* of teardown failed, not
  necessarily that the VM is still up — AGENTS.md's forced-teardown path
  (`Credential Isolation` / item 10) tears the VM down unconditionally when
  credential deletion can't be confirmed, so in practice this is usually
  already safe, but the local record alone can't prove it. Run
  `mighty-colab sessions` (and `job status --poll <job_id>` for that
  specific job) before deleting; if either shows a live endpoint, run
  `mighty-colab job destroy <job_id>` first, then re-verify with
  `mighty-colab sessions` before deleting the directory.
- **`done=False`, or an `apply.lock`/`supervisor.json` present** — do not
  delete. Either `apply` may still be running in another process on this or
  another machine, or a prior run was interrupted mid-flight. Run `job
  status --poll <job_id>` to get a live read before touching it.

## Live check commands

```bash
# Trustworthy verdict for one job (asks the VM, not local memory)
mighty-colab job status --poll <job_id>

# Is anything on this account currently billing, independent of local records?
mighty-colab sessions

# Force teardown for a job that's known-billing or ambiguous
mighty-colab job destroy <job_id>
```

`mighty-colab sessions` is the ground truth for "is anything billing right
now" — it asks the server directly, unlike `job list`'s envelope-only view.
Always confirm with it after any manual cleanup, the same way AGENTS.md
already requires after any `job apply` failure.
