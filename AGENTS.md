# Mighty Colab: Agent Guidelines

This file is for coding agents that change this repository. Agents that *use*
the CLI read `skills/colab-operator/SKILL.md`, which `mighty-colab skill`
prints.

The installed command is `mighty-colab`. Upstream's command is `colab`; both
can be installed on one machine, so always invoke `mighty-colab`.

## Architecture

- **CLI**: `cli.py` builds the Typer app, and each module in `commands/`
  registers its commands. `mighty-colab help` lists them.
- **Shared state**: `common.py` defines the lazily built `state` singleton (the
  client, the session store, history and the global flags) and session-name
  resolution.
- **Client**: `client.Client` sends the control-plane requests to
  `colab.research.google.com/tun/m/...`: assign, unassign, the assignment
  listing, keep-alive and `ccu-info`. Assignment requests use a 10 s connect
  and 30 s read timeout.
- **Runtime**: `runtime.ColabRuntime` runs code on the VM's kernel through the
  vendored `jupyter-kernel-client` (`src/colab_cli/_vendor/`).
- **Local state** (all under `~/.config/colab-cli/`, overridable with
  `--config`):
  - `sessions.json`: session records (`state.StateStore`);
  - `settings.json`: settings;
  - `history/*.jsonl`: structured events (`history.HistoryLogger`);
  - `keep-alive/<session>.log`: each keep-alive daemon's stderr;
  - `jobs/<job_id>/`: job records (`job/store.py`).
- **`--json`**: the commands in `cli.JSON_CAPABLE_COMMANDS` (`exec`, `run`,
  `exec-async`, `log`, `new`, `stop`, `sessions`, `status`, `usage`, and the
  `job` and `jobs` groups) print one envelope, validated by a model in
  `envelopes.py` before it is printed. `cli.main()` catches any exception that
  escapes a command and prints an error envelope, or a `[colab] Error: ...`
  line without `--json`. `--debug` re-raises the exception instead.
- **MCP**: `mcp_server.py` exposes the non-interactive commands as tools. A
  tool whose command has an envelope runs once in JSON mode and returns the
  envelope as structured content beside the command's text. Job records are
  resources (`job://<id>`, `job://<id>/logs`, `job://<id>/files/<name>`,
  `jobs://`). See `docs/07_mcp_server.md` and `docs/job/mcp.md`.
- **`job`**: an unattended run on a VM, driven by a YAML spec. The run is
  detached from the kernel and continues when the local `job apply` process
  dies. The modules are in `src/colab_cli/job/`:
  - `models.py`: the spec, plan and envelope models;
  - `planner.py`, `spec_io.py`: plan-time validation, the source and URL
    checks, and signed-URL identities;
  - `orchestrator.py`: the `apply` state machine (provision, install,
    restart, verify, stage, launch, poll, cleanup);
  - `install.py`: dependency install, uv first and pip as the fallback;
  - `transport.py`: the Contents-API reads and writes for a running job;
  - `verdict.py`: maps a runner result to a `retry_class`;
  - `store.py`: the local job records;
  - `runtime_payload/`: the code that runs on the VM (`runner.py`,
    `shim.py`, `watchdog.py`, `netpolicy.py`, `redact.py`, `ident.py`).
  
  `docs/job/design.md` describes the design, `docs/job/usage.md` and
  `docs/job/spec.md` its use, and `docs/job/chronology.md` each change and
  finding with its evidence.

### Authentication

`auth.get_credentials(config_path, provider)` returns credentials for the
global `--auth=oauth2|adc` flag (default `oauth2`).

- **`oauth2`**: `google-auth-oauthlib`'s `InstalledAppFlow`, with the token
  cached at `~/.config/colab-cli/token.json`. The client config comes from
  `-c/--client-oauth-config` (default `~/.colab-cli-oauth-config.json`), or
  else from the bundled `src/colab_cli/oauth_config.json`.
  - The flow is a remote copy-paste flow: `_run_remote_flow` sets
    `redirect_uri=https://sdk.cloud.google.com/applicationdefaultauthcode.html`
    and `token_usage=remote`, prints the URL, and reads the pasted code with
    `input()`.
  - Do not switch to the OOB flow (`urn:ietf:wg:oauth:2.0:oob`): Google
    blocked it in 2022.
  - The `sdk.cloud.google.com` redirect is registered only for the bundled
    Cloud SDK client (`764086051850-...`). Any other client id gets
    `redirect_uri_mismatch`.
  - To check whether Google accepts a variant of the authorization URL,
    build the URL and open it: a sign-in page means Google accepts it, and
    an OAuth error page means it does not. No resources are allocated.
- **`adc`**: `google.auth.default()` with `scopes=PUBLIC_SCOPES`, re-applied
  with `creds.with_scopes()` for credential types that support it.
  - User credentials from `gcloud auth application-default login` ignore
    `scopes=` and raise `NotImplementedError` on `with_scopes`. ADC users
    must therefore log in with the scopes named explicitly:
    `gcloud auth application-default login --scopes=openid,https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/userinfo.email,https://www.googleapis.com/auth/colaboratory`.
  - The session backend requires `userinfo.email`: assign, unassign,
    `sessions` and keep-alive return 401 without it. `gcloud` rejects any
    scope list without `openid` and `cloud-platform`. `colaboratory` is kept
    for other Colab features.
  - Service-account, GCE, GKE and impersonated credentials receive the
    scopes through `with_scopes`.
- **Two different authentications.** Do not confuse them:
  - CLI to Colab's control plane: how `Client` authenticates its HTTP
    requests. The `--auth` flag and `auth.get_credentials` decide it. Any
    `--auth=oauth2` command starts the consent flow when `token.json` does
    not exist; no separate command is needed.
  - Credentials on the VM: the `mighty-colab auth` command injects the
    user's GCP credentials into the running kernel (through the
    `USE_AUTH_EPHEM='0'` gcloud path), so notebook code can call `gcloud` or
    BigQuery. It does not fix a CLI-side 401 or 403; never suggest it for
    one.

### Keep-alive

- The keep-alive daemon is the hidden `keep-alive` command, started detached
  by `spawn_keep_alive`. Every 60 s, for at most 24 hours, it sends
  `GET https://colab.research.google.com/tun/m/<endpoint>/keep-alive/` with
  the header `X-Colab-Tunnel: Google` and the user's bearer token.
- The Tunnel Frontend records the activity before it forwards the request to
  the VM, and the VM often does not answer. `client.keep_alive_assignment`
  therefore treats a `ReadTimeout` as success. HTTP errors, such as a 404 for
  a deleted assignment, propagate.
- `--no-keepalive` on `new`, `run` and `job apply` starts no daemon.
  Upstream removed its keep-alive pings on 2026-09-25
  (googlecolab/google-colab-cli#144). Three idle CPU jobs kept their VMs for
  180 minutes with and without the daemon (`docs/job/chronology.md`,
  2026-10-06); GPU VMs have not been tested.
- Do not use the `colab.pa.googleapis.com` `RuntimeService/KeepAliveAssignment`
  RPC. It requires the caller to be a `serviceusage` consumer of Colab's
  internal project `1014160490159`, which ordinary accounts are not, so it
  returns 403 `USER_PROJECT_DENIED` (issue #14). Without the
  `X-Goog-User-Project` header it returns 400 `CONSUMER_INVALID`, because the
  API key and the token belong to different projects. The browser reaches the
  RPC through a cookie proxy (`colab.clients6.google.com`) that a bearer-token
  client cannot use. Any other call to `colab.pa.googleapis.com` with a bearer
  token meets the same entitlement check.

## Core Mandates

- **Minimalism**: prefer the standard library (for example `urllib`), and use
  Typer for the CLI.
- **Piping**: handle piped stdin as well as an interactive TTY.
- **Trace alignment**: validate a new endpoint against captured browser
  traces (HAR files).
- **Test-driven development**: write the tests first, and confirm that they
  fail before you implement the change. Every design states its testing
  strategy and its test cases.
- **Jupyter protocol deviations**: Colab extends the Jupyter protocol, for
  example with `colab_request` messages on the `iopub` channel and
  `input_reply` messages that wrap `colab_reply` payloads on `stdin`. These
  need handlers inside `jupyter-kernel-client`, such as interceptors on
  `wsclient.kernel_socket.on_message`.
- **Integration testing**: unit tests with mocks are not enough. Before you
  declare a feature complete, run an end-to-end test against a live Colab VM
  with the CLI. The tests are in `integration/`; run one with
  `uv run bash integration/<name>/test.sh` from the repository root.
- **Failure detail**: when a job fails, its operator, human or AI, is away.
  Unless the operator asked otherwise, `job` tries to clean up and leave no
  VM running, so re-creating a failure means re-running a multi-process
  workflow across several distributed systems, which costs significant time.
  Every error record MUST carry the detail an operator needs to diagnose the
  cause from the record alone. Reporting that an error occurred is never
  sufficient. Examples of detail that must be kept:
  - A package failed to install: which package, which version, from which
    index? Was the cause user input (a bad pin), a package index or provider
    failure, or a transient failure that can be retried? `retry_class`
    carries that last distinction.
  - A server returned an HTTP error: which operation failed, against which
    target (its URL identity, never a signed query string), with which
    status, and what did the response body say? Was it one file in an
    otherwise successful batch, and what was different about that file
    (size, name, type)?

  Keep the exception type and message, the HTTP status, a response body
  excerpt, exit codes, and the relevant part of tool output. Remove only
  credentials: signed-URL query strings, tokens, passwords and private keys.
  A record like `{"error": true, "message": "failed"}`, or one that keeps
  only an exception class name, is a defect. These examples are not
  exhaustive.
- **Recording corrections**: when the user corrects how you work, or gives
  advice that applies beyond the current task, add it to this file as a
  rule. State what to do and, where it helps, one sentence on why. Do not
  record when or how the correction came up; that belongs in the commit
  message.

## Git and Pull Requests

- Make clean, scoped commits: one logical change per commit.
- Before staging, run `git status` and `git log --oneline -5` to confirm that
  no other session has changed the working tree. If you find unexpected
  staged or committed work, stop and report it.
- Stage explicit file paths. Never run `git add -A`.
- `main` is protected by the `protect-main` ruleset. Every change to `main`
  goes through a pull request, and the `test` check must pass. Nothing pushes
  to `main` directly, and nothing force-pushes `main`.
- Run `git fetch` immediately before pushing or merging. When `origin/main`
  has moved, rebase the feature branch onto it and test again before you
  push.
- After resolving conflicts on a pull-request branch you own, rebase it onto
  `origin/main`, test again, and push with `git push --force-with-lease`.
  Never use a bare `--force`, and never rewrite a branch shared with another
  contributor without first confirming ownership.
- A trusted collaborator can comment `/update-branch` on an open
  same-repository pull request. The workflow merges `main` into the branch
  only when GitHub can merge cleanly; it never rebases or force-pushes.
  Conflicts still need manual resolution.
- Do not accumulate stacked unmerged pull requests. After a pull request
  passes CI and live verification and is merged, fetch `main`, rebase the
  next feature branch, and run its verification again before publishing it.
- Before `git commit --amend`, confirm all three conditions:
  1. the user asked for an amend, or a pre-commit hook changed files for an
     otherwise successful commit;
  2. you created HEAD in this conversation (`git log -1 --format='%an %ae'`);
  3. the commit has not been pushed.

  If any condition fails, create a new commit. Never amend a failed or
  rejected commit.
- After committing to a feature branch, suggest the command that reviews the
  whole branch, for example `git diff main..<branch>`, not `git show <sha>`,
  which shows one commit.
- For parallel work, use a dedicated git worktree instead of sharing this
  checkout.

## Documentation Discipline

Design and reference docs (`docs/**/*.md`) describe **what the system does
now**, in present tense, for a reader who was not there when it changed. They
are not a memory aid for an agent and not a journal of the session that
produced them.

- **No changelog in frontmatter.** A `log:` block of dated entries duplicates
  `git log` and the linked pull request, and every reader scrolls past it
  before reaching the content. Keep frontmatter only for a note that a reader
  of that document needs. For `job`, the dated history of changes and
  findings is `docs/job/chronology.md`: one entry per change or finding,
  newest first, each citing its evidence. Evidence ranks: a repro script under
  `integration/`, then shipped code (a pull request or commit), then a
  recorded live run. Observations, such as "three A100 runs past 87 minutes
  crossed the token boundary", go there, not into design prose.
- **No notes addressed to a future AI session.** Phrasing like "documented
  here so the next person doesn't re-diagnose this as a defect" or "proven
  live this session" describes the writer's process, not the system. If a
  sentence would not make sense to a human maintainer who never talked to an
  AI about it, cut it or rewrite it as a fact about the system. A
  design-rationale note that explains *why* a behavior is intentional is
  such a fact; state it once, plainly.
- **`CHANGELOG.md`** records the user-facing changes, in Keep a Changelog
  format. It is not updated per commit. At release time, the `release` skill
  drafts entries for the pull requests merged since the last tag, and the
  user approves them before the skill commits. Do not add to `CHANGELOG.md`
  outside that skill unless the user asks.
- **Upstream is not a clean baseline.** `docs/06_ssh_access.md`, the one doc
  identical to `googlecolab/google-colab-cli` upstream, has the same
  dated-frontmatter changelog problem. That a convention is established is
  not evidence that it is worth keeping.
- **When features are added or behavior changes**, review the design
  document in `docs/` for every command the change affects, not only the
  shared subsystem's, and update each one to describe current behavior.

## Workflow

1. **Draft**: plan the task, and create a git branch before changing
   anything.
2. **Refine**: implement the change and verify it with `uv run pytest tests/`
   and `uv run ruff check .` (`--fix` for fixable lint).
3. **Finalize**: update the design documents (see "Documentation
   Discipline" above), then commit the finished change to the branch and open
   a pull request.

## Testing and Live Runs

- **Mocking interactivity**: commands that branch on `stdin.isatty()` use the
  `is_stdin_tty` helper in `execution.py`. Mock it with
  `mocker.patch("colab_cli.commands.execution.is_stdin_tty", return_value=...)`
  so tests do not hang in CI or agent sandboxes.
- **State isolation**: patch the `colab_cli.common.state` singleton in tests
  to control session persistence and client behavior; `tests/conftest.py`
  has the standard fixture (`mock_common_state`).
- **Strip ANSI before asserting on CLI text.** Rich emits ANSI escape codes
  under `CliRunner` whenever the environment forces color (for example
  `FORCE_COLOR=1`, which agent sandboxes and CI runners often set), and it
  colors `--rm` as two separate spans. A plain substring check such as
  `"--rm" in result.output` then depends on the environment, not on the code.
  Strip with `re.compile(r"\x1b\[[0-9;]*[a-zA-Z]").sub("", text)`, the same
  pattern as `common._strip_ansi`.
- **Test the real lifecycle.** A test of retention must invoke a retention
  mode such as `run --keep`; default one-shot cleanup removes the binding, so
  it is not evidence that a binding survives. Assert the final store
  contents, not only an intermediate return value or message.
- **Run the integration tests yourself.** Only interactive commands need the
  user (see "Agent Execution Limits" below). Tests built on `new`, `stop` and
  `log` are non-interactive; run them before you declare a fix complete.
- **Use the repository's install.** A global install of `mighty-colab` can
  shadow the project's editable install when `uv run` is invoked from
  outside the repository. Run every command with the repository root as the
  working directory, so that `uv run mighty-colab` resolves to
  `.venv/bin/mighty-colab`; check with `which mighty-colab` and
  `uv run which mighty-colab`. A shebang such as
  `#!/usr/bin/env -S mighty-colab run ...` always resolves through `$PATH`.
  To test shebang behavior after a code change, run
  `uv tool install --reinstall --force --from . mighty-colab` first, then
  check `mighty-colab version`, which includes the git short SHA.
- **Live probes allocate real resources.** Every successful
  `POST /tun/m/assign` reserves a billable VM. Prefer read-only probes. For a
  call that changes state, record each endpoint you create, release it
  (`mighty-colab stop`, or `state.client.unassign(endpoint)`) before you
  finish, and confirm with `mighty-colab sessions` that nothing is left.
- **Clean up orphaned assignments.** After a live test, run
  `mighty-colab sessions`. For an assignment marked `[?]`, which no local
  record tracks, run `mighty-colab adopt <ENDPOINT>` and then
  `mighty-colab stop -s <ENDPOINT>`. Repeat `sessions` until it reports no
  active sessions.
- **gcloud context**: the workspace may select `CLOUDSDK_CONFIG` and the
  active gcloud project through `direnv`; inspect that context instead of
  inferring infrastructure from the checkout name. Pull-request checks run in
  GitHub Actions, independent of the workstation's gcloud project. The job
  data-plane GCS bucket and its Terraform belong to the LabKit consumer
  project, not to this repository, and that bucket is valid for integration
  tests; do not add an `infra/` tree here for it. When a test overrides the
  directory's context, scope the override to that command, and name the
  signer service account and `--region` explicitly.

## Implementation Principles

1. **Direct execution**: code for `auth`, `drivemount` and similar commands is
   injected into the VM's kernel and run there.
2. **Contents API**: file management uses the Jupyter Contents API, as in the
   browser traces.
3. **Transparent storage**: every local state path can be overridden with a
   flag.
4. **No netrc**: do not use `netrc` to store tokens.
5. **Fire and forget**: each command does one thing and exits. Do not run
   long tasks on background threads inside a command. For persistent work,
   such as keep-alive, use a detached daemon process and record its pid in
   the session state.
6. **Detached children re-parse argv.** A child started with
   `subprocess.Popen` (for example by `spawn_keep_alive`) does not inherit
   the parent's parsed Typer flags. Pass every relevant global flag, such as
   `--auth` and `--config`, on the child's command line, before the
   subcommand name (Typer requires global flags first). Otherwise the child
   silently uses the defaults.
7. **Persist before spawning.** When a detached child reads a state file the
   parent writes (for example `state.store.get(session_name)`), write the
   record before spawning the child, and again afterwards to record its pid.
   Otherwise the child can read the store before the parent writes it and
   exit with `session_not_found`.
8. **Never overwrite reconciled state with a stale object.** A helper that
   refreshes or removes a `SessionState` can invalidate an object read
   earlier in the command. A later `finally` block must follow the helper's
   outcome: skip persisting after a confirmed prune, and after retention
   read the stored object again before clearing transient fields.
9. **`_issue_request` with no schema returns without validating.** A caller
   that ignores the response body passes `schema=None`; validating then
   would raise `pydantic.ValidationError`. Keep the `if schema is None:
   return` guard after the empty-body check.
10. **Forwarding a script's own flags**: Typer treats any token starting with
    `-` as a flag of the command. A command that forwards flags to a script,
    like `run script.py --script-flag`, is registered with
    `context_settings={"allow_extra_args": True, "ignore_unknown_options": True}`
    and takes the arguments as
    `Annotated[Optional[List[str]], typer.Argument(...)] = None`. Embed the
    forwarded strings into kernel-side Python with `repr()`, which produces a
    safe literal whatever quotes, backslashes or non-ASCII bytes they contain.
11. **Reuse real semantics instead of building interception.** When a check
    must reproduce how the CLI behaves elsewhere, reuse the substitution that
    already happens there instead of monkey-patching or intercepting
    `sys.path`. `_build_script_prelude` (`execution.py`) makes transmitted
    code run with `python script.py` semantics by setting `sys.argv`, `__name__` and
    `__file__`, as the interpreter does. `import_check.py` reuses the same
    `__file__` sentinel and Python's own import machinery
    (`spec_from_file_location`, `module_from_spec`), so it inherits the rule
    that importing a module does not run its `__main__` block. Monkey-patch
    only when no such hook exists; `runtime.py`'s `_apply_ws_hook`, from
    upstream, is one case.
12. **Isolate the regression first.** When the user reports an error in code
    you just changed, reproduce the failure on `main` first. Debug your change
    only after confirming that the failure is new.
13. **Treat a research caveat as a task.** When research reports a caveat,
    such as "the proto allows it but the policy may reject it", call the
    service with the proposed inputs and confirm the response before writing
    code that depends on it.
14. **Verify tool claims against primary sources.** A research tool's claim
    that something is not used, not parsed or does not exist is a hypothesis.
    Check it against the code or config, including whether the chain of files
    it describes exists.

## Extending Upstream CLIs

Mighty Colab may change upstream command behavior and flags when its
correctness or usability requires it. Prefer a separately named command when
that keeps upstream mergeable without weakening Mighty Colab's design;
upstream behavior is context, not a prohibition.

## Releases

The `release` Claude Code skill (`.claude/skills/release/SKILL.md`) cuts a
release: it drafts the CHANGELOG entries for your approval, rolls the
`Unreleased` section into a dated section on a `release/vX.Y.Z` branch, pushes
the tag, and opens a pull request for the release commit. Pushing the tag
starts `.github/workflows/release.yml`, which runs the tests, builds the
package, publishes it to PyPI through trusted publishing (the `pypi`
environment), and creates the GitHub Release from the version's CHANGELOG
section. Merge the release pull request with "Create a merge commit", so the
tag stays an ancestor of `main`. Run the skill only when the user asks for a
release, and never propose one.

## Agent Execution Limits

An agent that runs commands through a non-interactive shell can run:
- `pytest`, `ruff`, and other headless scripts;
- commands that do not wait for input, such as `new`, `status`, `stop`, `ls`,
  `install`, `exec <file.py>`, `run` and `job`;
- `repl` and `console` with piped stdin; both exit on EOF
  (`echo 'cmd' | mighty-colab console -s s`).

These need the user, because they wait for input that a non-interactive shell
cannot provide:
- `mighty-colab auth`: the gcloud path (`USE_AUTH_EPHEM='0'`) waits in
  `input()` for an authorization code pasted from a browser.
- `mighty-colab drivemount`: waits for Enter on `/dev/tty` after the user
  grants consent in the browser.
- `repl` and `console` on a real TTY: they switch to raw keystroke streaming.

For an interactive command, implement the logic, write tests with mocks, and
ask the user to run the live test in their terminal.
