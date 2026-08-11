# Integration tests

End-to-end tests that run against a **live Colab backend** (unlike the mocked unit tests under `tests/`).

## Prerequisites
- Google account with Colab access.
- `uv` installed.
- Working auth — verify with `mighty-colab sessions`.

## Scenarios

| Directory | What it covers                                                                                                                                                                                  |
| --- |-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `repro_plot_redirection/` | `mighty-colab exec` of a matplotlib script with `--output-image` redirection.                                                                                                                   |
| `repro_keep_alive/` | Fast smoke test (~10s): keep-alive daemon spawns, persists its PID, no errors during the pre-flight ping, `mighty-colab stop` reaps it.                                                         |
| `repro_keep_alive_scope/` | Slow soak test (~95s): runs the daemon long enough for one ping past the pre-flight, asserts no `keep_alive_error` events.                                                                      |
| `repro_variable_persistence/` | Variables persist across `mighty-colab exec` calls in the same session.                                                                                                                         |
| `repro_piped_console/` | Fast smoke test (~5s including session creation): `echo cmd \| mighty-colab console -s s` runs the command and exits within 30s. Regression test for the 2026-05-07 EOF-handler fix.            |
| `repro_bundled_oauth/` | Fast smoke test (~5s): verifies that the fallback OAuth configuration is loaded and starts the OAuth flow with the default client ID when local config is missing.                              |
| `repro_ssh/` | Fast smoke test (~5s): `--help` advertises the flags and an unknown session exits. Slow soak test (~95s): Live e2e allocates a CPU VM, runs a real remote command over `colab ssh --proxy-mode` |
| `repro_run_command/` | Live e2e: `mighty-colab run` allocates a CPU VM, runs a script with forwarded argv, releases it; `--keep` leaves the session alive for a manual `stop`. |
| `repro_mcp_server/` | Live e2e: drives `mighty-colab mcp` through the `mcp` SDK's own stdio client (the real MCP client path) — `list_tools()` exclusions, `call_tool()` for `new`/`exec`/`status`/`stop` against a real CPU VM, and an `adopt` validation-error path. |
| `repro_non_zero_exit/` | Live e2e: regression test for the fix in commit `679c0b6` — `mighty-colab exec` exits non-zero (not 0) when the remote code raises, for stdin-piped code, a raw `.py` file via `-f`, and a multi-cell notebook (where later cells still run and the output notebook still saves despite the mid-run error). |
| `repro_dunder_file/` | Live e2e: regression test for the incident in `docs/AGENT_USABILITY_LEARNINGS.md` ("The driver that crashed on a billing A100 because there was no file") — `exec -f` and `run` both now set `__file__` to a synthetic `<mighty-colab-exec:...>` sentinel (not a plausible-but-wrong real path) instead of leaving it undefined, alongside the `sys.argv`/`__name__ == "__main__"` `run` already set. |
| `repro_exec_async/` | Live e2e (~75s): `mighty-colab exec-async` returns near-instantly for a ~10s script; `mighty-colab log -s <session> -f` streams its stdout live and in order; a concurrent `exec-async` on the same session is refused while the first is running; a fresh `exec-async` after completion is allowed (a stale pid doesn't block a restart); `--output-log <path>` redirects output to a caller-chosen, previously-nonexistent nested path, and `colab status` reports it; `log --tail` returns immediately for a still-running job and shows the full/`-n`-limited output once it finishes. |
| `repro_json_output/` | Live e2e (~60s): `--json` on `exec`/`run`/`exec-async`/`log --tail` (`docs/AGENT_USABILITY_LEARNINGS.md` asks #2/#6/#8). Envelope shape (`schema_version`/`cli_version`/`status`/`exit_code`); a `sys.exit(0)`-ending script resolves to `status="ok"`, not `job_raised`; `exec-async --json` submission envelope + polling `log --tail --json --since-offset` through `running` to a terminal status, cross-checked against the sidecar file's own on-disk content; the result still resolves via `log --tail --json` after `stop` removes the session. |


## Running
```bash
uv run bash integration/repro_keep_alive/test.sh
```
`uv run` ensures the local `mighty-colab` entry point is on `PATH`.

## Adding a scenario
1. Create `repro_<short_description>/`.
2. Add a script (`.sh` or `.py`) that demonstrates or verifies the issue.
3. Add a row to the table above noting whether it's fast (smoke) or slow (soak).
