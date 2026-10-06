# Job store layout and manual cleanup

`docs/job/design.md` is the design record, `docs/job/usage.md` is the
command guide, `docs/job/spec.md` is the spec-file reference. This document
covers where local job records live on disk, what each file means, and what
`jobs prune` deletes.

**Prefer `mighty-colab jobs prune` (`--dry-run` first) over deleting these
directories by hand.** It applies the rule in "What `jobs prune` deletes"
below and reports what it skipped and why. The rest of this
document explains what the command checks, for anyone auditing it or
cleaning up by hand (a different machine, a broken install, a script).

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
| `plan.json` | `job plan` | The planned spec + resolved source-file hashes. Signed URLs inside are replaced by a marker; the real values live in the sidecar below. |
| `plan.json.mighty-colab-secrets.json` | `job plan`, when the spec has signed URLs | Owner-only (mode 0600) map from each marker to the full signed URL. `apply --job-id` reads it. |
| `spec.json` | `job plan` | Redacted copy of the input spec, same URL-redaction treatment. |
| `envelope.json` | `job apply` / `job status` / `job destroy` | The four-field verdict (`workload`/`offload`/`cleanup`/`supervisor`) plus `failed_phase`, `retry_class` and `reason`. **Absence of this file is what `jobs list` renders as `(planned, not applied)`** — `job plan` writes `plan.json`/`spec.json` immediately but never touches `envelope.json`; `apply` first writes it when provisioning starts. |
| `supervisor.json` | `job apply`, while running | Live supervisor identity (pid/starttime/boot_id), used to tell a dead supervisor from a live one across processes. Removed once apply finishes. |
| `apply.lock` | `job apply`, while running | `flock`-held exclusive lock — a second `apply` on the same job ID fails fast (`ApplyInProgress`) while a live process holds it, and takes it over if the holder is dead. Removed when the lock is released. |
| `apply.log` | `job apply --async` | Output of the detached `job apply` process. |
| `events.jsonl` | `job apply` | Append-only phase-transition log for that one job (distinct from `HistoryLogger`'s per-*session* CLI-invocation log). |
| `runner.log` | `job apply` / `job status`, every poll; every release | The VM's runner log: the consumer's stdout/stderr and `[runner]` lines. |
| `install.log`, `result.json`, `exception.json`, `watchdog.json`, `launch.json`, `cancel.json`, `offload.manifest.json`, `stage.manifest.json` | whichever of `job apply` / `job destroy` / `job status --poll` releases the VM, immediately before release | Copies of the VM's job records, for any the VM had. The envelope's `hints` name what was copied. Nothing in the local store reads them; they are for the person or agent diagnosing the run. |

Separately, a plan file passed with `job plan --out <path>` (e.g.
`/tmp/plans/job.json`, anywhere the caller points it — not inside the
store) gets a `<path>.mighty-colab-secrets.json` sidecar holding the real
signed URLs the plan file itself redacted. `.gitignore` already excludes
`**/.mighty-colab-secrets.json` and `plans/` in repos that use that
convention. Full signed URLs exist only in the caller-owned spec and these
owner-mode sidecars; every other record holds redacted identities.

## What `jobs list`'s summary column means

```
overlap-pursuit-b-1000-verify-20260914T012727Z-8b286e  (planned, not applied)
overlap-pursuit-b-1000-verify-20260914T013829Z-c88780  failed/skipped/released  done=True
overlap-pursuit-b-1000-verify-20260914T020218Z-bdd4c4  succeeded/ok/released  done=True
```

The three-slash field is `workload/offload/cleanup`, read straight from
`envelope.json` — `jobs list` does not re-check the VM. `--json` rows also
carry `phase`, `done`, `endpoint` and `reason`. For a live answer for one
job, use `job status --poll <job_id>`, which asks the VM.

## What `jobs prune` deletes

`mighty-colab jobs prune` (`--dry-run` first) applies this rule and reports
what it removed and skipped, with reasons:

- **No `envelope.json`** — deleted as `(planned, not applied)`. This is the
  bulk of what accumulates from `job plan` iteration during spec authoring
  (every failed plan, every retry while fixing a spec, gets its own job ID
  and directory). Two exceptions are skipped:
  - an `apply.log` exists: an `apply --async` that refused or crashed
    before writing an envelope (the log is the only record of why, and the
    skip reason quotes its last line), or one still in its preflight (the
    log can still be empty). Read it, then delete the directory by hand;
  - a live `job apply` holds `apply.lock` (tested with a non-blocking
    `flock`, not a PID check): an apply between claiming the job ID and
    writing its first envelope.
- **`done=True` with `cleanup: released` or `cleanup: already_absent`** —
  deleted. Terminal, and the VM is confirmed gone.
- **`done=True` with `cleanup: left_up`** — skipped. The VM was left
  running deliberately and the local record is the only pointer to it.
- **`done=True` with `cleanup: failed`** — skipped. Release was not
  confirmed, which does not prove the VM is still up (for example, the
  unassign answered 404 but the assignment listing that would confirm it
  failed), but the local record alone cannot prove it is gone. The
  envelope's hints carry the release error. Run `mighty-colab sessions` (and `job status --poll <job_id>` for
  that job) before deleting by hand; if either shows a live endpoint, run
  `mighty-colab job destroy <job_id>` first, then re-verify with
  `mighty-colab sessions`.
- **An envelope that cannot be read** (truncated, or carrying a value this
  CLI cannot interpret, such as a workload state added by a newer CLI) —
  skipped, because its state is unknown. `jobs list` and the MCP job
  listings show the record with `envelope unreadable (...)`. Fields this
  CLI does not define do not make an envelope unreadable: prune classifies
  such an envelope by its `done` and `cleanup` values, like any other.
- **`done=False`** — skipped. `apply` may still be running in another
  process on this or another machine, or a prior run was interrupted
  mid-flight. Run `job status --poll <job_id>` for a live read.

A directory that cannot be fully deleted is reported as skipped with what
stopped it, not as removed.

**Known gap:** a synchronous `job apply` (without `--async`) checks the
plan, its sources and URL expiry before it claims `apply.lock`, and writes
no `apply.log`. During those seconds its job has no envelope, no lock and
no log, and `jobs prune` deletes the directory as `(planned, not applied)`;
the apply then fails to find its plan. Do not run `jobs prune` while a
synchronous apply is starting.

## Live check commands

```bash
# Preview what jobs prune would remove, without deleting anything
mighty-colab jobs prune --dry-run

# Actually prune (unapplied plans + confirmed-terminal jobs only)
mighty-colab jobs prune

# Live verdict for one job (asks the VM, not local memory)
mighty-colab job status --poll <job_id>

# Is anything on this account currently billing, independent of local records?
mighty-colab sessions

# Force teardown for a job that's known-billing or ambiguous
mighty-colab job destroy <job_id>
```

`mighty-colab sessions` is the ground truth for "is anything billing right
now" — it asks the server directly, unlike `jobs list`'s envelope-only view.
Confirm with it after any manual cleanup and after any failed `job apply`.
