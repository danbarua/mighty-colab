# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The package version is derived from the git tag via `hatch-vcs`; each release
below corresponds to a tag of the same name.

## [Unreleased]

### Added

- **`--json`:** new global flag for `exec`/`run`/`exec-async`/`log --tail`,
  emitting a versioned JSON envelope (`schema_version`/`cli_version`/
  `status`/`exit_code`) instead of human-readable output. The CLI process's
  own exit code stays 0 under `--json` whenever it mechanically completed
  its transaction, even if the remote job raised -- the job's own outcome
  (`status`: `"ok"`/`"job_raised"`/`"error"`, plus `reason` when not ok)
  lives in the envelope body instead, so a `SystemExit(0)` at the end of a
  successful script can no longer be misread as a failure. `exec-async
  --json`'s spawned worker writes its terminal result to a
  `<log_path>.json` sidecar file that survives the session being stopped;
  `log --tail --json` is sidecar-aware and gains `--since-offset` for
  incremental polling. Human-readable `[colab] ...` chatter moves to
  stderr under `--json` (stdout carries the JSON only), and tracebacks are
  ANSI-stripped by default (`--no-strip-ansi` keeps the raw escapes in
  that same field instead -- no separate raw-copy field either way).
  Backed by a Pydantic model family (`colab_cli.envelopes`), validated at
  emission time so a shape mismatch is a loud error, not a silent drift.
- **`--json`:** extended to `new`/`stop`/`sessions`/`status`. `new` returns
  the created session's name/endpoint/variant/accelerator, with reason
  codes for accelerator rejection, missing OAuth scope, and a generic
  failure catch-all. `stop` on an absent session stays `status="ok",
  reason="already_stopped"` (idempotent); `sessions`/`status` (no `-s`)
  return a session list, empty is `ok` not an error. `status -s <missing>`
  now errors under `--json` specifically (diverging from the plain-text
  exit-0 no-op), matching the "query commands should error on not found"
  principle. Every error envelope also carries `http_status`, the raw
  backend HTTP status code alongside `reason`.
- **`--debug`:** new global flag to opt into `DEBUG`-level logging
  (including third-party library chatter from urllib3,
  `jupyter_kernel_client`, and websocket). Off by default.
- **`--no-strip-ansi`:** new global flag to keep raw ANSI escapes in
  traceback text under `--json` (`exec`/`exec-async`/`run`) instead of
  the default stripped text. Controls the content of the single
  `traceback` field -- there is no separate raw-copy field to save
  bandwidth for callers who don't need it.
- **`--json`:** Click/Typer parse errors (unknown option, missing/extra
  argument) now emit a JSON envelope on stdout instead of a Rich-boxed
  stderr display, so a `| jq` pipeline gets a parseable answer instead of
  breaking. Applies to every subcommand, not just the `--json`-capable
  ones. New envelope fields: `message` (the underlying parse-error text)
  and `hint` (a short, actionable suggestion, populated for the handful of
  mistakes worth special-casing -- e.g. passing a session name
  positionally instead of via `-s/--session`).

### Fixed

- **`--json`:** `exec`/`exec-async`/`stop` without `-s` now emit a JSON
  envelope (`reason="no_active_sessions"` or `"ambiguous_session"`) when
  session auto-resolution fails, instead of silently falling back to
  plain text. The shared `resolve_session()` helper predated `--json` and
  was the one error path in these three commands that never got updated.
- **`--json`:** four more shared/duplicated-code gaps found via a
  follow-up audit, all fixed the same way: `_parse_env_vars` (shared by
  `exec`/`exec-async`/`run`, new reason `invalid_env`); `run`'s own copies
  of `new`'s accelerator-rejection and keep-alive-scope-preflight handling
  (never got the `--json` gating `new` has, despite matching text);
  `exec-async` on empty piped stdin (no envelope at all before, now emits
  a bare `status="ok"`); `log --json` without `--tail` (previously sent
  all output to stderr silently, now warns and falls back to plain text).
- **`--json`:** `auth.py`'s credential-loading errors -- a malformed or
  missing `-c/--client-oauth-config` file, and missing/invalid ADC
  credentials under `--auth=adc` -- previously escaped as a raw Python
  traceback (the OAuth2 config path) or an untyped `exit(1)` (the ADC
  path), neither of which any `--json`-gating logic ever saw. Both now
  raise plain, catchable exceptions (`AuthConfigNotFoundError`,
  `AuthConfigInvalidError`, `AuthenticationError`) carrying a `reason`
  and, where actionable, a `hint` (e.g. the exact `gcloud auth
  application-default login --scopes=...` command to run). Caught by a
  new top-level handler in `cli.py:main()` around the whole `app()`
  invocation -- the first genuinely central catch-all in this codebase,
  since Click's own `Command.main()` only catches
  `ClickException`/`Abort`/EPIPE, letting anything else (not just auth
  failures) escape uncaught. Emits the same envelope/plain-text shape as
  every other error path; `--debug` bypasses it and re-raises the full
  traceback for anyone debugging the CLI itself.

### Changed

- **Logging:** default log level dropped from `DEBUG` to `INFO`. Third-party
  libraries have no level of their own and inherit the root logger's, so a
  `DEBUG`-by-default root meant every invocation's log file (and stderr,
  under `--logtostderr`/`--json`) filled with their internal chatter --
  not something a normal CLI does by default. Use `--debug` to restore the
  old behavior.

## [0.3.0] - 2026-08-11

### Added

- **exec-async:** new background counterpart to `exec`, which blocks the
  caller until the run finishes. Spawns the same execution path as a
  detached process and returns almost immediately regardless of the
  submitted script's runtime. `colab status` reports whether a background
  job is tracked and where its log lives.
- **log:** `-f/--follow` tails a running `exec-async` job's raw
  stdout/stderr live, until it finishes. `--tail [-n N]` is a non-blocking
  sibling: prints the job's current output once and exits immediately,
  whether the job is still running or already finished.
- **exec-async:** `--output-log <path>` redirects the raw output log to
  any writable location instead of the default under
  `~/.config/colab-cli/history/`, for callers (e.g. sandboxed agents)
  without write access there.

### Fixed

- **version:** an editable/dev install (`pip install -e .` / `uv sync`)
  now reports the live git commit hash instead of a stale version string
  frozen in place the last time the environment was synced.
- **mcp:** `log`'s `--follow` flag is no longer exposed as an MCP tool
  parameter — it can block a single tool call for the unbounded duration
  of a background job, which MCP's request/response model (and Claude
  Desktop specifically) can't represent. The rest of `log`, including the
  new `--tail`, is unaffected.
- **docs:** `skills/colab-operator/SKILL.md` (bundled, also served via
  `colab skill`) consistently invokes `mighty-colab` instead of `colab`
  throughout, and now documents two gotchas that previously crashed a real
  billing run: `exec -f`'s text-transmission execution model (no
  `__file__`, no script semantics) and `--timeout`'s "gap between outputs,
  not wall clock" semantics.

## [0.2.2] - 2026-08-05

### Fixed

- **stop:** a genuine teardown failure (`unassign` erroring on a network
  blip or backend error) now prints a clean warning to stderr and exits
  non-zero, instead of an unhandled Python traceback. Local session
  tracking is kept (not removed) on this path so the VM isn't forgotten
  while it may still be billing, and `colab stop -s NAME` can be retried.
  This is what makes the not-found/genuine-failure distinction in `stop`
  actually usable by a caller: not-found still exits `0` (idempotent,
  unchanged), a real failure now exits `1` with an actionable message
  instead of relying on an uncaught exception happening to also be
  non-zero. Mirrors the equivalent handling `colab run`'s teardown
  (`_teardown` in `run.py`) already had.

- **docs:** `README.md` and `skills/colab-operator/SKILL.md` claimed the
  global `--auth` flag defaults to `adc`. It has actually defaulted to
  `oauth2` (an interactive browser flow) since upstream `#41` restored a
  bundled OAuth2 client config; the docs were never updated to match. Left
  uncorrected, this reads as "agents get ADC for free," when an agent that
  doesn't explicitly pass `--auth=adc` actually hits the exact interactive
  flow this skill elsewhere says to avoid. Code is unchanged — `--auth=adc`
  must still be passed explicitly for headless/agent use.

## [0.2.1] - 2026-08-05

### Fixed

- **exec/repl:** a non-terminal error during the `/content` pre-flight
  setup call now stops the runtime before propagating, instead of leaking
  the kernel client's websocket connection.
- **runtime:** the kernel-client startup retry loop (on `ReadTimeout`/
  `ConnectTimeout`) now closes the previous partially-started client
  before retrying, instead of discarding it without cleanup.
- **edit:** only a genuine "file not found" now falls back to starting
  from an empty buffer. Previously any download failure (auth, network,
  5xx) was silently treated the same way, risking an empty/incomplete
  edit overwriting real remote content.
- **history:** session history reads/writes are now locked (mirroring
  `state.py`'s existing pattern), closing the one piece of shared on-disk
  state in the codebase that had no locking at all.

## [0.2.0] - 2026-08-05

### Added

- **adopt:** `--keep-alive` opts in to starting the CLI's own keep-alive
  daemon on `colab adopt ENDPOINT` / `colab adopt --orphanage`. Previously
  adopt never started one, assuming the runtime's creator (e.g. an open
  Colab browser tab) was already keeping it alive.

### Fixed

- **exec:** `mighty-colab exec` now exits with a non-zero status code when
  the remote script raises an uncaught exception, instead of always
  exiting `0` regardless of outcome. Applies uniformly whether the code
  comes from stdin, a `.py` file, or a notebook — a failing notebook cell
  still lets later cells run and the output notebook still gets saved;
  only the process exit code now reflects the failure.
- **adopt:** re-running `colab adopt NAME` with a name that already tracks
  a *different* endpoint now errors instead of silently repointing it.
  Re-adopting an endpoint already tracked under the *same* name now
  refreshes its runtime proxy token (which expires roughly hourly) instead
  of being an unconditional no-op.
- **mcp:** the `version` tool's description now matches the CLI's own
  `colab version` output format (`"Version: X.Y.Z"`) instead of the bare
  version string.
- **repl:** piped `colab repl` (`echo code | colab repl -s NAME`) now exits
  with a non-zero status code when the remote code raises an uncaught
  exception, instead of always exiting `0` — the same class of bug just
  fixed in `exec` above, in the REPL's non-interactive path.
- **run:** if `colab run`'s post-script teardown fails to unassign the VM
  (network blip, transient backend error), it now prints a warning and
  keeps the session's local tracking instead of silently deleting it. The
  VM may still be billing in that case, and local state is what lets
  `colab stop -s NAME` retry the unassign; deleting it was the only way to
  recover. The script's own exit code is still unaffected by a teardown
  failure, as before.
- **upload:** the chunked-upload loop (for files over 1MB) now aborts with
  a clear error instead of looping forever if the local file yields fewer
  bytes mid-upload than its size promised when the upload started (e.g. the
  file is truncated or actively being written to while uploading).
- **auth/drivemount/install/reinstall:** these commands now reuse and
  track the session's persistent kernel instead of silently starting a
  new, untracked one on every call. Previously each call spun up a kernel
  that was never recorded locally and never shut down by anything —
  including `colab stop` — so repeated `colab install` calls accumulated
  orphaned kernel processes on the VM indefinitely.
- **restart-kernel:** `colab restart-kernel -s NAME` on an unknown session
  now prints a clean "not found" message instead of crashing with a raw
  `AttributeError` — it was the only session-targeting command missing
  that guard.
- **run:** `sys.exit(False)` in a `colab run`-executed script now correctly
  exits `0`, matching real Python semantics (`bool` is an `int` subclass),
  instead of being mis-mapped to exit code `1`.
- **install/reinstall:** package names and requirement-file paths are now
  safely quoted (via `repr()`) when building the remote install code.
  Previously a name containing a single quote could corrupt the generated
  Python source instead of producing a clean literal.
- **drivemount:** the mount path is now safely quoted (via `repr()`) when
  building the remote code, the same fix as install/reinstall above for a
  path containing a single quote.
- **adopt:** refreshing a session with `--keep-alive` now persists local
  state *before* spawning the keep-alive daemon (matching the existing
  fresh-adopt behavior), instead of only after — closing a narrow window
  where a crash mid-spawn could leave the daemon's PID untracked.
- Error and "not found" messages in `ls`/`rm`/`upload`/`download`/`edit`
  and `exec`/`repl`/`console` now print to stderr instead of stdout,
  matching every other command's convention — so scripts piping/checking
  a command's real output aren't polluted by, or blind to, error text.

## [0.1.20] - 2026-08-02

### Added

- **reinstall:** `colab reinstall` installs packages like `install`, then
  restarts the kernel if (and only if) the install succeeds. Python caches
  imports in `sys.modules`, so reinstalling an already-imported package
  (e.g. upgrading a pinned `jax`/`torch` version) has no visible effect
  until the kernel restarts; plain `install` intentionally never restarts
  on its own, to stay a faithful match to upstream `colab install`.
- **adopt:** `colab adopt ENDPOINT` brings a Colab runtime that was started
  outside the CLI (e.g. from the Colab web UI, or a process that exited
  before persisting local state) under local session tracking, so
  `status`/`stop`/`exec`/etc. can manage it going forward. `colab adopt
  --orphanage` adopts every such orphaned assignment in one pass. Idempotent
  — re-adopting an endpoint already tracked locally is a no-op — and never
  overwrites a session created normally via `colab new`.
- **mcp:** `colab mcp` starts a stdio MCP (Model Context Protocol) server
  exposing the CLI's own commands as tools, scanned directly from the Click
  command registry so the exposed toolset stays in sync automatically as
  commands are added. Interactive commands (`ssh`, `repl`, `console`, `edit`,
  `drivemount`) and internal/hidden commands are excluded. See
  [`docs/07_mcp_server.md`](docs/07_mcp_server.md).

### Changed

- Renamed the project, PyPI package, and installed executable from
  `google-colab-cli` to **`mighty-colab`**. Install with `pip install
  mighty-colab` or `uv tool install mighty-colab`.
- `mighty-colab` is now published on the public PyPI index (previously
  available only via a private Artifact Registry index). `uvx mighty-colab
  mcp` and `pip`/`uv tool install` no longer require pointing at a custom
  index.

### Fixed

- **mcp:** Error/traceback text returned by MCP tool calls no longer
  contains raw ANSI escape codes (e.g. `\x1b[0;31m`) from IPython's colored
  traceback formatter. Stripped only at the MCP wrapper boundary
  (`mcp_server.py`) — the CLI's own terminal output is untouched, so a human
  running `mighty-colab exec` directly still gets colored tracebacks.
- **install:** now reports failures with a non-zero exit code instead of
  always exiting `0` regardless of outcome, and the `uv`→`pip` fallback only
  triggers when `uv` itself isn't available on the VM rather than retrying
  every failure (including a nonexistent package) via `pip` and chaining
  both tracebacks together.
- **exec:** clarified `-f/--file`'s help text — it's a local path read and
  transmitted to the remote kernel, not a path that must already exist on
  the VM.
- **upload:** large files (over 1MB) are now uploaded using the Jupyter
  Contents API's real chunked-upload protocol (matching JupyterLab's own
  client: 1MB slices, numbered requests ending in a `chunk: -1` finalizer)
  instead of a single request that could hit an HTTP 500 from a
  request-size limit somewhere in the backend stack. Automatic for both
  `upload` and `edit` — no new command or flag. Verified live against a
  real session at 50MB and 160MB with byte-exact integrity. A bare HTTP 500
  on upload still surfaces a hint pointing at an undocumented backend limit
  as a fallback, for any other cause (e.g. a storage quota).

### Removed

- The experimental `colab-mcp` git submodule, superseded by the hand-rolled
  MCP server above.

[Unreleased]: https://github.com/danbarua/mighty-colab/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/danbarua/mighty-colab/compare/v0.2.2...v0.3.0
[0.2.2]: https://github.com/danbarua/mighty-colab/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/danbarua/mighty-colab/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/danbarua/mighty-colab/compare/v0.1.23...v0.2.0

---

# Google CoLab CLI Change Log
`mighty-colab` was forked from [upstream](https://github.com/googlecolab/google-colab-cli) at `v0.6.0`.

## [0.6.0] - 2026-06-16

### Changed

- **auth:** OAuth2 login now uses a remote copy-paste flow instead of a
  localhost callback server. The CLI prints an authorization URL with
  `redirect_uri=https://sdk.cloud.google.com/applicationdefaultauthcode.html`
  and `token_usage=remote`, then reads the pasted code from stdin. This works
  in headless/remote environments where a browser cannot reach a local
  callback port. (#54)

### Added

- **display output:** Rich rendering for `display_data` output via a shared
  `render_display_data()` helper. HTML is converted with `html2text` and
  rendered as Markdown, following a `text/markdown > text/html > text/plain`
  priority; `text/plain` is wrapped with `Text.from_ansi` to preserve embedded
  ANSI escapes. Applied consistently across `exec`, `console`/`repl`, and
  automation call sites. (#58)

### Fixed

- **keep-alive:** Replace the `RuntimeService/KeepAliveAssignment` RPC on
  `colab.pa.googleapis.com` with a Tunnel Frontend (TFE) HTTP ping
  (`GET /tun/m/<endpoint>/keep-alive/` with `X-Colab-Tunnel: Google`) on
  `colab.research.google.com`, authenticated by the user's own bearer token.
  The old RPC required `serviceusage` consumer access to Colab's internal
  project and returned HTTP 403 `USER_PROJECT_DENIED` for every external user,
  causing their sessions to be idle-pruned within minutes. The TFE ping needs
  no project entitlement; because the VM often does not answer on this path, a
  `ReadTimeout` is treated as success while genuine HTTP errors propagate.
  (#14, #61)

### Removed

- Dead grpc-web client-registry / API-key code path and the now-irrelevant
  `colaboratory`-scope / `pa.googleapis.com` pre-flight remediation messaging,
  superseded by the TFE keep-alive ping. (#61)

[0.6.0]: https://github.com/danbarua/mighty-colab/compare/v0.5.11...v0.6.0
