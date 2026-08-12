# Driving Colab from an AI coding agent: what broke and what we changed

Interview prep. Every claim below cites a commit, a file, or a test. The two
claims that depend on upstream's current state were checked against the live
repository on 2026-08-07 and are marked SETTLED; anything still open is marked
**[VERIFY]** with the check to run.

Two repositories are involved:

- `mighty-colab` — a fork of Google's official `google-colab-cli`, the CLI
  under discussion. Currently `v0.2.2`.
- `bonsai-2026` — an oscillator-network ML research project that used it for
  every GPU run: A100 sessions, artifacts to GCS, real billing.

---

## The opening answer

We ran a multi-month research project where an AI agent, not a human, was the
thing typing `colab` commands — provisioning A100s, uploading data, running
hour-long jobs, tearing sessions down. The CLI is good. But it was designed for
a human who can see a terminal, notice a spinner, and remember to clean up. An
agent has none of that peripheral vision, so every place where the CLI conveys
information *typographically* rather than *structurally* became a real failure
with a real bill attached.

We forked it and fixed sixteen defects in the upstream code — most of them
agent-relevant in a way a human user would rarely notice — plus five in our own
additions. Fourteen land as a commit literally named `test: reproduce <the bug>`
followed by the fix. We added three commands the upstream doesn't have. And on
the consumer side we ended up writing an executable specification of what an
agent needs from this CLI — a test suite that drives our real build recipes
against a stub CLI and asserts their failure behaviour
(`bonsai-2026/tests/test_mighty_colab_contract.py`). That file is the most
useful artifact from the whole exercise, because it is what a "Colab CLI agent
contract" would look like if one existed.

The three sentences that carry the most weight:

1. **A CLI that exits 0 while the work failed is invisible to an agent** — and
   even a correct exit code cannot distinguish "ran and passed" from "exited
   before reaching its verdict," which is why every one of our recipes greps for
   a sentinel string the script prints itself.
2. **`exec -f` transmits file *text* into a live IPython kernel**, so `__file__`
   is undefined and nothing from your repository is on disk. That is documented
   behaviour, not a bug — and it still crashed a run on a billing A100, because
   no local check reproduces that execution model.
3. **`exec --timeout` defaults to 30 seconds and bounds the gap between
   *outputs*, not the run.** Three of our GPU targets had never once completed
   as written.

---

## Contents

- [Provenance: what is Google's, what is ours](#provenance)
- [Verify before the interview](#verify)
- [The four strongest stories](#stories)
- [The fixes, with attribution](#fixes)
- [The extensions, and why an agent needed each](#extensions)
- [What the CLI got right for an agent](#what-worked)
- [Agent-specific failure modes](#failure-modes)
- [What we'd have hit anyway vs. what we did to ourselves](#self-inflicted)
- [What we'd ask Google for](#asks)
- [Not Google's problem](#not-googles-problem)
- [Citation index](#index)

---

<a name="provenance"></a>
## Provenance: what is Google's, what is ours

**`mighty-colab` is a fork of `github.com/googlecolab/google-colab-cli`.** The
full upstream history is present in our git history, so attribution is
mechanically checkable rather than remembered.

- **Merge base with upstream: `1005593`** (Tyler, 2026-07-30, "docs: update
  AGENTS with release instructions (#66)"). Everything at or before that commit
  is upstream's. Everything after is ours.
- `CHANGELOG.md`'s "Google CoLab CLI Change Log" section says the fork was taken
  at `v0.6.0`. That is the last upstream *CHANGELOG entry* (2026-06-16), not the
  fork point in the git history. The tree carries
  post-0.6.0 upstream work: `colab ssh` (`c129cbf`, #88), `--env KEY=VALUE`
  (`507d169`, #65), and dual `ColabKernelClient`/`KernelClient` support
  (`604aac1`, #95). **Use `1005593` in the room, not `v0.6.0`.**
- First commit of ours: `5766b79` (2026-08-02). The rename to `mighty-colab` is
  `f73376d`.
- Upstream is authored by Google employees (`sethtroisi@google.com` wrote the
  initial commit `2ef9825`, 2026-05-11) plus community PRs.

**Three layers, kept distinct:**

| layer | what it is | examples |
|---|---|---|
| Google's CLI | everything at or before `1005593` | `exec`, `run`, `install`, keep-alive, `--timeout`, `--env`, `ssh`, the `skill` command |
| Third party, Google-owned | `jupyter-kernel-client`, pinned to a git URL in `pyproject.toml:79` → `github.com/googlecolab/jupyter-kernel-client` | the execution transport; the CPU-spin bug below lives here |
| Ours | everything after `1005593` | `adopt`, `mcp`, `reinstall`, chunked upload, sixteen bug fixes |

**Upstream features we lean on heavily and must not claim as ours:**

- `--env KEY=VALUE` on `exec`/`run` — `507d169` (Matt Van Horn, #65). Our entire
  GCS credential-passing scheme (`bonsai-2026/Makefile`, the `GCS_EXEC_ENV`
  variable) depends on it.
- `--timeout` on `exec`/`run` — `96ef983` (Xiaoquan Kong, #38); default raised
  from 10s to 30s by `889d09f` (Seth Troisi, #43).
- `colab run`'s native-Python semantics: `sys.argv` and `__name__ ==
  "__main__"` are set in the prelude (`src/colab_cli/commands/run.py:100-111`),
  and CPython exit-code conventions for `sys.exit()` are honoured.
- Suppressing inline terminal-image escapes when stdout isn't a TTY
  (`docs/02_execution_and_interactive.md`, 2026-05-07). This one matters
  rhetorically — Google already set the precedent that terminal affordances
  should stand down for a non-TTY consumer. Several of our asks are just
  "extend that principle."
- The `skill` command and `SKILL.md` (`027e821`, `0e22a02`). Upstream shipped an
  agent-facing skill document *first*. We extended it by 19 lines
  (`git diff 1005593 HEAD -- skills/colab-operator/SKILL.md`), we did not
  invent it.

**A note on tone for the room:** upstream's `CONTRIBUTING.md` — unchanged from
`1005593`, and re-checked live on 2026-08-07 — says "we aren't accepting
external contributions at this time" and points at Discussions. That is why
sixteen fixes, fourteen of them reproduce-first, live in a public fork instead
of in Google's tree. It is the single highest-leverage thing on the ask
list, and it costs Google nothing but process.

---

<a name="verify"></a>
## Verify before the interview

Claims that depend on something outside these two repositories. Two are now
settled; do not state the remaining ones flatly without running the check.

Both settled items were checked on 2026-08-07 against the live upstream
repository. Re-check on the morning of the interview; the commands are given.

**SETTLED: upstream `main` has not moved since the fork point.**

```
$ git fetch --no-tags https://github.com/googlecolab/google-colab-cli.git main
$ git log -1 --format='%h %an %ad %s' --date=short FETCH_HEAD
1005593 Tyler 2026-07-30 docs: update AGENTS with release instructions (#66)
$ git log --oneline 1005593..FETCH_HEAD
(no output)
```

Zero commits in the eight days since. So every one of the sixteen defects below
is **still live upstream**, and that can be said flatly. (The fetch reads into
`FETCH_HEAD` and mutates no git config.)

**SETTLED: upstream still refuses external PRs.** `CONTRIBUTING.md` on `main`,
fetched 2026-08-07, is verbatim what we forked: "we aren't accepting external
contributions at this time… please share it on our discussions page."

Still open:

1. **Whether `googlecolab/jupyter-kernel-client` still has issues disabled**,
   and whether `google-colab-cli#82` is still open. `07479f7`'s commit message
   asserts both as of 2026-08-05.

2. **Whether `exec --timeout`'s default is still 30s.**
   `bonsai-2026/tests/test_mighty_colab_contract.py`'s
   `test_exec_default_timeout_is_short_enough_to_need_overriding` reads it from
   `mighty-colab help exec` at runtime, so running that test answers it.
   (Upstream is frozen, so this can only have changed in our own fork.)

3. **The stale skill copy.** **[✅ Resolved, verified 2026-08-11 — no commit
   hash here, the fix landed as a file sync in `bonsai-2026`, not a commit in
   this repo]** `bonsai-2026/.claude/skills/mighty-colab/SKILL.md` now reads
   byte-identical to the canonical `skills/colab-operator/SKILL.md` (`oauth2`
   correctly stated as the default, plus everything else added since,
   including the `mighty-colab` rename and the `exec-async` docs). Originally:
   still said `--auth` defaults to `adc`. It defaults to `oauth2` — an
   interactive browser flow — and we corrected the canonical copy in `a070161`.
   The wrong value sat in a document whose own heading calls authentication
   "the #1 thing that blocks agents." Left uncorrected deliberately at the
   time (that repo was out of scope for this write-up).

---

<a name="stories"></a>
## The four strongest stories

### 1. The driver that crashed on a billing A100 because there was no file

**What happened.** The Stage 2B ladder stage-2 driver crashed at module scope,
before its own `main()` ever ran: `NameError: name '__file__' is not defined`.
A refactor had imported a constant from a sibling driver using
`os.path.dirname(os.path.abspath(__file__))` to locate it.

**Why it happened.** `mighty-colab exec -f script.py` reads the file *locally*
and transmits its **text** into an existing IPython kernel cell. The script is
never run as a script and never imported as a module, so `__file__` is never
defined. Compounding it: nothing from the repository exists on the kernel's
filesystem at all until the driver's own `bootstrap_repo()` clones it — which
happens *inside* `main()`, after every module-scope statement has already run.
Module scope under this execution model can rely on stdlib and numpy, full stop.

**This is not a bug.** The CLI does exactly what `README.md` says under
"Transparent Code Execution" and what upstream's `docs/02` design doc specifies:
"If file path is local: Read content, send as code." The failure is a **model
mismatch** — the caller reasonably assumed script semantics because the flag is
spelled `--file`.

**Why no local check caught it.** Ordinary verification imports the driver as a
real Python module, and Python's own import machinery sets `__file__` correctly.
The bug was invisible to every local check *except* one that reproduces the
actual execution model.

**What we built.** Two permanent guards: a static check that neither driver
references `__file__` anywhere, and a dynamic check that `compile()`+`exec()`s
each driver's real source into a namespace with **no `__file__` key**
(`bonsai-2026/tests/test_stage2b_ladder_stage2.py`).

**The asymmetry that makes this an actionable ask.** `colab run` already builds
a prelude setting `sys.argv` and `__name__ = '__main__'`
(`src/colab_cli/commands/run.py:104-108`). `colab exec -f` builds no such
prelude (`src/colab_cli/commands/execution.py:254` — env vars only). Neither
sets `__file__`. So the request is small and precise: give `exec -f` `run`'s
prelude, and have both set `__file__` to something honest.

**Cost.** Session provisioning and package-install overhead only. Confirmed
against the live bucket and session list before any fix was written — zero
objects under `stage2b/train/stage2/`, no leaked session.

**Sources:** `bonsai-2026/experiments/stage2b_denoising/FINDINGS.md:378-402`;
`bonsai-2026/docs/PROJECT_MEMORY.md:647`;
`bonsai-2026/experiments/stage2b_denoising/run_ladder_stage1.py:31-38`.

---

### 2. Fixing an exit code broke the thing that had adapted to it — and leaked an A100

**What happened.** On the 0.1.x line, `mighty-colab exec` exited 0 even when the
remote script raised an uncaught exception. We fixed that in `679c0b6` (shipped
in `v0.2.0`).

**What the fix broke.** Our Makefile recipes were written as
`exec && download && stop`. That chain tore the session down on every path
*because* `exec` always returned 0. Once `exec` could fail, the chain **skips
the teardown** and leaves a billable A100 running. Three Stage 2A targets were
written that way.

**Why it's the best story in the set.** It is not "Google shipped a bug." It is:
a dependency fixing a bug can break code that had silently adapted to it, and
the adaptation is invisible at the call site. Upgrading needs a check of what
the *old* behaviour was load-bearing **for**, not just that the new behaviour is
correct. And in this instance we were both the fixer and the victim.

**What we built.** Every GPU recipe now captures the status, tears down
**unconditionally**, then propagates
(`bonsai-2026/Makefile`, `stage2a-evolve-train-gpu` and four more). A
`check_teardown` macro distinguishes the two outcomes that mean opposite things:
"already absent" (nothing is billing — the goal) from "could not stop" (money is
accruing unwatched — the only outcome worth failing an otherwise-successful
target for). `STOP_ABSENT_RC` names whichever exit code means
absent, so a future CLI that separates them needs one variable changed rather
than five recipes rewritten.

**And it's pinned.** All four paths — healthy, leak-only, leak-plus-failure,
distinct-absent-code — run against a stub CLI in
`bonsai-2026/tests/test_mighty_colab_contract.py` (`test_healthy_run_exits_zero`,
`test_teardown_failure_fails_an_otherwise_successful_target`,
`test_a_leak_never_masks_the_scientific_verdict`,
`test_a_distinct_absent_code_can_be_declared_without_rewriting_recipes`, and
their `stage2b-ladder-stage1` counterparts). No session,
no billing.

**Sources:** `bonsai-2026/docs/PROJECT_MEMORY.md:589-627`; `679c0b6`;
`bonsai-2026/Makefile` (`EXEC_TIMEOUT`, `STOP_ABSENT_RC`, `check_teardown`).

---

### 3. Three GPU targets that had never once completed, because of a 30-second default

**What happened.** `make stage2a-class0-classify-gpu` was run as a Makefile
target for the first time on 2026-08-05. It died 30 seconds in, on the third of
six input downloads, with `TimeoutError: Timeout waiting for output`.

**Why.** `mighty-colab exec --timeout` defaults to **30 seconds**, and it bounds
the **gap between outputs**, not the run. A remote script that is computing
normally but not printing dies. Our drivers go quiet for far longer:
`stage3_gpu_evolve.py` prints once per topology; the class-0 driver prints once
per download and then not at all through several minutes of cuML fitting.

**The deeper failure.** The target had been *codified from a hand-run
`mighty-colab exec` session*, and `--timeout` was never carried across. So it
had never once completed as written. The two Stage 2A evolve targets had the
same omission. A command sequence that produced real results when run by hand,
frozen into a target nobody ran end to end.

**What we built.** `EXEC_TIMEOUT ?= 3600` (`bonsai-2026/Makefile`), passed
explicitly by every GPU recipe, plus a test that **fails if any future recipe
omits it** — and which derives the set of recipes from the Makefile rather than
from a hand-maintained list
(`test_every_exec_passes_an_explicit_timeout`, which derives the recipe set by
parsing the Makefile). A second test reads the
CLI's *documented* default at runtime and asserts it is still short enough to
need overriding (`:368`), so the rationale can't go stale silently.

**And the tell for the ask.** Our drivers now print a heartbeat line every 30
seconds (`run_ladder_stage1.py:117, 156-166`) whose only purpose is to keep the
transport alive. That is a workaround for a missing distinction: **wall-clock
budget and inactivity budget are different things and want different flags.**

**This is also where CLAUDE.md principle 21 came from** — "a hand-maintained list
standing in for a derivable set will silently under-cover"
(`bonsai-2026/CLAUDE.md:360-365`, which cites these three targets by name).

**Sources:** `bonsai-2026/docs/PROJECT_MEMORY.md:629-645`;
`bonsai-2026/tests/test_mighty_colab_contract.py`, module docstring item 2.

---

### 4. The downstream consumer's test suite became the CLI's design authority

**What happened.** We noticed `status -s NAME` and `stop -s NAME` were the only
two session-targeting commands that printed "not found" to stdout and exited 0,
while `exec`/`repl`/`ls`/`rm`/`upload`/`download`/`edit`/`url`/`ssh` all treated
a missing session as an error. We changed both to exit non-zero (`c76d621`).

**Then we reverted both, for different reasons.**

- **`stop`** was reverted in `cf8d1ea`, and the commit message names the reason
  in the CLI repo: *"in downstream bonsai tooling, `stop` runs unconditionally
  on every teardown path, including after a `new`/`exec` that failed before
  creating anything — exactly the case where 'not found' is the strongest
  available evidence nothing is billing. Erroring there would invert that signal
  (a clean no-op reads as a leak warning)."* `stop` is a desired-state
  operation, not a query. Same reasoning as `rm -f`, `kill … || true`, and
  DELETE's idempotency.
- **`status`** was reverted in `e3cd8e1` for an honesty reason rather than a
  technical one: the original bug report never named a command, and changing
  `status` was an inference nobody had asked for.

**The precise claim to make.** Not "our test caught a breaking change" — read
`9987ee4`, which is explicit that the guard would *not* have broken: it greps
for "not found" inside an `if` (so a non-zero exit doesn't break it) and merges
stderr into what it greps (so the message moving streams doesn't either). The
defensible claim is stronger and more interesting: **a downstream consumer's
executable expectations were the artifact consulted to settle a CLI design
question, and they were legible enough to settle it.** The contract test made
the consumer's assumptions readable by someone who wasn't the consumer.

**What that suggests Google could have.** A published contract test — "here is
what the CLI guarantees a program calling it" — versioned alongside the CLI, in
the same spirit as the `skill` command upstream already ships.

**The file is alive, which is the point.** It has kept growing as the pipeline
changed: the pre-flight refusal that once rejected any dirty working tree is now
keyed to the *driver's own import closure*, with a test in each direction (a
dirty closure must refuse; a clean closure inside a dirty tree must run), plus
one asserting the coarse whole-tree gate has not come back and one that
**derives** the set of commit-pinning recipes and requires each to run the
closure check. A contract that only ever accumulated assertions would be a
liability; this one is edited when the contract genuinely changes.

**Sources:** `c76d621`, `cf8d1ea`, `94f27a6`, `9987ee4`, `e3cd8e1`;
`bonsai-2026/tests/test_mighty_colab_contract.py`, module docstring and
`test_status_of_unknown_session_exits_zero_on_stdout`.

---

<a name="fixes"></a>
## The fixes, with attribution

**Attribution method** (worth stating in the room, because it makes the list
credible): for each fix, the file was checked for existence at merge base
`1005593`, checked for whether we had touched it between the fork and the fix,
and the buggy construct was read directly out of `git show 1005593:<file>`. Not
recollection.

**Reproduce-first discipline.** Fourteen of these land as an explicit
`test: reproduce X` commit followed by `fix(…): X` — plus `8eb5212`, which does
the same thing under a different verb. Grep the log for it:
`git log --oneline --grep="^test: reproduce"`. The pairs are in the tables below.

### Upstream defects we hit and fixed

Each verified present in the code at `1005593`.

| # | defect | how it manifests to an agent | fix | red test |
|---|---|---|---|---|
| 1 | `exec` exits **0** when the remote script raises — errors were streamed to stderr but never inspected | the primary signal an agent has is a lie; `$?` says success | `679c0b6` | `5ab4698` (live integration) |
| 2 | piped `repl` has the same bug | same, on the other non-interactive path | `eff46f1` | `0dfc453` |
| 3 | `colab run`'s `_teardown` swallows an `unassign` failure **and deletes local session state anyway** | a possibly-billing VM, with the local record needed to retry `stop` destroyed | `b934507` | `0ce5827` |
| 4 | `sys.exit(False)` mis-mapped to exit code 1 (`int("False")` raises) | a successful run reported as failed | `e19db40` | `e273ba2` |
| 5 | `auth`/`drivemount`/`install`/`reinstall` each start a **new, untracked kernel** never shut down by anything, including `colab stop` | repeated `install` calls accumulate orphaned kernels on the VM indefinitely | `7eb22fd` | `6311e52` |
| 6 | `install` exits 0 on failure; the `uv`→`pip` fallback retried *every* failure, chaining two tracebacks | an agent installs a nonexistent package and proceeds | `cc77552` | `8eb5212` |
| 7 | package names interpolated into generated remote code as `'{c}'` — a name containing a quote corrupts the source | silent code corruption from a data-dependent input | `701fe65` | `a0d9a33` |
| 7a | *(same defect, our exposure)* the buggy `cmd_str` helper is upstream's, but our `reinstall` shares it — the bug is Google's, the second caller is ours | — | — | — |
| 8 | same for `drivemount`'s mount path | same | `9ead1eb` | `84fa5d3` |
| 9 | `edit` treated **any** download failure (auth, network, 5xx) as "file doesn't exist yet" | an empty buffer silently overwrites real remote content | `1a68c79` | `12e81f2` |
| 10 | session history JSONL reads/writes unlocked — the one piece of shared on-disk state with no locking | concurrent agent processes corrupt history | `aa2cd4c` | `ad5120b` |
| 11 | kernel-client startup retry discards the partially-started client without closing it | leaked websocket per retry | `b5f820d` | `24d450e` |
| 12 | a non-terminal error in `exec`/`repl`'s `/content` pre-flight propagates without stopping the runtime | leaked connection on the error path | `f2e1528` | `d523f62` |
| 13 | `restart-kernel` on an unknown session crashes with a raw `AttributeError` — the only session-targeting command missing the guard | a traceback where every sibling command gives a clean message | `33fb409` | `528ccb1` |
| 14 | error and "not found" messages printed to **stdout** in `ls`/`rm`/`upload`/`download`/`edit`/`exec`/`repl`/`console` | a program parsing stdout gets error text mixed into results | `af85dc9` | `a82f1e3` |
| 15 | `stop`'s `unassign` failure propagates as a raw Python traceback, and local tracking is dropped | the operator sees a traceback instead of "this may still be billing"; retry path gone | `94f27a6` | — |
| 16 | uploads over ~1MB issued as a single request, hitting an HTTP 500 from a request-size limit somewhere in the backend stack | large artifacts simply fail | `a6d4c75` (real chunked protocol), `a5b4722` (hint on 500) | `900bc79`, added after the fix — a live 50MB/160MB byte-exact check, not a red test |

**All sixteen are still live upstream**, verified 2026-08-07: upstream `main`
is still at `1005593` and has had no commits since 2026-07-30. See
[Verify before the interview](#verify) for the fetch.

Row 14 covers two files. `files.py` was untouched by us between the fork and
the fix. For `execution.py`, the three `typer.echo(f"[colab] Session '{name}'
not found.")` sites the fix moved to stderr are present verbatim at `1005593`
(lines 172, 303, 389, none carrying `err=True`) — our earlier edits to that file
touched other regions. Both halves are upstream's.

### Upstream defect documented but not fixed

- **`exec --timeout N` pegs a local CPU core at 100% and never exits**, even
  though the remote kernel and VM are fine. Root-caused to the vendored
  `googlecolab/jupyter-kernel-client` fork: once the deadline passes,
  `execute_interactive()`'s wait loop clamps to a 0-second timeout and spins
  forever with no deadline-exceeded exit. Confirmed live in the vendored source,
  not just from the linked report (`googlecolab/google-colab-cli#82`).
  Documented in the operator skill rather than fixed, because it is third-party
  code **and that fork has issues disabled**. Remedy: `kill -9` the local
  process; the remote session is untouched and reattachable.
  `07479f7`; `skills/colab-operator/SKILL.md:100`.

  This is a *process* complaint, not a code one: the defect is in a Google-owned
  fork of a third-party library, published as a git dependency, with no channel
  to report it. An agent is exactly the caller least able to notice a locally
  spinning process.

### Defects in our own additions — ours, not Google's

| defect | fix | red test | note |
|---|---|---|---|
| chunked-upload loop's only exit condition compared bytes sent against a pre-loop `file_size` snapshot; a file that shrank mid-upload spun forever sending empty PUT chunks | `914e3df` | `94dd049` | bug in **our own** `a6d4c75`. `contents.py` history is four commits: upstream's original, our two, and this fix. |
| `adopt --keep-alive` on refresh spawned the daemon **before** persisting state, so a crash mid-spawn left the PID untracked | `3a03175` | `4f0bb6b` | `adopt.py` did not exist at `1005593` |
| `adopt NAME` silently repointed a name already tracking a different endpoint; re-adopt didn't refresh the ~hourly proxy token | `39216ac` | — | ours |
| MCP `version` tool description didn't match the CLI's own output format; test isolation | `2a380e0` | — | ours |
| MCP tool output carried raw ANSI escape codes from IPython's colored traceback formatter | `6b125da` | — | our wrapper; the ANSI comes from IPython. Stripped only at the MCP boundary so a human running `exec` directly still gets colors. |

---

<a name="extensions"></a>
## The extensions, and why an agent needed each

### `adopt` — `0eabea6`, `39216ac`

Brings a Colab runtime started outside the CLI under local session tracking.
`adopt --orphanage` claims every orphaned server-side assignment in one pass.

**Why an agent needs it.** `mighty-colab`'s read commands have *different
scopes*, and this is not obvious: `sessions`/`status` query the backend directly
and see everything on the account; `log`/`ls`/`exec` operate through **this
local process's own** session tracking (`~/.config/colab-cli/sessions.json`). A
session created by a *different agent process* is invisible to the second set
even though `status` already shows it. Multi-agent and multi-session work is
normal for agents and rare for humans, so this is a workflow the design didn't
anticipate. `adopt` is also the recovery path for a stale proxy token (they
expire roughly hourly) without tearing down and reallocating a VM.

Source: `bonsai-2026/docs/PROJECT_MEMORY.md:535-547`.

### `reinstall` — `4a64e06`

`install` then restart the kernel, if and only if the install succeeded.

**Why.** Kernel state persists across `exec` calls in a session — that is the
point. But Python caches imports in `sys.modules`, so upgrading an
already-imported `jax`/`torch` has **no visible effect** until the kernel
restarts. A human notices the version didn't change; an agent proceeds and gets
numerically wrong results from the old library. Deliberately a *new command*
rather than a flag on `install`, so `install` stays a faithful match to upstream
(`AGENTS.md:44-45`: "Do not change the behavior or flags of existing upstream
commands… so patches stay upstream-mergeable").

Every GPU target in `bonsai-2026/Makefile` uses `reinstall`, not `install`.

### `mcp` — `6d58227`, `9bead3d`

A stdio MCP server exposing the CLI's own commands as tools, scanned from the
Click command registry so the toolset stays in sync automatically. Interactive
commands (`ssh`, `repl`, `console`, `edit`, `drivemount`) are excluded.

**Honest framing:** for our actual research work we did *not* use this — Claude
Code shells out to the CLI, which is what every Makefile target does. The MCP
server is for a different client (Claude Desktop). Worth mentioning because
Google ships an official in-notebook MCP server (`googlecolab/colab-mcp`,
linked in our README), and the two occupy genuinely different niches:
in-notebook interactive assistance vs. headless CI-shaped automation.

### Real chunked upload — `a6d4c75`

The Jupyter Contents API's actual chunked protocol (1MB slices, numbered
requests, `chunk: -1` finalizer), matching JupyterLab's own client. Verified
live at 50MB and 160MB with byte-exact integrity (`900bc79`).

### Skill extensions — `git diff 1005593 HEAD -- skills/colab-operator/SKILL.md`

Upstream shipped the skill. We added 19 lines: the `adopt` section, `reinstall`,
the MCP note, the CPU-spin recovery entry, and the corrected `--auth` default.
Worth noting that the corrections are all "things that block an agent
specifically."

---

<a name="what-worked"></a>
## What the CLI got right for an agent

A user researcher will ask this within fifteen minutes, and the answer is not
"nothing." Several of these Google did unprompted, before anyone was asking for
agent support.

- **A skill document shipped with the CLI.** `colab skill` prints an
  agent-facing operating manual (`027e821`, `0e22a02`), and `colab readme`
  prints the README. That is self-description for a non-human caller, in the
  box, and it predates our involvement entirely. Upstream's own `AGENTS.md` even
  carries a "What I Can vs Cannot Run" section listing which commands hang on a
  TTY. Whoever wrote that had already thought about this.
- **`colab run` has genuinely correct native-Python semantics.** `sys.argv` is
  set, `__name__ == "__main__"` fires, and CPython's exit-code conventions for
  `sys.exit()` are honoured down to `sys.exit('msg')` → 1
  (`src/colab_cli/commands/run.py:100-111`, `docs/05_run_command.md`). Someone
  ran a real script end to end and fixed the noisy `SystemExit: 0` traceback
  they saw. That is the level of care this whole document is asking for
  elsewhere.
- **`--env KEY=VALUE`** (`507d169`). Our entire credential-passing scheme for
  cloud-side GCS writes is built on it and needed nothing added.
- **Suppressing inline terminal-image escapes when stdout isn't a TTY**
  (2026-05-07). The exact instinct our ANSI complaints are asking to extend:
  a terminal affordance standing down for a non-terminal consumer.
- **The keep-alive fix** (`05027b6`, issue #14). The old
  `RuntimeService/KeepAliveAssignment` RPC required `serviceusage` consumer
  access to Colab's internal project, so it returned 403 for *every* external
  user and their sessions were idle-pruned within minutes. Replacing it with a
  Tunnel Frontend HTTP ping is the difference between the CLI working and not
  working for anyone outside Google. Diagnosed and fixed in the open.
- **Session state survives across `exec` calls.** Imports, variables and fitted
  objects persist between separate invocations, so an agent can build state
  incrementally instead of re-importing a 2GB framework per step. For our
  workload this was worth more than any single fix below.
- **`colab sessions` shows server-side assignments including orphans.** The
  data needed to detect a leaked, billing VM is exposed. `adopt` exists because
  we wanted to *act* on it, not because the visibility was missing.
- **Provisioning is genuinely fast**, and `run` as a shebang line
  (`#!/usr/bin/env -S mighty-colab run --gpu T4`) is a good idea that works.

---

<a name="failure-modes"></a>
## Agent-specific failure modes

The heart of it: **what breaks when the caller is a program rather than a person
at a terminal.** Each entry names the defence we built, because each defence is
evidence of a wound.

### 1. A zero exit code is not evidence the work happened

Two distinct problems, and only the first is a bug.

- `exec` exiting 0 on a remote exception — fixed (`679c0b6`).
- **Even a correct exit code cannot distinguish "ran and passed" from "exited
  cleanly without ever reaching its verdict."** A truncated or short-circuited
  script exits 0 either way.

**Defence:** every driver prints its own sentinel (`STAGE1_OK`,
`GPU_VERIFY_OK`, `CNN_GPU_VERIFY_OK`) and every recipe requires **both** a zero
exit and the sentinel (`bonsai-2026/Makefile`, every `grep -q *_OK` line). Both
halves are
tested — a missing sentinel on a zero exit fails
(`test_ladder_missing_sentinel_fails_even_on_a_zero_exit`), and a correct
sentinel does **not** rescue a non-zero exit
(`test_ladder_nonzero_exec_fails_the_target_even_when_the_sentinel_is_present`).

### 2. No peripheral vision for a running bill

A human notices a session still up. An agent's context ends and the A100 keeps
billing. Nothing reclaims a Colab session automatically except a 24-hour
keep-alive cap.

**Defence:** unconditional teardown on every path, plus the `check_teardown`
macro — which had to be careful about the thing that makes such
checks unadoptable: "already absent" and "could not stop" mean **opposite**
things, and conflating them turns the safest outcome (provisioning failed,
nothing was ever created) into a false alarm on exactly the path it fires
most.

### 3. The 30-second output-gap timeout

Covered above. **Defence:** `EXEC_TIMEOUT ?= 3600`, a heartbeat thread, and a
test that fails any future recipe omitting `--timeout`.

### 4. `exec -f` has no file, no `__main__`, and no repository on disk

Covered above. **Defence:** static and dynamic no-`__file__` tests; the driver
architecture (clone a pinned commit *inside* `main()`; module scope uses stdlib
and numpy only).

**A second-order consequence worth naming.** Because the transmitted text has no
`__file__`, a driver cannot hash itself to prove which code ran. Our fix comes at
it from the other side: the make target computes the local file's SHA-256 and
passes it as `BONSAI_DRIVER_SHA256`; the driver hashes the
*clone's* copy of itself and compares (`run_ladder_stage1.py:34-38`). Provenance
in an agent-driven pipeline is a real requirement, and the execution model
actively works against it.

### 5. Interactive-by-default authentication

`--auth` defaults to `oauth2` — an interactive browser consent flow. An agent
that doesn't explicitly pass `--auth=adc` hits exactly the flow the skill
document elsewhere says to avoid. Our own docs claimed the default was `adc`
until `a070161` corrected them, and the stale copy still lives in the consuming
repo. The global flag must also precede the subcommand, which is easy to get
wrong.

Related: `auth` and `drivemount` genuinely require a human — they block on
`input()` and `/dev/tty` respectively, and an agent's shell tool hangs
indefinitely. `repl`/`console` accept piped stdin and exit on EOF (upstream fixed
that on 2026-05-07), but interactive TTY mode cannot be driven by an agent at
all. Upstream's own `AGENTS.md` has a "What I Can vs Cannot Run" section listing
these — a good instinct, and evidence Google already thinks about this.

### 6. Terminal affordances become corruption

ANSI escape codes appearing in output a program parses. Two instances:

- IPython's colored tracebacks arriving through MCP tool results — `6b125da`,
  stripped at the boundary only.
- **`--help` text itself.** Rich (which Typer uses) emits raw ANSI even under a
  test runner with no TTY whenever the environment forces color — and
  `FORCE_COLOR=1` is common in AI agent sandboxes and CI. Worse, Rich styles
  `--rm` as *two separately-colored spans*, so a naive `"--rm" in output` check
  silently depends on the running environment rather than on the code. It passes
  on a human's terminal and fails in an agent's sandbox. Encoded as `AGENTS.md`
  principle 24 (`77a4322`, `be813c9`).

Google already suppresses inline terminal-image escapes when stdout isn't a TTY.
The ask is to extend that same rule to tracebacks and help text.

### 7. Undocumented upload limits, reported as bare HTTP codes

A single 250MB upload package was rejected by the upload endpoint with a **bare
400 Bad Request**. The 10MB package had worked. Worked around by splitting into
twelve ~20MB `.npy` chunks. Separately, files over ~1MB could hit an HTTP 500 —
the reason we implemented the real chunked protocol.

The limit is nowhere in the documentation, so the ceiling had to be found by
bisection, at cost. This one incident shaped the entire Stage 2B architecture:
`DESIGN.md:499` locks "artifacts pushed to GCS from within the cloud
environment — never round-tripped through local upload (Stage 2A's
242MB-vs-~6-15MB Colab upload limit, already hit once)."

Source: `bonsai-2026/experiments/stage2a_dynamics_classification/FINDINGS.md:753`.

### 8. Local filesystem vs. VM filesystem

`status` reports a "last execution" path like `/tmp/gpu_experiment/` that is
local to whichever machine ran the CLI, not the VM's `/content/`. Conflating them
sends an agent searching the wrong side.

Source: `bonsai-2026/docs/PROJECT_MEMORY.md:548-552`.

### 9. Silent state accumulation on the VM

Every `auth`/`drivemount`/`install` call started an untracked kernel that
nothing — not even `colab stop` — ever shut down (`7eb22fd`). A human runs
`install` twice. An agent runs it in a loop.

---

<a name="self-inflicted"></a>
## What we'd have hit anyway vs. what we did to ourselves

Four buckets, because two loses the most useful distinction.

### 1. Genuine product friction — any agent using the official CLI hits these

The sixteen upstream defects, and in particular:

- `exec` exiting 0 on failure (#1, #2)
- the 30-second output-gap default
- teardown swallowing failures and destroying the retry path (#3)
- automation commands leaking kernels (#5)
- `install` reporting success on failure (#6)
- error text on stdout (#14)
- the undocumented upload ceiling

Plus two things that are not defects at all but are structural: **an exit code
cannot carry a verdict**, and **`exec -f` has no script semantics**.

### 2. Genuine friction we could only document

The `jupyter_kernel_client` CPU spin. Google-owned fork, issues disabled, no
channel. The process is the problem, not the code.

### 3. Bugs in our own additions

The chunked-upload infinite loop (`914e3df`) — in code we wrote in `a6d4c75`.
The `adopt` persist-after-spawn ordering gap (`3a03175`). The MCP fixes
(`2a380e0`). All ours. **Say so unprompted**; it is the cheapest way to make the
rest of the list credible.

### 4. Costs of choosing to fork at all

- We carry a divergent CLI, a separate PyPI package, and a release pipeline that
  exist only because upstream doesn't take PRs. That is a *consequence* of
  Google's policy, but the decision to fork rather than wait was ours.
- We broke our own consumer by fixing `exec`'s exit code (story 2). Nobody
  imposed that on us.
- We changed `status`/`stop` exit codes on an inference nobody had asked for,
  and reverted both (`e3cd8e1`). Self-inflicted, caught, reverted before release.
- The stale skill copy in the consuming repo is duplication we chose. **[✅
  Resolved 2026-08-11]**

### Explicitly not Colab's problem — see the next section

---

<a name="not-googles-problem"></a>
## Not Google's problem

Listed so they don't get mixed in by accident.

- **Reused terminal windows killing foreground processes.** An IDE/MCP-harness
  behaviour (`bonsai-2026/CLAUDE.md`, "Running things"): reusing a terminal
  window that has a foreground process kills it. Nothing to do with Colab.
- **An agent session torn down mid-run, losing the diagnosis.** An ephemeral
  agent instance spawned to run a GPU pilot lost its session and was itself torn
  down before writing findings anywhere. Evidence trail: the VM's execution
  history showed a script had run, the local `.pyc` cache showed a local
  fallback had *started*, and no results file existed. The diagnosis had to be
  re-derived from scratch. That is an agent-harness durability problem
  (`bonsai-2026/docs/PROJECT_MEMORY.md:525-534`) — though it does motivate an
  ask below, because a *server-side* record of what a session ran would have
  made recovery possible.
- **The stale `--auth` default in the consuming repo's skill copy.** Ours.
- **GPU numerics.** A100 XLA computes float32 convolutions at TF32 by default
  (max relative difference 1.058e-04 vs CPU; 1.172e-07 with precision pinned),
  and a T4 has no TF32 hardware so it cannot exhibit the effect and *looks* like
  a pass. Genuinely surprising, genuinely cost us a review round — but it is
  JAX/XLA/hardware behaviour, not Colab's, and it is documented by NVIDIA and
  JAX. Mention it only if asked what surprised us about cloud GPUs.
  (`bonsai-2026/docs/PROJECT_MEMORY.md:558-590`.)

---

<a name="asks"></a>
## What we'd ask Google for

Each traces to a specific incident above. Ordered by leverage.

### 1. A contribution path

`CONTRIBUTING.md` refuses external PRs. Sixteen tested fixes, each with a
reproducing test, sit in a public fork as a direct result. Even a narrow
"accepted: bug fixes with tests, in these files" would move most of them
upstream. Verified 2026-08-07: the policy is unchanged, and upstream `main` has
had no commits since 2026-07-30, so none of the sixteen has been fixed
independently either.

*Traces to:* the entire fixes table.

### 2. A machine-readable execution result

**[✅ Done — `de060d2`..`896ed77` on `main`, 2026-08-12]** `--json` on
`exec`/`run`/`exec-async`/`log --tail`. Each envelope carries
`schema_version`/`cli_version`, a `status` (`"ok"` / `"job_raised"` /
`"error"`) and `exit_code` for the *remote job*, separate from the CLI
process's own exit code (which stays 0 under `--json` whenever the CLI
itself completed its transaction, even if the remote job raised — see ask
#6). `exec`/`run` reuse the outputs `runtime.execute_code` already returns
(nbformat-shaped, per block for `exec`), with tracebacks ANSI-stripped by
default; `--no-strip-ansi` keeps the raw escapes in that same `traceback`
field instead (no separate raw-copy field either way, to keep the payload
lean). `exec-async --json` returns
`{status:"started", pid, log_path}` immediately and writes its terminal
result to a `<log_path>.json` sidecar file that survives session teardown;
`log --tail --json --since-offset N` polls it incrementally. Designed
against real feedback from the consumer agent behind this ask, plus a
second review pass — see the `de060d2`..`896ed77` commit history on `main`
for the full rationale, including the exact `SystemExit(0)` incident
described below. Verified against extensive mocked unit tests, and live
against a real Colab session end to end: `integration/repro_json_output/`
and `integration/repro_json_jq_lifecycle/` (a full new→exec→exec-async→
log--tail→status→sessions→stop lifecycle composed entirely with `jq`
against real `--json` output) both pass clean against a real backend.

*Traces to:* `679c0b6`; the sentinel pattern
(`bonsai-2026/Makefile`, the `grep -q *_OK` lines);
`test_ladder_missing_sentinel_fails_even_on_a_zero_exit` and
`test_ladder_nonzero_exec_fails_the_target_even_when_the_sentinel_is_present`.

Original ask, for context: per-cell status, exception type and message, timing,
and whether the script reached completion. A correct exit code still cannot
distinguish "ran and passed" from "exited before reaching its verdict," which is
why every recipe we have greps a sentinel the script prints itself. The parsing
we do today is fragile by construction.

### 3. Separate the wall-clock budget from the inactivity budget

`--timeout` currently means "maximum gap between outputs" and defaults to 30
seconds. Those are two different budgets. An agent wants: "kill this if it runs
longer than an hour" *and* "kill this if the kernel has said nothing for ten
minutes." Today it can only express the second, badly. The tell is our heartbeat
thread, whose only job is to keep the transport alive.

*Traces to:* three targets that never once completed
(`bonsai-2026/docs/PROJECT_MEMORY.md:629-645`);
`run_ladder_stage1.py:117, 156-166`.

### 4. Give `exec -f` `run`'s execution semantics — or name the model in the flag

**[✅ Done — `241b48b`, 2026-08-11]** `exec -f` (non-notebook files) and `run`
now share one prelude builder (`_build_script_prelude()` in
`commands/execution.py`) setting `sys.argv`, `__name__ = '__main__'`, and
`__file__` — the last of which neither command set before. `__file__` is a
synthetic `<mighty-colab-exec:basename>` sentinel rather than the caller's
real local path, since nothing from the local filesystem exists on the
runtime and a plausible-looking real path would be more misleading, not
less. Verified live against a real Colab session, including the exact
`os.path.dirname(os.path.abspath(__file__))` pattern from story 1 below.

`colab run` sets `sys.argv` and `__name__ = '__main__'`
(`run.py:104-108`); `exec -f` sets neither, and neither sets `__file__`. If
`exec -f` is going to take a file, it should behave like one — or the help text
should say "the file's text is transmitted into a live kernel; `__file__` will
not exist." Also worth documenting explicitly: **nothing from the caller's
filesystem exists on the runtime**, so module-scope code can rely on stdlib and
preinstalled packages only.

*Traces to:* story 1;
`bonsai-2026/experiments/stage2b_denoising/FINDINGS.md:378-402`.

### 5. Document the upload ceiling, and return an actionable error

A bare 400 on a 250MB upload and a bare 500 over ~1MB are both unactionable. A
documented limit and an error naming it would have saved a bisection and would
not have shaped an entire experiment's architecture around avoiding the
mechanism.

*Traces to:*
`bonsai-2026/experiments/stage2a_dynamics_classification/FINDINGS.md:753`;
`DESIGN.md:499`; `a5b4722`; `a6d4c75`.

### 6. A written, versioned exit-code and stream contract

**[✅ Done, scoped to `--json` — `de060d2`..`896ed77` on `main`,
2026-08-12]** Under `--json` (`exec`/`run`/`exec-async`/`log --tail`, plus
`new`/`stop`/`sessions`/`status`), the contract is now explicit and
versioned rather than something to derive by reading source:
`schema_version`/`cli_version`/`command`/`http_status` in every envelope;
the CLI process's own exit code stays 0 whenever it mechanically completed
its transaction — even if the remote job raised — with the job's own
outcome carried separately as `status`/`exit_code`/`reason` in the body;
`[colab] ...` chatter always on stderr (a `typer.echo` redirect installed
once, globally), JSON only on stdout. The envelope shape is also now
Pydantic-backed (`src/colab_cli/envelopes.py`) and strictly validated at
emission, not just documented convention. `status --json -s <missing>` is
the one place this ask's "query commands should error on not found"
position was actually applied under `--json` (diverges from the
unconditional exit-0 plain-text path below it). **Still open:** the
broader case-by-case survey below (which *non*-`--json` commands treat
"not found" as an error vs. an idempotent no-op) is unchanged — that's a
wider audit across the whole CLI, not something `--json` alone resolves.

Which commands treat "not found" as an error and which treat it as an idempotent
no-op; which write to stdout and which to stderr; what changes count as
breaking. We had to derive this by reading source and pin it in a test. And when
*we* changed one of these, it silently broke a consumer that had adapted to the
old behaviour.

The specific, defensible position we landed on and would offer as a starting
point: **query commands should error on "not found"; desired-state commands
(`stop`) should not** — `stop` returning 0 on an absent session is what makes an
unconditional teardown safe, and is the strongest available evidence nothing is
billing.

*Traces to:* `c76d621` → `cf8d1ea` → `94f27a6` → `e3cd8e1`; `STOP_ABSENT_RC`
(`bonsai-2026/Makefile`).

### 7. Distinguish "already absent" from "could not stop" in `stop`'s exit code

Today both are 0 — which is the right *default* (see above), but it means a
caller cannot detect a genuine teardown failure without parsing text. We
parameterised for it: `STOP_ABSENT_RC` names whichever code means absent, so a
future CLI that separates them needs one variable changed rather than five
recipes rewritten. `94f27a6` already made a genuine unassign failure exit 1 in
our fork; upstream's still emits a raw traceback.

*Traces to:* the `check_teardown` macro;
`test_ladder_absent_session_is_not_treated_as_a_leak` and
`test_a_distinct_absent_code_can_be_declared_without_rewriting_recipes`.

### 8. Non-TTY output hygiene, extended

**[⏳ Partially addressed — `de060d2`..`896ed77` on `main`, 2026-08-12]** Under
`--json` specifically: tracebacks are ANSI-stripped by default in the
envelope, with `--no-strip-ansi` available to keep the raw escapes for
anyone who wants to re-render it (single `traceback` field either way, no
separate raw-copy field), and stdout carries the JSON only — kernel stdout/stderr
stays inside `outputs` as nbformat blocks rather than being interleaved
raw. **Still open:** `--help` and non-`--json` invocations still don't
respect `NO_COLOR`/`FORCE_COLOR`, and there's still no general
`--no-color` flag independent of `--json`.

Google already suppresses inline terminal-image escapes when stdout isn't a TTY.
Extend that to colored tracebacks and `--help` — or provide `--no-color` /
respect `NO_COLOR`. An agent sandbox commonly sets `FORCE_COLOR=1`, which turns
`--help` parsing into an environment-dependent coin flip.

*Traces to:* `6b125da`; `AGENTS.md` principle 24 (`be813c9`).

### 9. Triage the `jupyter-kernel-client` fork

`exec --timeout` can spin a local core at 100% forever. The fork is Google-owned
and has issues disabled, so there is no way to report it. Either enable issues
or accept reports through the CLI repo. **[VERIFY]** current state.

*Traces to:* `07479f7`; `googlecolab/google-colab-cli#82`.

### 10. Server-side session provenance

A queryable record of what a session ran and when, surviving the client that
started it. Adjacent to what `adopt` reconstructs and to what would have made a
lost-agent-session diagnosis recoverable instead of re-derivable.

*Traces to:* `0eabea6`; `bonsai-2026/docs/PROJECT_MEMORY.md:525-534`.

---

<a name="index"></a>
## Citation index

**In `mighty-colab`:**

| what | where |
|---|---|
| merge base with upstream | `1005593` (2026-07-30) |
| first commit of ours | `5766b79` (2026-08-02) |
| fork provenance statement | `CHANGELOG.md`, "Google CoLab CLI Change Log" |
| third-party pin | `pyproject.toml:79` |
| upstream contribution policy | `CONTRIBUTING.md` (unchanged from `1005593`) |
| "don't change upstream commands' behaviour" | `AGENTS.md:44-45` |
| ANSI-in-help gotcha | `AGENTS.md` principle 24 |
| agent execution limits (upstream's own) | `AGENTS.md`, "Agent Execution Limitations" |
| CPU-spin bug | `07479f7`; `skills/colab-operator/SKILL.md:100` |
| `exec` prelude (env only) | `src/colab_cli/commands/execution.py:254` |
| `run` prelude (`sys.argv`, `__main__`) | `src/colab_cli/commands/run.py:100-111` |
| all fixes | `CHANGELOG.md`, `[0.1.20]`–`[0.2.2]` |

**In `bonsai-2026`:**

| what | where |
|---|---|
| the contract test | `tests/test_mighty_colab_contract.py` |
| `EXEC_TIMEOUT` | `Makefile` |
| `STOP_ABSENT_RC` | `Makefile` |
| `check_teardown` | `Makefile` |
| `GCS_EXEC_ENV` | `Makefile` |
| sentinel-grep pattern | `Makefile`, every `grep -q *_OK` line |
| pre-flight refusals (dirty import closure, unpushed HEAD) | `Makefile`, the ladder targets' `REFUSING` lines |
| infrastructure lessons, consolidated | `docs/PROJECT_MEMORY.md:505-700` |
| `__file__` incident | `experiments/stage2b_denoising/FINDINGS.md:378-402` |
| upload-ceiling incident | `experiments/stage2a_dynamics_classification/FINDINGS.md:753` |
| upload constraint, locked into the design | `experiments/stage2b_denoising/DESIGN.md:499` |
| driver execution-model docstring | `experiments/stage2b_denoising/run_ladder_stage1.py:1-48` |
| heartbeat | `experiments/stage2b_denoising/run_ladder_stage1.py:117, 156-166` |
| principle 21 (derivable sets), caused by the `--timeout` omission | `CLAUDE.md:360-365` |
| principle 18 (per-stage timing), cost-driven | `CLAUDE.md:305` |
| principle 20 (hand-verified becomes a test) | `CLAUDE.md` |
