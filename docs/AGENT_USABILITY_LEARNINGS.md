# Driving Colab from an AI coding agent: what broke and what we changed

These are field notes from August 2026, kept as an archive. They describe the
upstream CLI at the fork point (`1005593`) and this fork up to `v0.4.1`. The
current behavior is in `docs/` and in the operator skill
(`skills/colab-operator/SKILL.md`); the later `job` work is recorded in
`docs/job/chronology.md`. Every claim cites a commit, a file or a test.

Two repositories are involved:

- `mighty-colab`: a fork of Google's `google-colab-cli`, the CLI described
  here.
- `bonsai-2026`: an oscillator-network ML research project that used it for
  every GPU run, with A100 sessions, artifacts written to GCS, and real
  billing.

## Summary

For several months an AI agent, not a person, typed the `colab` commands in a
research project: it provisioned A100s, uploaded data, ran hour-long jobs and
tore sessions down. The CLI was designed for a person who can see a terminal,
notice a spinner and remember to clean up. Wherever the CLI conveyed
information through presentation instead of structure, the agent failed, and
the failure had a bill attached.

We fixed sixteen defects in the upstream code and seven in our own additions.
Fourteen of the fixes landed as a commit named `test: reproduce <the bug>`
followed by the fix. We added three commands. On the consumer side, a test
suite drives the real build recipes against a stub CLI and asserts their
failure behavior (`bonsai-2026/tests/test_mighty_colab_contract.py`); it is,
in effect, a contract for what an agent needs from the CLI.

Three findings carry the most weight:

1. **A CLI that exits 0 while the work failed is invisible to an agent.** Even
   a correct exit code cannot distinguish "ran and passed" from "exited before
   reaching its verdict", so every recipe also greps for a sentinel string the
   script prints.
2. **`exec -f` sends a file's text into a live IPython kernel.** `__file__` is
   undefined and nothing from the repository is on disk. That is documented
   behavior, and it still crashed a run on a billing A100, because no local
   check reproduces that execution model.
3. **`exec --timeout` defaults to 30 seconds and limits the gap between
   outputs, not the run.** Three GPU targets had never completed as written.

## Contents

- [Provenance](#provenance)
- [Four incidents](#stories)
- [The fixes, with attribution](#fixes)
- [The extensions](#extensions)
- [What the CLI got right for an agent](#what-worked)
- [Agent-specific failure modes](#failure-modes)
- [What any agent would hit, and what we did to ourselves](#self-inflicted)
- [Not Google's problem](#not-googles-problem)
- [What we would ask Google for](#asks)
- [Citation index](#index)

<a name="provenance"></a>
## Provenance

`mighty-colab` is a fork of `github.com/googlecolab/google-colab-cli`, and the
full upstream history is in our git history, so attribution can be checked
mechanically.

- **Merge base with upstream: `1005593`** (Tyler, 2026-07-30, "docs: update
  AGENTS with release instructions (#66)"). Everything at or before that
  commit is upstream's; everything after it is ours.
- `CHANGELOG.md` says the fork was taken at `v0.6.0`. That is the last
  upstream changelog entry (2026-06-16), not the fork point: the tree carries
  later upstream work, such as `colab ssh` (`c129cbf`, #88), `--env KEY=VALUE`
  (`507d169`, #65) and dual `ColabKernelClient`/`KernelClient` support
  (`604aac1`, #95).
- Our first commit is `5766b79` (2026-08-02); the rename to `mighty-colab` is
  `f73376d`.
- Upstream is written by Google employees (`sethtroisi@google.com` wrote the
  initial commit `2ef9825`, 2026-05-11) and community contributors.
- On 2026-08-07, upstream `main` had no commits after `1005593`, so all sixteen
  upstream defects below were still present upstream. Its `CONTRIBUTING.md`,
  unchanged from `1005593`, said "we aren't accepting external contributions
  at this time", which is why the fixes live in a fork.

| layer | what it is | examples |
|---|---|---|
| Google's CLI | everything at or before `1005593` | `exec`, `run`, `install`, keep-alive, `--timeout`, `--env`, `ssh`, the `skill` command |
| Third party, Google-owned | `jupyter-kernel-client`, pinned to a git URL (`pyproject.toml:79`, `github.com/googlecolab/jupyter-kernel-client`) | the execution transport, where the CPU-spin bug below lives |
| Ours | everything after `1005593` | `adopt`, `mcp`, `reinstall`, chunked upload, the sixteen fixes |

Upstream features this work depends on, and does not claim:

- `--env KEY=VALUE` on `exec`/`run` (`507d169`, Matt Van Horn, #65). The GCS
  credential scheme (`bonsai-2026/Makefile`, `GCS_EXEC_ENV`) is built on it.
- `--timeout` on `exec`/`run` (`96ef983`, Xiaoquan Kong, #38); the default
  rose from 10 s to 30 s in `889d09f` (Seth Troisi, #43).
- `colab run`'s Python semantics: the prelude sets `sys.argv` and
  `__name__ == "__main__"` (`src/colab_cli/commands/run.py:100-111`), and
  `sys.exit()` follows CPython's exit-code conventions.
- Suppressing inline terminal-image escapes when stdout is not a TTY
  (`docs/02_execution_and_interactive.md`, 2026-05-07). Several asks below
  extend that principle.
- The `skill` command and `SKILL.md` (`027e821`, `0e22a02`). Upstream shipped
  an agent-facing skill first; we added 19 lines
  (`git diff 1005593 HEAD -- skills/colab-operator/SKILL.md`).

<a name="stories"></a>
## Four incidents

### 1. The driver that crashed on a billing A100 because there was no file

**What happened.** The Stage 2B ladder stage-2 driver crashed at module scope,
before its `main()` ran, with `NameError: name '__file__' is not defined`. A
refactor had located a sibling driver with
`os.path.dirname(os.path.abspath(__file__))`.

**Why.** `mighty-colab exec -f script.py` reads the file locally and sends its
text into an IPython kernel cell. The script is neither run as a script nor
imported as a module, so `__file__` is never defined. Nothing from the
repository is on the kernel's filesystem until the driver's own
`bootstrap_repo()` clones it, inside `main()`, after module scope has run. The
CLI did what `README.md` ("Transparent Code Execution") and upstream's
`docs/02` specify; the caller assumed script semantics because the flag is
spelled `--file`. Ordinary local checks import the driver as a module, which
sets `__file__`, so only a check that reproduces the execution model could
catch it.

**What changed.** `bonsai-2026` added a static check that no driver references
`__file__` and a dynamic check that runs each driver's source with
`compile()` and `exec()` in a namespace without `__file__`
(`bonsai-2026/tests/test_stage2b_ladder_stage2.py`). `colab run` already built a prelude
with `sys.argv` and `__name__` (`run.py:104-108`); `exec -f` built none
(`execution.py:254`, environment variables only). Ask #4 below came from this.

**Cost.** Provisioning and package installation only: no objects under
`stage2b/train/stage2/`, and no leaked session.

**Sources:** `bonsai-2026/experiments/stage2b_denoising/FINDINGS.md:378-402`;
`bonsai-2026/docs/PROJECT_MEMORY.md:647`;
`bonsai-2026/experiments/stage2b_denoising/run_ladder_stage1.py:31-38`.

### 2. Fixing an exit code broke the code that had adapted to it, and leaked an A100

**What happened.** On the 0.1.x line, `exec` exited 0 when the remote script
raised. `679c0b6` fixed that (`v0.2.0`). The Makefile recipes were written as
`exec && download && stop`, a chain that had torn the session down on every
path *because* `exec` always returned 0. Once `exec` could fail, the chain
skipped the teardown and left a billable A100 running. Three Stage 2A targets
were written that way.

**The lesson.** A dependency that fixes a bug can break code that silently
adapted to it, and the adaptation is invisible at the call site. An upgrade
needs a check of what the old behavior was load-bearing for. Here we were both
the fixer and the victim.

**What changed.** Every GPU recipe now captures the status, tears down
unconditionally, then propagates the status (`bonsai-2026/Makefile`,
`stage2a-evolve-train-gpu` and four more). A `check_teardown` macro separates
"already absent" (nothing is billing) from "could not stop" (money is accruing
unwatched), and `STOP_ABSENT_RC` names the exit code that means absent. All
four paths run against a stub CLI in
`bonsai-2026/tests/test_mighty_colab_contract.py`
(`test_healthy_run_exits_zero`,
`test_teardown_failure_fails_an_otherwise_successful_target`,
`test_a_leak_never_masks_the_scientific_verdict`,
`test_a_distinct_absent_code_can_be_declared_without_rewriting_recipes`, and
their `stage2b-ladder-stage1` counterparts).

**Sources:** `bonsai-2026/docs/PROJECT_MEMORY.md:589-627`; `679c0b6`;
`bonsai-2026/Makefile` (`EXEC_TIMEOUT`, `STOP_ABSENT_RC`, `check_teardown`).

### 3. Three GPU targets that had never completed, because of a 30-second default

**What happened.** `make stage2a-class0-classify-gpu` first ran as a target on
2026-08-05 and died 30 seconds in, on the third of six downloads, with
`TimeoutError: Timeout waiting for output`.

**Why.** `exec --timeout` defaults to 30 seconds and limits the gap between
outputs. A healthy script that computes without printing dies.
`stage3_gpu_evolve.py` prints once per topology; the class-0 driver prints
once per download and then nothing through minutes of cuML fitting. The target
had been written down from a hand-run session without the `--timeout` flag,
so it had never completed as written; the two Stage 2A evolve targets had the
same omission.

**What changed.** `EXEC_TIMEOUT ?= 3600` (`bonsai-2026/Makefile`) is passed by
every GPU recipe. `test_every_exec_passes_an_explicit_timeout` fails if a
recipe omits it, and derives the set of recipes by parsing the Makefile. A
second test, `test_exec_default_timeout_is_short_enough_to_need_overriding`,
reads the CLI's documented default from `mighty-colab help exec` at runtime
and asserts that it still needs overriding. The drivers print a heartbeat every 30 seconds
(`run_ladder_stage1.py:117, 156-166`) only to keep the transport alive, which
is why ask #3 separates a wall-clock budget from an inactivity budget. This
incident is also the origin of `bonsai-2026` principle 21, "a hand-maintained
list standing in for a derivable set will silently under-cover"
(`bonsai-2026/CLAUDE.md:360-365`).

**Sources:** `bonsai-2026/docs/PROJECT_MEMORY.md:629-645`;
`bonsai-2026/tests/test_mighty_colab_contract.py`, module docstring item 2.

### 4. A consumer's test suite settled a CLI design question

**What happened.** `status -s NAME` and `stop -s NAME` were the only
session-targeting commands that printed "not found" to stdout and exited 0;
`exec`, `repl`, `ls`, `rm`, `upload`, `download`, `edit`, `url` and `ssh`
treated a missing session as an error. `c76d621` made both exit non-zero, and
both changes were reverted:

- **`stop`** (`cf8d1ea`): the bonsai tooling runs `stop` on every teardown
  path, including after a `new` or `exec` that failed before creating
  anything, where "not found" is the strongest evidence that nothing is
  billing. `stop` is a desired-state operation, like `rm -f`, `kill … || true`
  or an idempotent DELETE.
- **`status`** (`e3cd8e1`): the bug report never named a command, so changing
  `status` was an inference nobody had asked for.

**What it showed.** The guard in the consumer would not have broken (`9987ee4`:
it greps for "not found" inside an `if` and merges stderr into what it greps).
What mattered is that the consumer's executable expectations were the artifact
consulted to settle the design question, and they were readable by someone
who was not the consumer. A published, versioned contract test, in the spirit
of the `skill` command, would give Google the same thing. The contract file is
maintained, not only appended to: its pre-flight refusal moved from "any dirty
working tree" to "a dirty import closure of the driver", with a test in each
direction, a test that the whole-tree gate has not returned, and a test that
derives the set of commit-pinning recipes.

**Sources:** `c76d621`, `cf8d1ea`, `94f27a6`, `9987ee4`, `e3cd8e1`;
`bonsai-2026/tests/test_mighty_colab_contract.py`, module docstring and
`test_status_of_unknown_session_exits_zero_on_stdout`.

<a name="fixes"></a>
## The fixes, with attribution

For each fix, the file was checked for existence at `1005593`, checked for our
edits between the fork and the fix, and the buggy construct was read from
`git show 1005593:<file>`. Fourteen fixes land as a `test: reproduce X` commit
followed by `fix(…): X`, plus `8eb5212` under a different verb
(`git log --oneline --grep="^test: reproduce"`).

### Upstream defects we hit and fixed

Each was present in the code at `1005593`.

| # | defect | how it affects an agent | fix | red test |
|---|---|---|---|---|
| 1 | `exec` exits **0** when the remote script raises; errors were streamed to stderr but never inspected | `$?` reports success | `679c0b6` | `5ab4698` (live integration) |
| 2 | piped `repl` has the same bug | same, on the other non-interactive path | `eff46f1` | `0dfc453` |
| 3 | `colab run`'s `_teardown` swallows an `unassign` failure **and deletes the local session record anyway** | a VM that may be billing, with the record needed to retry `stop` gone | `b934507` | `0ce5827` |
| 4 | `sys.exit(False)` maps to exit code 1 (`int("False")` raises) | a successful run reported as failed | `e19db40` | `e273ba2` |
| 5 | `auth`, `drivemount`, `install` and `reinstall` each start a **new, untracked kernel** that nothing shuts down, including `colab stop` | repeated `install` calls accumulate orphaned kernels | `7eb22fd` | `6311e52` |
| 6 | `install` exits 0 on failure; the `uv`→`pip` fallback retries *every* failure and chains two tracebacks | an agent installs a nonexistent package and proceeds | `cc77552` | `8eb5212` |
| 7 | package names interpolated into generated code as `'{c}'`; a name containing a quote corrupts the source | data-dependent code corruption | `701fe65` | `a0d9a33` |
| 7a | the buggy `cmd_str` helper is upstream's, but our `reinstall` also calls it | — | — | — |
| 8 | the same for `drivemount`'s mount path | same | `9ead1eb` | `84fa5d3` |
| 9 | `edit` treats **any** download failure (auth, network, 5xx) as "the file does not exist yet" | an empty buffer overwrites real remote content | `1a68c79` | `12e81f2` |
| 10 | session-history JSONL is read and written without a lock, the one shared on-disk state without one | concurrent agent processes corrupt history | `aa2cd4c` | `ad5120b` |
| 11 | the kernel-client startup retry discards the partly started client without closing it | one leaked websocket per retry | `b5f820d` | `24d450e` |
| 12 | a non-terminal error in the `/content` pre-flight of `exec`/`repl` propagates without stopping the runtime | a leaked connection on the error path | `f2e1528` | `d523f62` |
| 13 | `restart-kernel` on an unknown session crashes with `AttributeError`; it is the only session-targeting command without the guard | a traceback where every sibling prints a message | `33fb409` | `528ccb1` |
| 14 | error and "not found" messages go to **stdout** in `ls`, `rm`, `upload`, `download`, `edit`, `exec`, `repl` and `console` | a program parsing stdout gets error text in its results | `af85dc9` | `a82f1e3` |
| 15 | an `unassign` failure in `stop` propagates as a raw traceback, and local tracking is dropped | a traceback instead of "this may still be billing", and no retry path | `94f27a6` | — |
| 16 | uploads over about 1 MB go as one request and hit an HTTP 500 from a request-size limit in the backend | large artifacts fail | `a6d4c75` (real chunked protocol), `a5b4722` (hint on 500) | `900bc79`, added after the fix: a live 50 MB/160 MB byte-exact check |

Row 14 covers two files. `files.py` was untouched by us between the fork and
the fix. In `execution.py`, the three `typer.echo(f"[colab] Session '{name}'
not found.")` sites the fix moved to stderr are present verbatim at `1005593`
(lines 172, 303 and 389, without `err=True`).

### Upstream defect documented but not fixed

`exec --timeout N` keeps a local CPU core at 100% and never exits, although the
remote kernel and VM are fine. The cause is in the vendored
`googlecolab/jupyter-kernel-client` fork: once the deadline passes,
`execute_interactive()`'s wait loop clamps to a 0-second timeout and spins with
no exit. It was confirmed in the vendored source, not only from the report
(`googlecolab/google-colab-cli#82`). The operator skill documents it instead
of a fix, because it is third-party code and the fork has issues disabled; the
remedy is `kill -9` on the local process, after which the session can be
reattached (`07479f7`; `skills/colab-operator/SKILL.md:100`).

### Defects in our own additions

| defect | fix | red test | note |
|---|---|---|---|
| the chunked-upload loop compared bytes sent with a pre-loop `file_size`; a file that shrank mid-upload sent empty PUT chunks forever | `914e3df` | `94dd049` | a bug in our `a6d4c75`; `contents.py` has four commits: upstream's original, our two, and this fix |
| `adopt --keep-alive` on refresh spawned the daemon **before** persisting state, so a crash mid-spawn left the pid untracked | `3a03175` | `4f0bb6b` | `adopt.py` did not exist at `1005593` |
| `adopt NAME` silently repointed a name that tracked a different endpoint, and re-adopting did not refresh the hourly proxy token | `39216ac` | — | ours |
| the MCP `version` tool's description did not match the CLI's output; test isolation | `2a380e0` | — | ours |
| MCP tool output carried raw ANSI escapes from IPython's colored tracebacks | `6b125da` | — | stripped only at the MCP boundary, so `exec` in a terminal keeps colors |
| `auth.py`'s credential errors ignored `--json`: a malformed `-c/--client-oauth-config` raised a bare traceback, and bad ADC credentials called the builtin `exit()` | `7421e7c` | `tests/test_auth.py`, `tests/test_auth_adc.py` | generalized into a top-level catch-all in `cli.py:main()`; Click's `Command.main()` catches only `ClickException`, `Abort` and EPIPE |
| the daily update-check banner could leak onto `exec`'s stdout under `--json` or `--json-result-path`, corrupting a `jq` consumer's input | `406c6cb`, corrected in `c4aa2c3` | `tests/test_json_flag.py`, `tests/test_exec_json.py` | the first fix scanned the real `sys.argv`, which does nothing under `CliRunner`, and the banner leaked into `v0.4.0`'s CI run; the correction gates on the command's own state |

<a name="extensions"></a>
## The extensions

- **`adopt`** (`0eabea6`, `39216ac`) brings a runtime started outside the CLI
  under local tracking; `adopt --orphanage` claims every orphan. The read
  commands have different scopes: `sessions` and `status` query the backend
  and see the whole account, while `log`, `ls` and `exec` use this process's
  `sessions.json`. A session created by another agent process is invisible to
  the second group. `adopt` also refreshes a stale proxy token (about hourly)
  without reallocating the VM (`bonsai-2026/docs/PROJECT_MEMORY.md:535-547`).
- **`reinstall`** (`4a64e06`) runs `install` and restarts the kernel if the
  install succeeded. Python caches imports in `sys.modules`, so upgrading an
  already imported `jax` or `torch` has no effect until a restart; an agent
  proceeds with the old library and gets wrong numbers. It is a new command,
  not an `install` flag, so `install` stays identical to upstream. Every GPU
  target in `bonsai-2026/Makefile` uses it.
- **`mcp`** (`6d58227`, `9bead3d`) exposes the CLI's commands as MCP tools,
  built from the Click registry, excluding the interactive ones (`ssh`,
  `repl`, `console`, `edit`, `drivemount`). The research work did not use it;
  Claude Code shells out to the CLI. It serves a different client from
  Google's in-notebook `googlecolab/colab-mcp`: headless automation instead of
  interactive help inside a notebook.
- **Chunked upload** (`a6d4c75`) implements the Contents API's chunked
  protocol (1 MB slices, numbered requests, a `chunk: -1` finalizer), as
  JupyterLab's client does; verified live at 50 MB and 160 MB, byte-exact
  (`900bc79`).
- **Skill additions**: 19 lines covering `adopt`, `reinstall`, the MCP note, the
  CPU-spin recovery, and the corrected `--auth` default.

<a name="what-worked"></a>
## What the CLI got right for an agent

- **A skill document ships with the CLI.** `colab skill` prints an agent-facing
  manual (`027e821`, `0e22a02`) and `colab readme` the README. Upstream's
  `AGENTS.md` also listed which commands hang on a TTY.
- **`colab run` has correct Python semantics**: `sys.argv`, `__name__ ==
  "__main__"`, and CPython's exit codes down to `sys.exit('msg')` → 1
  (`run.py:100-111`, `docs/05_run_command.md`).
- **`--env KEY=VALUE`** (`507d169`) carried the whole GCS credential scheme
  unchanged.
- **Inline terminal images are suppressed when stdout is not a TTY**
  (2026-05-07).
- **The keep-alive fix** (`05027b6`, issue #14): the old
  `RuntimeService/KeepAliveAssignment` RPC returned 403 for every external
  user, whose sessions were then reclaimed within minutes. The Tunnel Frontend
  ping made the CLI work outside Google.
- **Kernel state persists across `exec` calls**, so an agent builds state step
  by step instead of re-importing a 2 GB framework each time. For this
  workload, that was worth more than any single fix.
- **`colab sessions` lists server-side assignments, including orphans**, so a
  leaked, billing VM is detectable.
- **Provisioning is fast**, and `run` works as a shebang
  (`#!/usr/bin/env -S mighty-colab run --gpu T4`).

<a name="failure-modes"></a>
## Agent-specific failure modes

What breaks when the caller is a program, and the defence built for each.

1. **A zero exit code is not evidence that the work happened.** `exec`
   exiting 0 on a remote exception was a bug (`679c0b6`). The second problem
   is structural: a truncated script also exits 0. Every driver prints a
   sentinel (`STAGE1_OK`, `GPU_VERIFY_OK`, `CNN_GPU_VERIFY_OK`), and every
   recipe requires a zero exit *and* the sentinel. Both halves are tested
   (`test_ladder_missing_sentinel_fails_even_on_a_zero_exit`,
   `test_ladder_nonzero_exec_fails_the_target_even_when_the_sentinel_is_present`).
2. **No view of a running bill.** A person notices a session still up; an
   agent's context ends and the A100 keeps billing, and nothing reclaims a
   session except the 24-hour keep-alive cap. The defence is unconditional
   teardown plus `check_teardown`, which must not treat "already absent" as a
   failure: that is the safest outcome, and the path it fires on most.
3. **The 30-second output-gap timeout** (incident 3): `EXEC_TIMEOUT ?= 3600`,
   a heartbeat, and a test that fails a recipe without `--timeout`.
4. **`exec -f` has no file, no `__main__` and no repository on disk**
   (incident 1): the no-`__file__` checks, and drivers that clone a pinned
   commit inside `main()` and use only the stdlib and numpy at module scope.
   Without `__file__`, a driver cannot hash itself to prove which code ran, so
   the make target passes the local file's SHA-256 as `BONSAI_DRIVER_SHA256`
   and the driver compares it with the hash of its cloned copy
   (`run_ladder_stage1.py:34-38`).
5. **Interactive-by-default authentication.** `--auth` defaults to `oauth2`, a
   browser consent flow; an agent must pass `--auth=adc`, before the
   subcommand. Our docs said the default was `adc` until `a070161`. `auth`
   and `drivemount` need a human (`input()` and `/dev/tty`); `repl` and
   `console` accept piped stdin and exit on EOF (upstream, 2026-05-07), but
   their TTY mode cannot be driven by an agent.
6. **Terminal affordances corrupt parsed output.** IPython's colored
   tracebacks reached MCP tool results (`6b125da`). Rich emits ANSI in
   `--help` under a test runner whenever the environment forces color, which
   agent sandboxes and CI often do, and it colors `--rm` as two spans, so a
   substring check passes on a person's terminal and fails in a sandbox
   (`77a4322`, `be813c9`).
7. **Undocumented upload limits, reported as bare HTTP codes.** A 250 MB
   upload got a bare 400 where a 10 MB one worked, and was split into twelve
   chunks of about 20 MB. Files over about 1 MB could get a 500, which led to
   the chunked protocol. The ceiling was found by bisection and shaped the
   Stage 2B design: artifacts go to GCS from the VM and never through local
   upload (`DESIGN.md:499`;
   `bonsai-2026/experiments/stage2a_dynamics_classification/FINDINGS.md:753`).
8. **Local versus VM filesystem.** `status` reports a "last execution" path
   such as `/tmp/gpu_experiment/` on the machine that ran the CLI, not the
   VM's `/content/` (`bonsai-2026/docs/PROJECT_MEMORY.md:548-552`).
9. **State accumulates silently on the VM.** Every `auth`, `drivemount` and
   `install` started an untracked kernel that nothing shut down (`7eb22fd`).
   A person runs `install` twice; an agent runs it in a loop.

<a name="self-inflicted"></a>
## What any agent would hit, and what we did to ourselves

1. **Product friction any agent hits** with the official CLI: the sixteen
   upstream defects, in particular `exec` exiting 0 on failure (#1, #2), the
   30-second output-gap default, teardown that swallows failures and destroys
   the retry path (#3), leaked kernels (#5), `install` reporting success on
   failure (#6), error text on stdout (#14), and the undocumented upload
   ceiling. Two more are structural: an exit code cannot carry a verdict, and
   `exec -f` has no script semantics.
2. **Friction we could only document**: the `jupyter_kernel_client` CPU spin,
   in a Google-owned fork with issues disabled.
3. **Bugs in our own additions**: the chunked-upload infinite loop (`914e3df`,
   in our `a6d4c75`), the `adopt` persist-after-spawn gap (`3a03175`), and the
   MCP fixes (`2a380e0`).
4. **Costs of forking**: a divergent CLI, a separate PyPI package and a release
   pipeline, which exist because upstream takes no pull requests, though the
   decision to fork was ours. We broke our own consumer by fixing `exec`'s exit
   code (incident 2). We changed `status` and `stop` exit codes on an inference
   and reverted both before release (`e3cd8e1`). A stale copy of the skill in
   the consuming repository (`bonsai-2026/.claude/skills/mighty-colab/SKILL.md`)
   said `--auth` defaulted to `adc`; it was synced with the canonical copy on
   2026-08-11.

<a name="not-googles-problem"></a>
## Not Google's problem

- **A reused terminal window kills its foreground process.** That is IDE and
  agent-harness behavior (`bonsai-2026/CLAUDE.md`, "Running things").
- **An agent session torn down mid-run lost its diagnosis.** An agent spawned
  to run a GPU pilot lost its session and was torn down before writing
  findings. The VM's execution history showed that a script ran and the local
  `.pyc` cache showed that a local fallback had started, but no results file
  existed, so the diagnosis was re-derived from scratch
  (`bonsai-2026/docs/PROJECT_MEMORY.md:525-534`). A server-side record of what
  a session ran (ask #10) would have made recovery possible.
- **GPU numerics.** A100 XLA computes float32 convolutions at TF32 by default
  (maximum relative difference 1.058e-04 against CPU; 1.172e-07 with the
  precision pinned), and a T4 has no TF32 hardware, so it looks like a pass.
  That is JAX, XLA and hardware behavior, documented by NVIDIA and JAX
  (`bonsai-2026/docs/PROJECT_MEMORY.md:558-590`).

<a name="asks"></a>
## What we would ask Google for

Ordered by leverage. Each traces to an incident above; the status reflects this
fork on 2026-08-12.

1. **A contribution path.** `CONTRIBUTING.md` refuses external pull requests,
   so sixteen fixes with reproducing tests sit in a fork. Even "bug fixes with
   tests, in these files" would move most of them upstream. *Traces to:* the
   fixes table.
2. **A machine-readable execution result.** *Done in this fork*
   (`de060d2`..`896ed77`, extended in `b4bc47a`..`c4aa2c3`, `v0.3.0`–`v0.4.1`):
   `--json` on `exec`, `run`, `exec-async`, `log --tail`, `new`, `stop`,
   `sessions` and `status`. Each envelope carries `schema_version`,
   `cli_version`, `command`, `status`, `exit_code`, `reason` and
   `http_status`; tracebacks are ANSI-stripped unless `--no-strip-ansi` is
   given. `exec-async --json` returns at once and writes its result to a
   `<log_path>.json` sidecar that survives teardown, which
   `log --tail --json --since-offset N` reads incrementally. Parse errors also
   produce envelopes, and a catch-all in `cli.py:main()` turns any escaping
   exception into one. Verified live by `integration/repro_json_output/` and
   `integration/repro_json_jq_lifecycle/`. The original ask was per-cell
   status, the exception type and message, timing, and whether the script
   reached completion. *Traces to:* `679c0b6`; the sentinel pattern.
3. **Separate the wall-clock budget from the inactivity budget.** `--timeout`
   means "maximum gap between outputs" and defaults to 30 seconds. An agent
   wants both "kill this after an hour" and "kill this after ten silent
   minutes"; the heartbeat exists only to work around the missing first.
   *Traces to:* incident 3; `run_ladder_stage1.py:117, 156-166`.
4. **Give `exec -f` `run`'s execution semantics, or name the model in the
   flag.** *Done in this fork* (`241b48b`, 2026-08-11): `exec -f` and `run`
   share `_build_script_prelude()` (`commands/execution.py`), which sets `sys.argv`, `__name__` and a
   synthetic `__file__` (`<mighty-colab-exec:basename>`), because a real
   local path would mislead on a VM that has no local files. Verified live
   with the `os.path.dirname(os.path.abspath(__file__))` pattern from
   incident 1. The docs should also say that nothing from the caller's
   filesystem exists on the runtime. *Traces to:* incident 1.
5. **Document the upload ceiling, and return an actionable error.** A bare 400
   at 250 MB and a bare 500 over about 1 MB are not actionable. *Traces to:*
   failure mode 7; `a5b4722`; `a6d4c75`.
6. **A written, versioned exit-code and stream contract.** *Done in this fork
   for `--json`*: the process exits 0 when the CLI completed its transaction,
   even if the remote job raised; the job's outcome is in the envelope's
   `status`, `exit_code` and `reason`; `[colab] ...` text always goes to
   stderr; and `src/colab_cli/envelopes.py` validates every envelope before it
   is printed.
   `status --json -s <missing>` is an error, applying the position below.
   *Open*: a survey of which non-`--json` commands treat "not found" as an
   error. The position we offer:
   **query commands should error on "not found"; desired-state commands (`stop`) should not**, because `stop`
   returning 0 for an absent session is what makes unconditional teardown safe.
   *Traces to:* `c76d621` → `cf8d1ea` → `94f27a6` → `e3cd8e1`;
   `STOP_ABSENT_RC`.
7. **Distinguish "already absent" from "could not stop" in `stop`.** *Done*:
   since `94f27a6`, "already absent" exits 0 and a genuine `unassign` failure
   exits 1 with "the VM may still be billing" on stderr, keeping the local
   record for a retry. Under `--json`, the reasons are `already_stopped` and
   `unassign_failed` (with `http_status`), pinned in `tests/test_stop_json.py`
   (`test_stop_json_idempotent_not_found_is_ok`,
   `test_stop_json_unassign_failure_emits_error_envelope`). Both paths are in
   `stop()` in `src/colab_cli/commands/session.py`. `STOP_ABSENT_RC` was not
   needed. *Traces to:* `94f27a6`; `check_teardown`;
   `test_ladder_absent_session_is_not_treated_as_a_leak`.
8. **Non-TTY output hygiene, extended.** *Partly done*: `--json` output strips
   ANSI from tracebacks and keeps kernel output inside `outputs`. *Open*:
   `--help` and non-`--json` output ignore `NO_COLOR` and `FORCE_COLOR`, and
   there is no `--no-color` flag. *Traces to:* `6b125da`; failure mode 6.
9. **Triage the `jupyter-kernel-client` fork.** `exec --timeout` can spin a
   local core forever, and the fork has issues disabled. Enable issues, or
   accept reports through the CLI repository. *Traces to:* `07479f7`;
   `googlecolab/google-colab-cli#82`.
10. **Server-side session provenance.** A queryable record of what a session
    ran and when, which outlives the client that started it. *Traces to:*
    `0eabea6`; `bonsai-2026/docs/PROJECT_MEMORY.md:525-534`.

<a name="index"></a>
## Citation index

**In `mighty-colab`:**

| what | where |
|---|---|
| merge base with upstream | `1005593` (2026-07-30) |
| our first commit | `5766b79` (2026-08-02) |
| fork provenance statement | `CHANGELOG.md`, "Google CoLab CLI Change Log" |
| third-party pin | `pyproject.toml:79` |
| upstream contribution policy | `CONTRIBUTING.md` (unchanged from `1005593`) |
| ANSI in `--help` | `AGENTS.md`, "Strip ANSI before asserting on CLI text" |
| agent execution limits | `AGENTS.md`, "Agent Execution Limits" |
| CPU-spin bug | `07479f7`; `skills/colab-operator/SKILL.md:100` |
| `exec` prelude (environment only, at the time) | `src/colab_cli/commands/execution.py:254` |
| `run` prelude (`sys.argv`, `__main__`) | `src/colab_cli/commands/run.py:100-111` |
| the fixes | `CHANGELOG.md`, `[0.1.20]`–`[0.2.2]` |
| `--json` extended to `new`/`stop`/`sessions`/`status` | `CHANGELOG.md`, `[0.3.0]`; `b4bc47a`..`3b3ffcd` |
| `auth.py` and `--json`, top-level catch-all | `CHANGELOG.md`, `[0.4.0]`; `7421e7c` |
| update-check banner on `--json` stdout | `CHANGELOG.md`, `[0.4.0]`/`[0.4.1]`; `406c6cb`, `c4aa2c3` |

**In `bonsai-2026`:**

| what | where |
|---|---|
| the contract test | `tests/test_mighty_colab_contract.py` |
| `EXEC_TIMEOUT`, `STOP_ABSENT_RC`, `check_teardown`, `GCS_EXEC_ENV` | `Makefile` |
| sentinel-grep pattern | `Makefile`, every `grep -q *_OK` line |
| pre-flight refusals (dirty import closure, unpushed HEAD) | `Makefile`, the ladder targets' `REFUSING` lines |
| infrastructure lessons | `docs/PROJECT_MEMORY.md:505-700` |
| `__file__` incident | `experiments/stage2b_denoising/FINDINGS.md:378-402` |
| upload-ceiling incident | `experiments/stage2a_dynamics_classification/FINDINGS.md:753` |
| upload constraint in the design | `experiments/stage2b_denoising/DESIGN.md:499` |
| driver execution-model docstring | `experiments/stage2b_denoising/run_ladder_stage1.py:1-48` |
| heartbeat | `experiments/stage2b_denoising/run_ladder_stage1.py:117, 156-166` |
| principle 21 (derivable sets), from the `--timeout` omission | `CLAUDE.md:360-365` |
| principle 18 (per-stage timing) | `CLAUDE.md:305` |
| principle 20 (a hand-verified check becomes a test) | `CLAUDE.md` |
