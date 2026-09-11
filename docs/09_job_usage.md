---
log:
2026-09-11: First version. Usage guide for `mighty-colab job`, written for an agent (or a human) running real science unattended. Design rationale lives in `docs/08_job.md`; this file is how to drive it.
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
outcome is read back by polling files over the Contents API. Dropping the
connection at any point after launch costs you nothing but the poll.

That is the whole idea. Everything below is detail.

## Four commands

```bash
mighty-colab job plan    spec.yaml          # free. validates. allocates nothing.
mighty-colab job apply   --job-id <id>      # the only step that spends money
mighty-colab job status  <id>               # asks the VM, not local memory
mighty-colab job destroy <id>               # unconditional. safe to run twice.
```

`plan` is free and side-effect-free, deliberately: iterate on a broken spec as
many times as you like before anything bills. Under `--json` every one of these
emits a validated envelope.

## A minimal spec

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
you locally. Sibling imports work. `if __name__ == "__main__":` works.

Your exit code is the verdict. Raise to fail, return 0 to succeed.

**A silent job is not a suspicious job.** Liveness is observed from outside by
a watchdog process; you are never punished for not printing. This is why there
is no stall timeout — "stdout went quiet" is exactly what a healthy JAX or XLA
compile looks like, and killing on it is how you lose good runs.

## Reading the result

Four fields, and they are orthogonal on purpose:

| field | what it answers |
|---|---|
| `workload` | did your code succeed, fail, get cancelled, or is it unknown |
| `offload` | did your declared artifacts get uploaded |
| `cleanup` | was the VM released |
| `supervisor` | is anything still driving this job |

Plus two derived booleans:

- **`done`** — all four are terminal. Nothing is still moving.
- **`ok`** — `done` *and* it actually worked.

`done` is the one to poll on. `ok` is the one to branch on.

Keeping these apart matters in the two cases that cost real money or real
science:

- `workload: succeeded` + `cleanup: failed` → your results are fine and **a VM
  is still billing**. `done` is true; `ok` is false. Run `job destroy`.
- `workload: failed` + `cleanup: released` → a perfectly healthy failure. Your
  code has a bug; nothing is leaking.

A single status field cannot say either of those things.

### `retry_class` tells you what to do next

Never guess from the message text:

| value | meaning |
|---|---|
| `fix_code` | your spec or your script is wrong. Retrying unchanged will fail again. |
| `fix_human` | credentials, quota, or grants. No amount of code editing helps. |
| `retry_same` | transient. The same spec on a new VM should work. |
| `retry_different` | the accelerator or shape was the problem. Ask for another. |
| `refresh_urls` | your signed URLs expired. Re-sign and re-plan. |
| `do_not_retry` | retrying will make it worse. |

## Data and artifacts

Do **not** upload a dataset through the Contents API. It is base64 inside JSON
and there is a live 250MB ceiling; `plan` will refuse a bundle above it and
tell you to use a URL instead.

Put your data in GCS, sign a URL, and declare it:

```yaml
data:
  - url: https://storage.googleapis.com/bucket/x.npy?X-Goog-Signature=...
    dest: /content/data/x.npy
    sha256: 7d79...          # optional, and you want it
artifacts:
  - path: /content/out/model.pt
    url: https://storage.googleapis.com/bucket/runs/model.pt?X-Goog-Signature=...
    required: true
```

The VM pulls and pushes these itself. The URLs never enter your process's
environment and are never written to the session log.

**Artifacts are uploaded even when your run fails.** A crashed job's last
checkpoint is usually the thing you most want, and `on_run_fail:
offload_anyway` is the default.

`sha256` is worth the trouble: it is the only thing that distinguishes your
dataset from a truncated copy of your dataset, and a silently truncated input
produces a result that looks plausible and is wrong.

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

**Escaped descendants.** If your code spawns something with `setsid`, it can
outlive the run and keep holding the GPU. The runner detects these by scanning
for its own job tag and reports them in `surviving_descendants`. Detection is
not containment: it tells you, it does not currently kill them.

## If the supervisor dies

Closing the laptop does not kill the job — that is the point. The VM keeps
running and `supervisor` becomes `interrupted`. Reattach with:

```bash
mighty-colab job status <id> --poll
```

The VM is deliberately left up in this case, and the envelope says so, because
silently tearing down a run you are still paying for would be worse.

## Cost discipline

Teardown is not a phase the job reaches, it is how the job leaves — including
when it fails early at `verify`. But two cases deliberately leave a VM running:
an interrupted supervisor, and `on_offload_fail: leave_up` (so you can rescue
artifacts that failed to upload). Both report `cleanup: left_up` and carry the
endpoint.

When in doubt:

```bash
mighty-colab job destroy <id>    # exits 0 even if already gone
mighty-colab sessions            # the real answer about what is billing
```

## Known gaps

Be aware of these before trusting a long run:

- **GPU sessions are untested end-to-end.** Every live run so far has been CPU.
- **Independent kernel restart mid-run** is untested.
- **The signed-URL data plane has not been exercised against a real bucket.**
- Log offload is whole-file replace, not append.
- Detected escapees are reported, not killed.

Please report what breaks — the failure modes above were all found by running
the thing, not by reading it.
