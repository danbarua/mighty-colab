# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The package version is derived from the git tag via `hatch-vcs`; each release
below corresponds to a tag of the same name.

## [Unreleased]

### Added

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

### Removed

- The experimental `colab-mcp` git submodule, superseded by the hand-rolled
  MCP server above.

[Unreleased]: https://github.com/danbarua/mighty-colab/compare/v0.6.0...HEAD

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
