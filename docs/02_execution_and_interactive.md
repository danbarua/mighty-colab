---
log:
2026-05-07: Fixed `colab console` piped-stdin handling. Previously a piped invocation (e.g. `echo 'cmd' |mighty-colabconsole -s s`) sent the command and then hung indefinitely because the previous EOF handler emitted a bare `\x04` (Ctrl-D), which the remote `tmux`-wrapped bash treats as a literal character rather than a session terminator. The new handler sends `exit\n` (which bash actually exits on) and then closes the websocket from the client side after a short grace period (`PIPED_EOF_GRACE_SECONDS = 0.5s`) so any tail output (bash `logout`, tmux `[exited]`) makes it back to the user. TTY mode is unchanged: real-terminal EOF is left to the remote shell. Verified live: `echo 'echo HELLO' |mighty-colabconsole -s s` now exits in ~1.2s instead of hanging.

2026-05-07: Fixed `print_kitty` (used by `colab exec --output-image` and any image-producing exec) to no-op when `sys.stdout.isatty()` is false. The Kitty Graphics Protocol escape sequence is meaningless when stdout is a file or pipe and was visually corrupting captured output (a multi-KB base64 PNG blob would land in log files, grep targets, or showboat captures). Image bytes are still saved to disk via `handle_image`'s file-write path; only the inline-render attempt is suppressed.

2026-06-04: Bumped the default `--timeout` for `colab exec` from 10s to 30s (and the matching `colab run` default) so brief silent tasks are less likely to hit a premature `TimeoutError`. Explicit `--timeout` overrides are unaffected.

2026-08-11: Added `colab exec-async` and `colab log -f/--follow`. `colab exec` is synchronous end-to-end: `exec_command` blocks the calling process on `runtime.execute_code`'s `execute_interactive` call until the kernel returns `execute_reply` (or `--timeout` elapses). Its *live* stdout is not buffered -- `output_hook` fires and `display_output` flushes on every IOPub message as it arrives -- but the separate JSONL history (`colab log`, no `-f`) only gets its `execution` event once the whole call returns, so a long-running job looks silent there until it finishes. `exec-async` re-spawns the real `exec` command itself as a detached background process (same `start_new_session=True` daemon pattern as `spawn_keep_alive`), redirecting its stdout/stderr to a raw per-session log file (`~/.config/colab-cli/history/<session>.exec.log`) instead of the caller's terminal, and returns immediately. `SessionState.exec_pid`/`exec_log_path` track it. `colab log -s <session> -f` tails that raw file live (real polling, not the JSONL path) until the worker's PID exits. A stale `exec_pid` from a finished run never blocks a new `exec-async` call -- liveness is always re-checked via `os.kill(pid, 0)`, not by a child-side state write (which wouldn't survive a hard kill). `colab stop` now also kills a live `exec_pid`, mirroring `keep_alive_pid`, so a background job isn't left talking to a kernel that's about to be shut down. Verified live against a real Colab CPU session: `exec-async` returned in <0.5s for a ~10s script, `log -f` streamed each line as it was printed, a second `exec-async` while the first was still running was correctly refused (exit 1), and a third `exec-async` after the first finished was correctly allowed.

2026-08-11: Added a cross-session collision guard to `exec-async`. `log_path` is derived from the session name alone, so two different sessions should never compute the same path -- but `spawn_exec_async` opens that path with `open(log_path, "wb")`, which truncates unconditionally. If local state is ever wrong in a way that produces the same path for two sessions (hand-edited `sessions.json`, a future rename flow, a case-insensitive filesystem), that truncation would silently corrupt whatever another live session's worker is mid-write on. `exec_async` now scans `state.store.list()` before spawning and refuses (exit 1, loud stderr message) if any *other* session's tracked `exec_log_path` already equals the path about to be claimed and that other session's `exec_pid` is still alive. Out of scope, called out as a known limitation rather than fixed: the microsecond spawn-instant TOCTOU race where two `exec-async` invocations on the *same* session both pass the "already running" check before either persists its PID -- closing that fully would require the log file to be opened (and locked, e.g. via `filelock`, already a project dependency) inside the detached worker itself rather than by the parent before spawning it, which is a larger redesign than this fix warrants today.
---

# Design: Execution and Interactive Interaction (`repl`, `exec`, `console`)

## Overview
Execution involves sending Python code (or shell commands) to the Jupyter kernel running on the Colab VM and processing the stream of output messages.

## Approach

### 1. REPL (`colab repl`)
- **Transport**: WebSockets (using `websockets` library if allowed, or a custom `http.client` based long-polling implementation if we're strictly stdlib).
- **Communication**: Jupyter Kernel Messaging Protocol.
    - `execute_request`: Send code string.
    - `execute_reply`: Get status.
    - `iopub.stream`: Capture `stdout` and `stderr`.
- **Interactive Mode**: Standard Python `cmd.Cmd` or `code.InteractiveConsole` for local input/output.
- **Piping Support**: Detect `sys.stdin.isatty()`. If not a TTY, read all input and send as a single execution request.

### 2. Execution (`colab exec`)
- **File Handling**:
    - If file path is local: Read content, send as code.
    - If file path is remote: Execute `!python <path>`.
- **Multi-Modal Output**: Handle `display_data` messages (e.g., `image/png`, `text/html`). For the CLI, we'll save images to temporary files and print their paths, or if the terminal supports it (e.g., iTerm2), inline them.
- **Timeout Configuration**: Exposes a `--timeout` flag (default 30s) to allow long-running silent tasks (like model compilation or data downloading) to execute without being prematurely killed.

### 2b. Background Execution (`colab exec-async` / `colab log -f`)
- **Motivation**: `colab exec` blocks the caller for the entire run. For a long training job the caller either has to keep a terminal tied up, or -- if they instead poll `colab log` (no `-f`) -- see nothing, since the JSONL history's `execution` event is only written once the whole call returns.
- **Implementation**: `exec-async` does not introduce a new execution path. It spawns the *actual* `exec` command as a detached daemon (same `subprocess.Popen(..., start_new_session=True)` pattern `spawn_keep_alive` uses for keep-alive), with stdout/stderr redirected to a raw log file instead of the caller's terminal. This means the daemon gets `exec`'s full behavior for free -- `s.running`, `s.last_execution`, the `execution` history event, notebook cell splitting, `_output.ipynb` saving -- with zero duplicated logic.
- **State**: `SessionState.exec_pid` / `exec_log_path` track the daemon. A second `exec-async` on the same session is refused while the tracked PID is still alive (`os.kill(pid, 0)`); once it exits, a new one is allowed. `colab stop` kills a live `exec_pid` alongside `keep_alive_pid`.
- **Piped stdin**: since a detached child's stdin is `DEVNULL`, piped code can't be forwarded live -- it's read once by the parent and materialized to a temp `.py` file under `~/.config/colab-cli/exec-async/`, then handed to the daemon via `-f`.
- **Following output**: `colab log -s <session> -f` tails the raw log file (polling + liveness check), independent of the JSONL history view the same command renders without `-f`.

### 3. Console (`colab console`)
- **Implementation**: Connects directly to the backend terminal endpoint (`/colab/tty`) via WebSockets using `websocket-client`.
- **Interactive**: Bypasses the Jupyter kernel entirely to provide a raw, PTY-backed bash session on the Colab VM.
- **Terminal Management**: Configures `sys.stdin` to raw mode using `termios` and `tty`, passing single characters to the socket and writing raw ANSI escape sequences directly to `sys.stdout.buffer`. Hooks into `SIGWINCH` to communicate local terminal dimensions (`cols`/`rows`) to the remote bash environment so output rendering works perfectly during resizing.
- **Piped stdin**: Detected via `sys.stdin.isatty()`. When piped, the input characters are forwarded one at a time to the remote pty, and on EOF the client sends `exit\n` and then closes the websocket itself after `PIPED_EOF_GRACE_SECONDS` (0.5s) so the user's shell goodbye text drains back. The remote `/colab/tty` endpoint wraps bash in tmux, which intercepts a bare `\x04` as a literal character — that is why we send `exit\n` rather than Ctrl-D.

## Implementation Details
- **Kernel Management**: `ColabRuntime` (from `colab-agent`) already handles message signing and message types.
- **Output Streaming**: Continuous polling or asynchronous message handling to provide real-time output.
- **Piping Example**: `cat script.py |mighty-colabexec -s my-session`.

## Testing Strategy
TDD is mandatory for all execution features.

### 1. Mock Kernel Client
- **Test Case**: Verify `ColabRuntime` correctly sends an `execute_request` message over the websocket.
- **Test Case**: Verify `iopub.stream` messages are correctly handled and printed to `stdout` in real-time.
- **Test Case**: Verify `display_data` (specifically `image/png`) triggers the correct local handling (saving or display).

### 2. TTY and Piping
- **Test Case**: Mock `sys.stdin.isatty()` to verify `colab repl` correctly switches between interactive mode and one-shot piped execution.
- **Test Case**: Verify large piped inputs are handled without buffer overflow or truncation.
- **Test Case**: `colab console` with piped stdin sends `exit\n` and calls `ws.close()` on EOF (regression: previously sent `\x04` only and hung).
- **Test Case**: `colab console` in TTY mode does not synthesize an exit on EOF (the user owns the session lifecycle).
- **Test Case**: `print_kitty` is a no-op when `sys.stdout.isatty()` is false (regression: previously emitted ANSI/base64 into pipes and files).
