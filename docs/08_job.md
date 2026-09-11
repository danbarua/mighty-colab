---
log:
2026-09-09: Draft architecture for an agent-facing `job` supervisor (`plan`/`apply`/`status`/`destroy`). Does not grow `run`. Remote `mighty_runtime` package; runner is parent of a shim that `runpy`s the entry; caller-supplied HTTPS URLs as the v0 data plane. Six adversarial reviews folded in; remaining `[open]` items and apply-time gaps are listed at the end. Not implemented.
2026-09-11: Spiked the launcher for real (`integration/spike_job_runner/`). Live CPU VM: kernel RPC returns in 3.9s, kernel IDLE, verdict read back via Contents API only, all nine exit/cancel/duplicate cases correct, clean teardown. Confirmed the descendant-escape hole (setsid grandchild outlived a `succeeded` verdict, invisible to the group scan). Caught two runner-cleanup bugs before implementation: `killpg` EPERM on an empty group killed the runner before it wrote `result.json` (spurious `unknown`), and an escapee inheriting the runner's stdout pipe blocks any reader keying off stream EOF. Token lifetime >1h, independent kernel restart, and GPU remain untested — the first is still the falsifier.
2026-09-11: **Corrected a wrong conclusion.** The 77min token spike lost contact at 61min and the first write-up called it "runtime reset under a live assignment" with a "no activity = reclaimed" hypothesis, citing a peer field report as corroboration. Both were wrong. Issue #3 (this repo, 2026-08-12) already documents the real cause: the runtime-proxy token has a TTL the CLI never refreshes, expiry returns **401/404** (so the "404 not 401, therefore not auth" reasoning was invalid), and it reproduces "at ~60 minute intervals" — this run failed at 61. `stop: ok` proved nothing: unassign uses the Gaia token on a different host than the Contents API's proxy token. The field report is evidence *against* the activity hypothesis — their job wrote continuously and still hit 404/401, recovering via `adopt`. Retracted "watchdog is life support"; the real consequence is that the supervisor's poll loop MUST refresh the proxy token (`list_assignments()` returns a fresh one and the CLI discards it). Next experiment replaced: re-adopt at first failure and retry the read, which discriminates in one ~65min session.
2026-09-11: **Falsifier settled by experiment.** `token_discriminator_spike.py` ran the same quiet workload and, at the first Contents failure (t+61min, again), re-adopted instead of concluding: the assignment was still listed, `adopt --keep-alive` returned 0, and the immediate re-read of `launch.json` succeeded. `VERDICT=TOKEN_EXPIRY` — the files were intact the whole time. The activity hypothesis is dead and the planned 75-minute A/B would have measured the wrong variable. The supervisor's poll loop MUST refresh the proxy token; that is now implemented, not just specified.
2026-09-11: **Implemented** (`src/colab_cli/job/`, `mighty-colab job plan|apply|status|destroy|list`). Four-field envelope with separate `done`/`ok`; stage+run+offload behind one kernel RPC so a dropped websocket cannot lose the run; `JobTransport` refreshes the proxy token once per 401/404 (rate-limited so routine "result.json not there yet" 404s don't re-resolve every poll). Live CPU runs: `done=True ok=True`, exit 0, VM released. Three defects found by running it rather than reading it — Contents PUT into a non-existent directory returns a bare HTTP 500 that the client attributes to the size limit (now `makedirs` first); the watchdog inherits `MIGHTY_JOB_ID` and reported itself as a surviving descendant on every job (now excluded); `retry.on` is unusable in a YAML spec because YAML 1.1 resolves a bare `on:` key to boolean true (renamed `retry.when`). Still untested: GPU session, independent kernel restart mid-run, and a real signed-URL data plane.
---

**Implemented and live-verified** (2026-09-11). `mighty-colab job plan|apply|status|destroy|list` ships in `src/colab_cli/job/`; usage lives in `docs/09_job_usage.md`. This document is the design and the evidence behind it, not a proposal. Settled contract is written as MUST. The falsifier that gated the design — whether an unattended multi-hour job can keep a verdict without a long-lived kernel execute — is **resolved**: the loss at ~61min was proxy-token expiry, not VM loss, and the supervisor refreshes the token. Remaining `[open]` items and untested surfaces are listed at the end; the largest is that the signed-URL data plane has never run against a real bucket.

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
- Mid-run `install`/`reinstall` issued by apply itself. Colab independently restarting the kernel is a different, still-open reliability case.

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

| command | mutates | analogue |
|---|---|---|
| `job plan spec.yaml --out plan.json` | no | `terraform plan -out` |
| `job apply plan.json` | yes | `terraform apply plan` |
| `job status [-j JOB]` | no | refresh |
| `job destroy [-j JOB]` | yes | `terraform destroy` |

`-j`/`--job`, never `-s`. `-s` already means session name via `resolve_session`.

Three MCP tools later: synchronous `plan`; nonblocking `apply` that returns a job id; bounded `status` snapshot (the client polls). `apply` MUST NOT be auto-exposed as a blocking MCP tool — `mcp_server.py` dispatch is synchronous.

`provision` is a **phase of apply**, not a fourth command. Warm pool later MAY be `apply --stop-after=provision`.

Apply consumes a plan file. The plan hashes the spec with URL query strings canonicalized out (re-signing the same object MUST NOT invalidate the plan). Apply MUST refuse a spec that is not that plan.

## Spec (v0)

```yaml
name: train-cls0
ignore_warnings: false
accelerator: { prefer: [A100, T4], accept_cpu: false }
code:
  kind: file | bundle | git
  entry: train.py
  args: ["--epochs", "3"]
deps: [torch==2.4.1]
data:
  - { url: "https://…", dest: /content/jobs/<id>/data/x.npy, sha256: …, size_bytes: … }
artifacts:
  - { path: /content/jobs/<id>/out/model.pt, url: "https://…", required: true }
checkpoints: /content/jobs/<id>/out/ckpt-*
budgets: { wall_clock: 3600 }   # only zero-cooperation kill in v0; stall is a warning, not a SIGTERM
control:
  result:
    put_url: "https://…"    # signed PUT; runner writes result.json here
    get_url: "https://…"    # signed GET of the same object; local supervisor/spike reads here
  log:                      # optional
    put_url: "https://…"
    get_url: "https://…"
  # both methods' expiries MUST cover retry.budget + cleanup slack
retry:
  when: [retry_same]      # `when`, not `on`: YAML 1.1 reads a bare `on:` as boolean true
  max_attempts: 3
  budget: 4h              # job-total from apply start; wall_clock is per attempt
  mode: recreate | resume
on_offload_fail: leave_up | destroy
on_run_fail: offload_anyway | skip
```

`resume` is an argv contract (`--resume <path>` we pass when checkpoint files exist). We do not invent a JAX training loop. No matching files → `recreate` or `stop`, never a lie. `mode: resume` on preempt/session_lost cannot use VM-local globs — those files died with the VM. v0: resume is same-VM only; lost-VM is always `recreate`. Checkpoint URL rotation is a later door.

**Not implemented in v0: `apply` runs exactly one attempt.** `max_attempts`/`mode` are accepted and recorded, but no retry loop exists yet. Whoever implements it MUST handle this: `recreate` wipes the job directory, and `data[].dest`/`artifacts[].path` are validated against `/content`, not against the job dir — so declared paths like `/content/out/model.pt` **survive a recreate**. Staging re-fetches and re-verifies every `data[]` entry by sha256 on each attempt, so stale inputs are overwritten and checked; artifacts are not. An artifact left by attempt 1 that attempt 2 never rewrites would be uploaded as if attempt 2 had produced it — a silently wrong result, invisible in the envelope. **Attempt N>1 MUST delete every declared `artifacts[].path` before starting the consumer.** The containment rule deliberately allows conventional Colab paths so an unmodified script is a valid job; this is the cost of that choice, and it is paid here rather than by forcing every spec to be job-id-aware.

`ignore_warnings` lives in the spec. Plan will not write `-out` while warnings exist and this is false.

`data[].dest` and `artifacts[].path` MUST resolve inside `/content/jobs/<id>/` (data root / `out/`). Plan rejects anything that canonicalizes onto `mighty_runtime/`, `result.json`, `exception.json`, `watchdog.json`, or `launch.json`.

### Budgets

`wall_clock` is per attempt, enforced by the **watchdog** (absolute epoch deadline written into `launch.json` at start). On breach: write an intent record, SIGTERM the shim's process group, grace, SIGKILL. `retry.budget` is job-total from first `apply`; an attempt is only started when remaining budget ≥ `wall_clock`.

v0 does **not** hard-kill on stall. `exec --timeout` already taught us that "stdout went quiet" murders healthy JAX/XLA. The consumer MUST NOT import us, so there is no honest zero-cooperation progress signal that isn't that wound. Watchdog still *reports* inactivity (stdout mtime, `out/` mtime, GPU util) as a non-terminal `hint` / `supervisor: degraded` warning. It does not SIGTERM. A later opt-in `mighty_runtime.pulse()` MAY turn that warning into a real stall deadline; until the consumer calls it, the field is absent, not inert-but-present. `wall_clock` is the only kill that requires nothing from their code.

## Remote process tree

A long-lived `execute_code` of the workload reintroduces the silent-kernel `--timeout` and the `jupyter-kernel-client` CPU-spin. A file heartbeat cannot unblock that wait.

A bare `Popen([python, entry])` from a short kernel call is also wrong: once that call returns, nothing that can `waitpid()` remains.

Launcher (settled shape; `[open]` items are alternatives the spike must choose):

1. Place `mighty_runtime` on disk (before their bytes). Plan rejects a bundle above a declared, tested Contents-API ceiling (chunked PUT is not unbounded — this repo has a live 250MB 400).
2. One short kernel call, **non-interactive execute path only** (`execute_code` with no output hook; the interactive/output-hook path is documented to CPU-spin past its timeout and is unsupported for launch), with an **explicit finite timeout**, not the 10s default:
   `Popen([sys.executable, "-m", "mighty_runtime.runner", entry, *args], start_new_session=True)`
   redirect runner stdout/stderr to files, return `{pid}` immediately. Kernel goes IDLE. The runner's first action is `O_EXCL` create of `launch.json` `{schema_version, pid, pgid, starttime, boot_id, attempt, deadline}`. A retried launch RPC that finds a live matching `launch.json` is a no-op that returns the recorded pid — never a second runner.
3. The runner launches `python -m mighty_runtime.shim entry *args` as its own child, **in its own session/process group** (`start_new_session=True` on this second `Popen`). The shim runs **in the consumer's process**: sets real `sys.argv`/`__file__` (the entry's real on-disk path) and **`sys.path[0]`** to the entry's directory, then `runpy.run_path(entry, run_name="__main__")` wrapped in `try/except BaseException`. On a caught exception whose mapped exit code is nonzero, it writes `exception.json` (type, message, traceback tail trimmed to the entry's frames) **before** exiting with the matching code — same `sys.exit()` / `sys.exit(N)` / `sys.exit('msg')` mapping `run.py` already has. A clean `sys.exit()` / `sys.exit(0)` is **not** an exception: no `exception.json`. The shim is ours; the entry never imports it. **[open]** A double-fork/`setsid` descendant can escape this process group. Spike must confirm whether a cgroup or `PR_SET_CHILD_SUBREAPER` is available on a Colab VM before this is a real boundary. After `waitpid`, the runner MUST verify no surviving descendants before writing `result.json` or starting offload.
4. Runner is the shim's **parent**: `waitpid`, `killpg`s the shim's **own** group only (never its own), atomically writes `result.json`, then **PUT**s that file to `control.result.put_url` (log tail to `control.log.put_url` if set). The shim/consumer process MUST NOT receive these URLs in env or argv — they stay in the runner. That PUT is **defence-in-depth for the case where the VM is genuinely gone**, not the primary answer to a failed poll: a 401/404 is usually an expired proxy token, which step 6's refresh fixes directly. Cancel intent is `cancel.json` via Contents; the watchdog signals the shim pgid. Signal death maps to `cancelled` only when that intent record exists (OOM SIGKILL is `failed`).
5. Watchdog is a **sibling**, `start_new_session=True`, booted in `run` after apply's one dependency-install restart. It enforces `wall_clock` only. Inactivity is a warning in `watchdog.json`, not a kill. Liveness of the runner is **not** `kill(pid, 0)`: it is `launch.json` identity — pid exists AND `/proc/<pid>/stat` starttime matches AND boot_id matches. Apply starts the existing TFE keep-alive daemon (same path as `colab new`); an IDLE kernel with a detached runner is exactly the prune scenario keep-alive exists to prevent.
6. Local supervisor polls `launch.json` / `watchdog.json` / `result.json` through the Contents API. It MUST NOT `execute_code` to learn whether the job is alive. Contents requests MUST have connect/read deadlines. **The runtime-proxy token is a snapshot taken at `assign` with a finite TTL (`tokenExpiresInSeconds`), and the poll loop MUST refresh it** — either periodically ahead of expiry, or on the first 401/404 by re-resolving via `list_assignments()` (which returns a fresh token on every call) and retrying once before classifying anything. Holding the assign-time snapshot loses contact with a healthy VM at ~60min, deterministically (issue #3). A 401/404 that survives one refresh-and-retry is `transport_degraded`; only an endpoint absent from the assignment list is `session_lost`.

`waitpid` never yields a Python exception type/message. Detail depends on the shim surviving long enough to write `exception.json`, which it cannot for `os._exit()`, `SIGKILL`/OOM, or a native segfault. Those still produce `workload: failed` (`exit_code`/`signal` set, `exception: null`). `workload` derives from `exit_code`/`signal`/intent; `exception` is annotation.

`workload: unknown` is the one terminal value an agent cannot act on, so it needs the tightest evidence, not the loosest:

```
unknown ≡ launch.json exists
       AND identity (pid, starttime, boot_id) matches no live process
       AND result.json is absent
       AND Contents reads of launch.json succeeded (a 401/timeout is transport_degraded, not unknown)
```

A reused PID without starttime/boot_id would report the runner alive forever. A Contents blip without this rule would report unknown on a healthy job. `launch.json` is therefore required, not polish.

The consumer does not import `mighty_runtime`. Real `__file__`, real `sys.argv`. Stdout is their log, not pulses.

## Data plane

v0: **URLs are inputs.** The caller already has GET/PUT HTTPS (signed by *their* signer, or public). `mighty_runtime` uses `urllib`. No GCS client required on the VM. Query strings MUST never appear in specs-as-logged, envelopes, history, or the pulled log.

Signed URLs are HTTP-method-specific. A URL signed for GET commonly rejects `HEAD` with 403 even though the download works. `plan` MUST NOT HEAD them, and MUST NOT classify a failed HEAD as `fix_human`.

GET inputs: probe with a tiny ranged GET (`Range: bytes=0-0`). Inspect status **before** reading the body. 206 → parse `Content-Range` for size; 200 → close without draining, size unknown (warn); 403/404 on **that GET** is `fix_human` / `fix_code` **before** `assign`. Cap the read at one byte; explicit connect/read timeouts. Plan parses signature expiry on every URL including `control.*.put_url` and `control.*.get_url`, and errors `fix_human` when `expiry - now < retry.budget + cleanup slack` (control) or `< wall_clock + stage/offload slack` (data/artifacts). Missing `control.result.put_url` or `control.result.get_url` is a plan error. Plan MUST treat them as different methods of the same object — a PUT-signed URL used as a GET is a 403, not IAM.

PUT / artifact URLs and `control.*` URLs: plan cannot prove they work without mutating the destination. Do not preflight them. Offload 403 from an expired signature is `retry_class: refresh_urls`, not `retry_same`.

Scheme MUST be `https`. Plan rejects `file://`, link-local, RFC1918. Second door, explicit, not default:

```yaml
credentials:
  type: service_account_json   # env or file; never in the consumer's env or job dir
```

The key is read only inside the runner for stage/offload `gs://` calls, lives outside `/content/jobs/<id>/`, is unlinked before `run` spawns the consumer, and is never used for assign/unassign/keep-alive. The CLI's own oauth2/ADC token is never placed on the VM.

## Plan

Never calls `assign`. Numbered diagnostics, each with `retry_class` + `hint`.

| severity | examples | effect |
|---|---|---|
| error | missing ADC scopes; sibling import in a `kind: file`; ranged-GET 403/404; `sum(data) >` anything we already know; dest escapes the job dir; URL scheme not https; signature expiry too soon | non-zero; no `plan.json` |
| warn | artifact sizes undeclared; ranged GET returned 200 so size unknown | needs `ignore_warnings: true` |

Plan says “will request A100+hm”. It cannot say “you will get one.” Quota is apply’s first contact with reality. Plan hashes code inputs (entry bytes / bundle manifest / git commit), not just the spec text.

Plan-time expiry validation is necessary but **not sufficient**: a plan file is durable and may be applied hours later. `apply` MUST revalidate every data/artifact/`control.*` URL against `now + remaining retry budget + slack` **before** `assign` — failing there costs nothing, failing after costs a VM. Revalidation checks expiry and object identity only: a *refreshed signature for the same canonical object* (scheme://host/path unchanged) is accepted and does not invalidate the plan hash; a different object does.

## Apply phases

```
plan.json
  → provision   assign + persist endpoint in envelope.json (before any other side effect)
                + place mighty_runtime + start TFE keep-alive daemon
  → install     pip/uv install pinned deps
  → restart     kernel restart — the only one apply itself performs
  → verify      deps + device, re-probed after restart so sys.modules is not stale
  → stage       recompute free disk against post-install footprint; GET URLs;
                verify size/sha256 per item as it lands
  → run         boot watchdog, start runner (launch.json O_EXCL), poll files
  → offload     PUT URLs (even on job_raised if on_run_fail: offload_anyway)
  → cleanup     destroy | left_up
```

`install` before `stage`: a bad pin is cheaper than a multi-GB transfer, and disk math after pip's wheels/caches have landed is the number that matters.

Watchdog and runner start in `run`, after apply's one restart. Intentionally reinstalling *during* `run` is out of scope for v0. **[open]** Colab can still restart or crash the kernel independently. Whether the detached runner/watchdog — and the Contents API (likely the Jupyter *server*, not the IPython kernel, unconfirmed) — survive that is unverified. Spike must add an independently-triggered restart while a workload runs. Supervisor lands on one of: polling continues, non-terminal `transport_degraded`, or `session_lost`.

Refuse, no override: `sum(data) >` post-install free; no GPU when `accept_cpu: false`; `mighty_runtime` placement failed; local scopes missing.

Warn (`ignore_warnings`): artifact sizes unknown and free disk tight; GPU RAM low vs unknown model.

Unknown `--gpu` MUST NOT become A100. Fallback walks `prefer[]` only (this walk is the one stated exception to "auto-retry is only `retry_same`"). Silent CPU is how you publish chance-level science.

Retry is classified by **reason**, not by phase:

| reason | class | default |
|---|---|---|
| bad pin / resolver 404 / ResolutionImpossible | `fix_code` | destroy, do not loop |
| package-index 429 / 5xx / timeout | `retry_same` | bounded; same VM |
| restart transport timeout | `retry_same` | bounded |
| session gone during install/restart | `retry_same` | **new** VM |
| stage GET 403/404 | `fix_human` / `fix_code` | destroy, do not loop |
| verify: wrong torch / device | `fix_code` / `retry_different` | do not train |
| run: traceback | `fix_code` | no auto-retry |
| run: wall_clock exceeded | `fix_code` | no auto-retry into the same budget |
| run: cancelled (intent file) | `do_not_retry` | |
| run: no result.json, identity dead (`unknown`) | `do_not_retry` until descendants proven dead; then `recreate` on a **new** VM | |
| run: preempt / session_lost (endpoint absent from sessions listing, not a 401) | `retry_same` | recreate (lost-VM; resume is same-VM only) |
| offload: expired signature | `refresh_urls` | not `retry_same` |
| offload: 5xx | `retry_same` | bounded |
| destroy fail | `do_not_retry` | leak; keep local state; endpoint in envelope |

A 401/timeout on Contents is `transport_degraded` with backoff, never `session_lost`, until the endpoint is proven absent.

`recreate` on the same VM (wipe job dir except `attempts/`, new runner) skips assign+pip. `recreate` on a new VM if the machine is the failure. Disk-full at epoch 3 is not `recreate` on the same disk. Each attempt stamps `attempt` into `launch.json`/`result.json`; the supervisor ignores files whose attempt is not current (same defect `execution.py` already fixed for exec-async sidecars).

## Envelope, `done`, `ok`

Three fields with closed enumerations, plus persistence. Collect/teardown MUST NOT overwrite the workload.

| field | non-terminal | terminal |
|---|---|---|
| `workload` | `pending` \| `running` | `succeeded` \| `failed` \| `cancelled` \| `unknown` |
| `offload` | `pending` \| `running` | `ok` \| `skipped` \| `not_required` \| `failed` |
| `cleanup` | `pending` \| `running` | `released` \| `already_absent` \| `left_up` \| `failed` |
| `supervisor` | `running` \| `degraded` | `finished` \| `interrupted` |

`supervisor` is the fourth conjunct `done` actually needs — a filesystem path is not a value. Persist `endpoint` in `envelope.json` **immediately on assign**, before any later side effect. If apply dies, `job status` MAY observe a dead supervisor pid and set `supervisor: interrupted`, `workload: unknown` only under the identity rule above, `cleanup: left_up`.

`done: true` iff `workload`, `offload`, `cleanup`, and `supervisor` are all terminal. Process exit is not `done`. Any value outside the enumerations is a protocol error.

```
ok ≡ workload=succeeded
   ∧ offload ∈ {ok, not_required}
   ∧ cleanup ∈ {released, already_absent, left_up}
```

`not_required` is "spec declared no artifacts." `skipped` is "artifacts were declared and we chose not to upload." Partial artifact success is a per-item array in the envelope; the scalar `offload` is failed iff any `required: true` item failed.

`left_up` keeps billing. Envelope MUST carry `endpoint` and `hint: job destroy when done poking`.

Every envelope also carries `phase`, `retry_class`, numbered `hint`s, `requested` vs `actual` accelerator, `next_poll_after` seconds, artifact hashes when offload ran.

Local job dir (durable across agent death):

```
~/.config/colab-cli/jobs/<id>/
  spec.yaml            # query strings stripped; raw URLs in a 0600 sidecar
  plan.json
  envelope.json
  log                  # whole-file replace; Contents client has no range GET
```

Remote job dir:

```
/content/jobs/<id>/
  mighty_runtime/
  entry + bundle
  launch.json          # O_EXCL, identity + attempt + deadline
  watchdog.json
  result.json          # atomic, stamped with attempt
  exception.json       # optional
  cancel.json          # optional intent
  data/
  out/
  attempts/<n>/        # prior result.json moved here on resume/recreate
```

`<id>` is `spec.name` plus a short plan-hash, never the bare spec name (avoids `StateStore` overwrite-by-name).

## Testing strategy

Contract tests first (no VM): plan diagnostics; apply refuses a mismatched plan hash; envelope `done`/`ok` truth table including non-terminal rows and `offload: not_required`; URL query strings **and** `credentials` values absent from spec-as-logged, envelope, history, and log; unknown GPU name is an error; plan never issues `HEAD`; dest outside the job dir is a plan error; missing/short-lived `control.result.put_url` or `get_url` is a plan error; watchdog inactivity is a warning, never a SIGTERM.

Live spike (CPU, then one GPU), always `unassign` before done (`AGENTS.md` #10, #22):

1. Place `mighty_runtime`, short-launch runner of a silent 90s script, confirm the kernel RPC returns in ~1s on the non-interactive path. Assert `launch.json` exists with pid/starttime/boot_id.
2. Poll `watchdog.json` / `result.json` via Contents API while the kernel is IDLE. Keep-alive TFE pings running.
3. Exit 0 vs raised exception (`exception.json` present) vs `os._exit()`/`SIGKILL` (`exception: null`, `signal` set, `failed`) vs runner crash matching the `unknown` identity rule. A clean `sys.exit(0)` asserts `exception.json` absent.
4. Cancel via `cancel.json`; consumer dies; runner survives and writes `result.json` with `cancelled`. Assert a SIGKILL *without* `cancel.json` is `failed`, not `cancelled`.
5. Ranged-GET 403 on a data URL in plan does not `assign`. A GET-signed URL that would 403 on HEAD still plans if the ranged GET succeeds.
6. **Token lifetime — the refresh path is the thing under test.** One job that idles past the runtime-proxy token's documented ~60min expiry (issue #3). Assert: (a) the poll's 401/404 is classified `transport_degraded`, **never** `session_lost`; (b) the supervisor re-resolves the token (`list_assignments()` returns a fresh one on every call) and the **next poll succeeds against the same still-intact files** — proving the earlier "filesystem vanished" reading was a credential artefact; (c) only then, that `result.json` is also readable from `control.result.get_url` as defence-in-depth. Do not GET the PUT URL. A spike that needs the control URL to recover a verdict has found a **missing token refresh**, not a vindicated durable-push design.
7. Independently `restart-kernel` while a workload runs. Record whether runner, watchdog, and Contents API survive. Do not treat the result as a v0 requirement either way — pick the supervisor response from the `[open]` list above.
8. Consumer double-forks a sleeper. Assert the runner does not report a clean terminal state while descendants hold the GPU.

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

Detection has an implementation that needs no cgroups, **not yet confirmed on
Linux**: the runner exports `MIGHTY_JOB_ID=<id>` into the shim's environment,
and the sweep matches any process whose `/proc/<pid>/environ` carries it. An
escapee leaves the process group (`setsid`) and is reparented to init, so pgid
and ppid walks are both structurally blind — but **a child cannot shed the
environment it inherited**. `result.json` reports `survivors_by_pgid`,
`survivors_by_job_tag`, and `escapee_detection_available` separately, so a
platform that cannot answer (no `/proc`) reports *unknown* rather than falsely
reporting *nothing survived*.

Status: the sweep has only ever executed on macOS, where it correctly returns
"cannot answer". **It has never run on Linux**, which is the only place it can
actually work. Unlike the table above, this is not a live-verified claim —
re-run `live_spike.py` after the token spike finishes to confirm it, and treat
it as unproven until then.

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

`VERDICT=TOKEN_EXPIRY`. **The files were intact all along.** `/content` never
vanished, the VM was never recycled, and the only thing that had died was the
credential. Failure at 61 minutes on both runs, matching issue #3's documented
~60-minute reproduction interval.

This kills the activity hypothesis outright: the discriminating variable was
never write-activity, and the originally-planned 75-minute A/B would have
measured nothing. One ~65-minute session answered it for the cost of a CPU box.

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

**Next experiment — discriminating, cheap, one session (~65min), replaces the
A/B.** Run the same quiet workload; at first Contents failure, run
`adopt <ENDPOINT> --keep-alive` (which re-resolves the assignment and rewrites
session state, refreshing the proxy token the CLI uses) and immediately retry
the same read. Files reappear → token expiry, confirmed, and the A/B on
write-activity was testing the wrong variable. Files still absent after a
successful re-adopt → the filesystem really did go, and the activity hypothesis
is back on the table. Record the raw HTTP status either way.

**The experiment MUST NOT implement the token refresh it is testing for.** The
refresh belongs in the supervisor contract (launcher step 6); a spike that
auto-refreshes never reaches the failure and observes nothing. Let it break,
then `adopt`, then retry.

- **Independent kernel restart** mid-run (step 7). Untested.
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
  process (its runner enforces `wall_clock` directly) and no data plane; the
  shipped implementation has both a watchdog and `control.*` PUT, but the
  signed-URL data plane has still never been exercised against a real bucket.

## Known gaps

Apply-implementation-time, not spike-blocking. Real tradeoffs, not polish.

**Envelope / supervisor death:** apply dying between provision and run still needs the supervisor field + persist-before-spawn (stated above; implement).

**Data plane leftovers:** redaction as a type (`SignedUrl` whose `__str__` strips query), not a comment; PUT `Content-Length` + streamed body; `job offload <id>` re-drive with fresh URLs.

**Credentials:** contract test that the consumer's `os.environ` and readable files contain no SA key (rule stated above; test not written).

**CLI integration:** ownership record so `adopt`/`stop` refuse a job-owned endpoint; account-scoped in-flight assignment limit; keep-alive daemon is required in provision (stated) but its `left_up` handoff is unspecified.

**Transport leftovers:** Contents whole-file GET (log is replace-not-append until a chunk scheme); bundle size ceiling at plan (enforced at 250MB, but that bound is inherited from one live 400 and has not been measured precisely). Contents request timeouts and the proxy-token refresh are now **implemented** (`src/colab_cli/job/transport.py`): explicit connect/read deadlines, one refresh-and-retry per 401/404, rate-limited to one assignment re-resolve per 60s so a routine "result.json isn't there yet" 404 does not re-resolve on every poll.
