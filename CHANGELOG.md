# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The package version is derived from the git tag via `hatch-vcs`; each release
below corresponds to a tag of the same name.

## [Unreleased]

### Changed

- **BREAKING: `status`/`stop`:** `colab status -s NAME` and `colab stop -s
  NAME` on a session with no local record now print "not found" to stderr
  and exit non-zero, matching every other session-targeting command
  (`exec`, `repl`, `console`, `ls`, `rm`, `upload`, `download`, `edit`,
  `url`, `ssh`). Previously both printed to stdout and exited `0`, so a
  script chaining on `status`/`stop` couldn't distinguish "found and OK"
  from "no such session" via exit code alone. Any script that greps
  `status`'s stdout for "not found" instead of checking the exit code, or
  redirects stderr away before grepping, needs updating. Also affects the
  MCP server: `call_tool("status" | "stop", ...)` on an unknown session now
  returns an error result (`is_error: true`) instead of a successful result
  containing the "not found" text. Note `stop` is now the odd one out
  compared to `rm -f`-style idempotent teardown commands elsewhere: an
  unconditional teardown line that calls `stop` on a session already gone
  (e.g. auto-pruned by `exec`/`repl` after a 404) now fails instead of
  succeeding — guard or ignore-error such lines if that matters to you.

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

[Unreleased]: https://github.com/danbarua/mighty-colab/compare/v0.2.1...HEAD
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
