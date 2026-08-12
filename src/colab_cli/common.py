# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
import logging
import os
import re
import signal
import sys
import time
from typing import NoReturn, Optional

import typer

from colab_cli.auth import AuthenticationError, AuthProvider, get_credentials
from colab_cli.client import Client, Prod
from colab_cli.history import HistoryLogger
from colab_cli.state import StateStore, SettingsStore

# `--json` envelope schema version, stamped on every emitted envelope so a
# consumer can detect drift between the CLI version they wrote a recipe
# against and the one that's actually running.
SCHEMA_VERSION = "1"

# Colab kernels format tracebacks with IPython's colored formatter, which
# embeds raw ANSI SGR escape bytes (e.g. \x1b[0;31m) in error output. Those
# are meaningless -- and hard to parse -- in a JSON text field, even though
# they're exactly what a human wants in a real terminal. Shared by the MCP
# boundary and `--json` envelope building so both strip the same way.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


# Matches a truncated ANSI escape sitting at the very end of a string --
# `\x1b`, or `\x1b[` optionally followed by digits/semicolons, with no
# terminating letter yet. A complete sequence (which always ends in a
# letter) never matches this, since the trailing `[0-9;]*` must reach the
# end of the string with nothing after it.
_ANSI_PARTIAL_TAIL_RE = re.compile(r"\x1b(\[[0-9;]*)?$")


def make_ansi_stream_stripper():
    """Return a `feed(chunk, final=False) -> str` closure for stripping
    ANSI escapes from text arriving in arbitrary-sized pieces (e.g. `log
    -f`'s live tail), where a single escape sequence can be split across
    two reads.

    `_strip_ansi` alone is only safe on a complete string: fed one chunk
    at a time, a sequence like `\\x1b[0;31m` split as `...\\x1b[0;3` /
    `1m...` would leave the first half's `\\x1b[0;3` unmatched (no
    terminating letter in that chunk) and leak straight into the output.
    This buffers any such trailing partial and prepends it to the next
    chunk before stripping again; `final=True` (end of stream) flushes
    whatever's buffered as plain text instead of holding it forever.
    """
    pending = ""

    def feed(chunk: str, final: bool = False) -> str:
        nonlocal pending
        combined = pending + chunk
        if final:
            pending = ""
            return _strip_ansi(combined)
        m = _ANSI_PARTIAL_TAIL_RE.search(combined)
        if m:
            pending = combined[m.start() :]
            return _strip_ansi(combined[: m.start()])
        pending = ""
        return _strip_ansi(combined)

    return feed


def _is_systemexit(out) -> bool:
    """True iff this output is a `raise SystemExit(...)` (a.k.a. `sys.exit`)."""
    return out.get("output_type") == "error" and out.get("ename") == "SystemExit"


def _systemexit_code(out) -> int:
    """Map a SystemExit kernel output back to a CPython-style integer exit code.

    CPython conventions (mirrored):
      - `sys.exit()` / `sys.exit(None)` / `sys.exit(0)` / `sys.exit(False)` -> 0
      - `sys.exit(<int>)`                                -> <int>
      - `sys.exit('msg')` (any non-int)                  -> 1
    """
    evalue = (out.get("evalue") or "").strip()
    if evalue in ("", "None", "0", "False"):
        return 0
    try:
        return int(evalue)
    except ValueError:
        return 1


def _exit_code_from_outputs(outputs) -> int:
    """Derive the CLI's exit code from the kernel's outputs for a single cell.

    A `SystemExit` is treated like CPython would treat the same call from a
    plain `python script.py` invocation. Any *other* error (uncaught
    exception, NameError, etc.) is exit 1.
    """
    code = 0
    for o in outputs:
        if o.get("output_type") != "error":
            continue
        if _is_systemexit(o):
            ec = _systemexit_code(o)
            # Last SystemExit wins, matching the runtime -- and any non-zero
            # eclipses any prior zero.
            code = ec if ec != 0 else code
        else:
            return 1
    return code


def build_envelope(
    status: str,
    command: str,
    exit_code: int = 0,
    reason: Optional[str] = None,
    **extra,
) -> dict:
    """Build a `--json` response envelope.

    Every JSON emitter (exec, run, exec-async, log --tail) funnels its
    response through this, so the envelope shape can't drift between
    commands. `command` (e.g. "exec", "run", "exec-async", "log") names
    which one produced this envelope -- for field debugging/tracing,
    since a caller polling `log --tail` may be looking at a sidecar an
    entirely different process wrote.
    """
    # Imported lazily: auto_update.py imports `state` from this module at
    # its own top level, so a top-level import here would be circular.
    from colab_cli.auto_update import get_app_version

    envelope = {
        "schema_version": SCHEMA_VERSION,
        "cli_version": get_app_version(),
        "command": command,
        "status": status,
        "exit_code": exit_code,
    }
    if reason is not None:
        envelope["reason"] = reason
    envelope.update(extra)
    return envelope


def emit_json(envelope: dict, model: Optional[type] = None) -> None:
    """Print a `--json` envelope to stdout.

    Validates `envelope` against `model` (an `envelopes.py` Pydantic
    model; defaults to `EnvelopeBase`) before printing -- a shape mismatch
    raises `pydantic.ValidationError` immediately rather than shipping a
    silently-wrong field. That's what makes the model an enforced
    invariant instead of just documentation.

    Uses an explicit `file=` so it bypasses the `typer.echo` `--json`
    redirect installed in `cli.py` (that redirect exists to keep
    human-readable chatter off stdout while `--json` is active; this call
    IS the stdout payload it's making room for).
    """
    # Imported lazily: envelopes.py has no reason to import common.py, but
    # keeping the import here (rather than at module top) matches this
    # file's existing pattern for avoiding import-order surprises.
    from colab_cli.envelopes import EnvelopeBase

    (model or EnvelopeBase).model_validate(envelope)
    typer.echo(json.dumps(envelope), file=sys.stdout)


def json_safe_outputs(outputs, strip: bool = True):
    """Return a copy of `outputs` with each error output's `traceback`
    ANSI-stripped by default -- a single field, not stripped-plus-raw.

    IPython ships `traceback` with embedded ANSI SGR escapes as a
    convention, not incidental leakage -- under `--json` we strip by
    default so the field is directly usable as clean text. Pass
    `strip=False` (wired to `--no-strip-ansi`) to keep the original ANSI
    bytes in `traceback` instead, for a caller that wants to re-render it
    in a terminal -- there is no separate raw-copy field either way, only
    the one `traceback` key with its content controlled by `strip`.
    Does not mutate `outputs` -- callers that also feed the same list to
    `state.history.log_event`/notebook-output-saving need the untouched
    original.
    """
    if not strip:
        return outputs
    result = []
    for out in outputs:
        if out.get("output_type") == "error" and out.get("traceback"):
            out = dict(out)
            out["traceback"] = [_strip_ansi(line) for line in out["traceback"]]
        result.append(out)
    return result


class State:
    def __init__(self):
        self.client_oauth_config = os.path.expanduser("~/.colab-cli-oauth-config.json")
        self.config_path = None
        self.logtostderr = False
        self.debug = False
        self.json_output = False
        self.no_strip_ansi = False
        self.auth_provider = AuthProvider.OAUTH2
        self._client = None
        self._store = None
        self._settings_store = None
        self._history = None
        self._sessions = None

    @property
    def store(self):
        if self._store is None:
            self._store = StateStore(self.config_path)
        return self._store

    @property
    def settings_store(self):
        if self._settings_store is None:
            # We don't currently allow overriding settings path via CLI,
            # but we could if needed. For now, use default.
            self._settings_store = SettingsStore()
        return self._settings_store

    @property
    def history(self):
        if self._history is None:
            self._history = HistoryLogger()
        return self._history

    @property
    def client(self):
        if self._client is None:
            creds = get_credentials(
                self.client_oauth_config, provider=self.auth_provider
            )
            self._client = Client(Prod(), creds)
        return self._client

    def prune_session(self, name: str):
        """Removes a session from local state and kills its keep-alive process."""
        s = self.store.get(name)
        if s and s.keep_alive_pid:
            kill_process(s.keep_alive_pid)
        self.store.remove(name)
        if self._sessions and name in self._sessions:
            del self._sessions[name]
        self.history.log_event(name, "session_terminated", {"reason": "pruned"})

    def sync_sessions(self):
        if self._sessions is not None:
            return self._sessions, self.client.list_assignments()

        # Check local store first. If it's empty, we don't necessarily need to hit the backend
        # unless we are specifically looking for server-side assignments (e.g. 'colab sessions').
        local_sessions = self.store.list()
        if not local_sessions:
            self._sessions = {}
            # We still need to return assignments for 'colab sessions' to work
            # But we only trigger client creation (and thus auth) if we have to.
            try:
                assignments = self.client.list_assignments()
            except (SystemExit, AuthenticationError):
                # If auth fails, we just return empty assignments. Catches
                # `AuthenticationError` (ADC creds missing/invalid) --
                # `SystemExit` is kept too even though nothing currently
                # raises it here, since it's cheap insurance against a
                # future auth path reverting to the builtin `exit()` this
                # one replaced.
                assignments = []
            return self._sessions, assignments

        assignments = self.client.list_assignments()
        active_endpoints = {a.endpoint for a in assignments}

        self._sessions = local_sessions
        pruned = 0
        for name, s in list(self._sessions.items()):
            if s.endpoint not in active_endpoints:
                self.prune_session(name)
                pruned += 1

        if pruned > 0:
            typer.echo(f"[colab] Pruned {pruned} stale local session(s).")

        return self._sessions, assignments

    def _resolve_session_failure(self, command: str, reason: str, message: str) -> NoReturn:
        """Shared exit path for `resolve_session`'s two failure shapes.

        `resolve_session` predates `--json` and previously always
        `typer.echo`'d plain text no matter which command called it --
        the one escape hatch left after every *other* error path in
        `exec`/`exec-async`/`stop` was made `--json`-aware, since it's a
        shared helper those commands call before their own JSON-gating
        logic ever runs. Fixed once, centrally, rather than patching each
        of the three call sites separately.
        """
        if self.json_output:
            emit_json(
                build_envelope(
                    "error", command, exit_code=1, reason=reason, message=message
                )
            )
            # The envelope above is the stdout payload; this echo is
            # supplementary and must not share that stream, or it'd corrupt
            # the JSON a `--json` caller is trying to parse. Matches the
            # existing `err=True` convention every other error-alongside-an-
            # envelope call site in this codebase already uses (e.g.
            # exec_command's session_not_found branch).
            typer.echo(f"[colab] Error: {message}", err=True)
        else:
            typer.echo(f"[colab] Error: {message}")
        raise typer.Exit(1)

    def resolve_session(self, session_name: Optional[str], command: str = "cli") -> str:
        if session_name:
            return session_name

        # Check local store first to avoid hitting the backend (and triggering auth) if we don't have to
        local_sessions = self.store.list()
        if not local_sessions:
            self._resolve_session_failure(
                command,
                "no_active_sessions",
                "No active sessions found. Create one with 'colab new'.",
            )

        # If we have local sessions, we need to sync to make sure they are still valid.
        # This will trigger auth if valid credentials are not present.
        sessions, _ = self.sync_sessions()
        active_names = list(sessions.keys())

        if len(active_names) == 1:
            name = active_names[0]
            typer.echo(f"[colab] Using unique session '{name}'.")
            return name
        elif len(active_names) > 1:
            self._resolve_session_failure(
                command,
                "ambiguous_session",
                f"Multiple active sessions found. Specify one with -s: {', '.join(active_names)}",
            )
        else:
            self._resolve_session_failure(
                command,
                "no_active_sessions",
                "No active sessions found. Create one with 'colab new'.",
            )


state = State()


def pid_alive(pid: Optional[int]) -> bool:
    """True if `pid` names a process we can still signal."""
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def kill_process(pid: int):
    """Safely terminates a process by PID."""
    if not pid:
        return
    try:
        os.kill(pid, signal.SIGTERM)
        # Give it a moment to exit
        for _ in range(5):
            time.sleep(0.1)
            os.kill(pid, 0)
    except OSError:
        # Already dead
        pass
    except Exception:
        logging.debug(f"Failed to kill process {pid}")


def setup_logging(log_to_stderr: bool, debug: bool = False):
    """Configure the root logger.

    Defaults to INFO -- DEBUG is opt-in via `--debug`, since third-party
    libraries (urllib3, jupyter_kernel_client, websocket) have no level of
    their own and inherit whatever the root logger is set to, so a
    DEBUG-by-default root logger meant every invocation's log file (and
    stderr, under --logtostderr/--json) filled with their internal chatter
    -- not something a "normal" CLI does by default.
    """
    log_format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    level = logging.DEBUG if debug else logging.INFO
    logger = logging.getLogger()
    logger.setLevel(level)

    requests_log = logging.getLogger("urllib3")
    requests_log.setLevel(level)
    requests_log.propagate = True

    log_dir = os.path.expanduser("~/.config/colab-cli")
    os.makedirs(log_dir, exist_ok=True)
    file_handler = logging.FileHandler(os.path.join(log_dir, "colab.log"))
    file_handler.setFormatter(logging.Formatter(log_format))
    logger.addHandler(file_handler)

    if log_to_stderr:
        stream_handler = logging.StreamHandler(sys.stderr)
        stream_handler.setFormatter(logging.Formatter(log_format))
        logger.addHandler(stream_handler)
