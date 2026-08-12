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
import os
import sys
import time
from typing import Optional

import typer
from typing_extensions import Annotated

from colab_cli import auto_update
from colab_cli.auto_update import get_app_version
from colab_cli.common import build_envelope, emit_json, pid_alive, state
from colab_cli.envelopes import EnvelopeBase, ExecEnvelope

# Poll interval while `log -f` waits for more bytes to be appended to a
# still-running exec-async job's log file.
FOLLOW_POLL_SECONDS = 0.3


def _follow_log(path: str, pid: Optional[int]):
    """Tail `path` (a raw exec-async stdout/stderr log) until `pid` exits.

    Unlike `mighty-colab log`'s JSONL history (written once, after the
    whole `exec` call returns), this file is written -- and flushed -- in
    real time by the background worker's own stdout/stderr, since it's
    just the redirected file descriptors of the running `exec` process.
    """
    while not os.path.exists(path) and pid_alive(pid):
        time.sleep(FOLLOW_POLL_SECONDS)

    if not os.path.exists(path):
        typer.echo(f"[colab] Log file '{path}' not found.", err=True)
        raise typer.Exit(1)

    with open(path, "r") as f:
        while True:
            chunk = f.read()
            if chunk:
                sys.stdout.write(chunk)
                sys.stdout.flush()
                continue
            if not pid_alive(pid):
                # One last read in case the process wrote and exited
                # between our last read and the liveness check above.
                chunk = f.read()
                if chunk:
                    sys.stdout.write(chunk)
                    sys.stdout.flush()
                return
            time.sleep(FOLLOW_POLL_SECONDS)


def _tail_log_once(path: str, lines: Optional[int]):
    """Print `path`'s current content once and return -- no polling, no
    liveness check. Unlike `_follow_log`, this never blocks: a still-running
    job's partial output and a finished job's complete output are read the
    same way. Safe to expose over MCP for exactly that reason.
    """
    if not os.path.exists(path):
        typer.echo(f"[colab] Log file '{path}' not found.", err=True)
        raise typer.Exit(1)

    with open(path, "r") as f:
        content = f.read()

    if lines:
        content = "\n".join(content.splitlines()[-lines:])
        if content:
            content += "\n"

    sys.stdout.write(content)
    sys.stdout.flush()


def _tail_log_json(log_path: str, pid: Optional[int], since_offset: Optional[int]):
    """`--tail --json`: report an `exec-async` job's outcome plus any log
    bytes written since `since_offset`, without polling or blocking.

    Four states, in priority order:
      - Neither the log file nor a sidecar exists: no job ever ran at this
        path (`spawn_exec_async` creates the log file before `Popen`, so its
        absence isn't "the worker died", it's "there was never a worker") --
        `status="error", reason="no_job_found"`, same as the sibling check
        in `log()` for a session with no `exec_log_path` at all.
      - A sidecar (`<log_path>.json`) exists and parses: the job finished --
        return its envelope fields (status/exit_code/reason/...) as-is, with
        `content`/`next_offset` merged in. `done` is implied by `status`.
      - No sidecar, `pid` still alive: `status="running"`.
      - No sidecar, `pid` not alive (or unknown, e.g. after the session
        record itself is gone): `status="error", reason="worker_terminated"`
        -- mighty-colab can't tell *why* the worker ended without a result
        (killed, crashed, OOM, a caller's own `stop`), so it reports one
        honest code rather than guessing.
    """
    sidecar_path = f"{log_path}.json"
    if not os.path.exists(log_path) and not os.path.exists(sidecar_path):
        emit_json(build_envelope("error", "log", exit_code=1, reason="no_job_found"))
        raise typer.Exit(1)

    offset = since_offset or 0
    content = ""
    next_offset = offset
    if os.path.exists(log_path):
        with open(log_path, "rb") as f:
            f.seek(offset)
            new_bytes = f.read()
        content = new_bytes.decode("utf-8", errors="replace")
        next_offset = offset + len(new_bytes)

    if os.path.exists(sidecar_path):
        try:
            with open(sidecar_path, "r", encoding="utf-8") as f:
                envelope = json.load(f)
        except (json.JSONDecodeError, OSError):
            envelope = None
        if envelope is not None:
            envelope["content"] = content
            envelope["next_offset"] = next_offset
            # The sidecar's own "command" field is left as-is (it names
            # whichever command wrote it -- always "exec", since sidecars
            # are only ever produced by exec-async's spawned child, never
            # by `log` itself). It's error-shaped if the async child's own
            # preflight/execution failed before producing any blocks.
            model = EnvelopeBase if envelope.get("status") == "error" else ExecEnvelope
            emit_json(envelope, model=model)
            return

    if pid_alive(pid):
        emit_json(
            build_envelope("running", "log", content=content, next_offset=next_offset)
        )
    else:
        emit_json(
            build_envelope(
                "error",
                "log",
                exit_code=1,
                reason="worker_terminated",
                content=content,
                next_offset=next_offset,
            )
        )


def pay():
    """Open the Colab signup page to manage compute units"""
    import webbrowser

    url = "https://colab.research.google.com/signup"
    typer.echo(f"[colab] Opening {url}...")
    webbrowser.open(url)


def url(
    session: Annotated[
        Optional[str], typer.Option("-s", "--session", help="Session name")
    ] = None,
    host: Annotated[
        str,
        typer.Option(
            "--host",
            help=(
                "Colab frontend host (origin) to use for the URL. The Colab "
                "frontend resolves `dbu` against `window.location.origin`, "
                "so this only changes the page origin, not the embedded "
                "backend path."
            ),
        ),
    ] = "https://colab.research.google.com",
    open_browser: Annotated[
        bool,
        typer.Option(
            "--open",
            help=(
                "After printing the URL, also open it in the system browser. "
                "Off by default so the command remains pipeable "
                "(e.g. `colab url -s s1 | xclip`)."
            ),
        ),
    ] = False,
):
    """Print a browser URL that connects to an existing session.

    Format: ``https://<host>/notebooks/empty.ipynb?dbu=<urlencoded path>#datalabBackendUrl=<host>/tun/m/<endpoint>``,
    where the path is ``/tun/m/<endpoint>``. When opened, the Colab frontend
    skips ``/tun/m/assign`` and attaches the kernel to our existing VM.

    Two backend-URL signals are embedded:

    - ``?dbu=<urlencoded path>`` — the ``datalab_backend_url`` development
      query flag. The frontend resolves the value against
      ``window.location.origin``.

    - ``#datalabBackendUrl=<full URL>`` — the hash-fragment form. Some
      frontend code paths consult this first and ignore ``dbu``, so we
      emit both for robustness. The fragment value is a FULL URL (with
      scheme + host) and is intentionally NOT URL-encoded — browsers do
      not decode the fragment before passing ``location.hash`` to page
      JS, and Colab's hash parser expects the raw string.

    The fragment's host always matches ``--host`` (the page origin), so
    Colab's same-origin enforcement on the embedded backend URL doesn't
    block the connection, and sandbox/dev users get a sandbox fragment
    automatically.
    """
    # Imported here (not at module top) to mirror the lazy-state pattern used
    # elsewhere in this module and avoid a circular import via colab_cli.common.
    from urllib.parse import quote

    from colab_cli.common import state

    name = state.resolve_session(session)
    s = state.store.get(name)
    if not s:
        typer.echo(f"[colab] Session '{name}' not found.", err=True)
        raise typer.Exit(code=1)

    # Strip a trailing slash so we don't produce `https://host//notebooks/...`
    # or `https://host//tun/m/...` in the fragment URL.
    host_clean = host.rstrip("/")
    backend_path = f"/tun/m/{s.endpoint}"
    # `dbu` value is the backend path. URL-encode it (incl. the slashes via
    # `safe=""`) so the value survives any downstream non-strict query-string
    # re-parsing — this is also the form shown in real Colab connect URLs.
    dbu_value = quote(backend_path, safe="")
    # `#datalabBackendUrl=` value is the FULL backend URL, raw (un-encoded):
    # the browser does not decode the fragment before passing it to page JS,
    # and Colab's hash parser calls `new URL(rawString)` directly. Pinning
    # the host to `host_clean` (not hardcoding research.google.com) keeps
    # this aligned with the page origin so same-origin enforcement passes
    # for sandbox / dev hosts too.
    fragment_value = f"{host_clean}{backend_path}"
    connect_url = (
        f"{host_clean}/notebooks/empty.ipynb"
        f"?dbu={dbu_value}"
        f"#datalabBackendUrl={fragment_value}"
    )

    # Print the URL on its own line with no `[colab]` prefix so the output
    # is pipeable (`colab url -s s1 | xclip`, etc.).
    typer.echo(connect_url)

    if open_browser:
        import webbrowser

        webbrowser.open(connect_url)


def log(
    session: Annotated[
        Optional[str],
        typer.Option(
            "-s",
            "--session",
            help="Session name (if omitted, lists all sessions with logs)",
        ),
    ] = None,
    lines: Annotated[
        Optional[int],
        typer.Option(
            "-n", "--lines", help="Number of lines to show/export (default: all)"
        ),
    ] = None,
    type: Annotated[
        Optional[str],
        typer.Option(
            "-t",
            "--type",
            help="Filter by event type (e.g., execution, file_operation)",
        ),
    ] = None,
    output: Annotated[
        Optional[str],
        typer.Option(
            "-o",
            "--output",
            help="Output file path (suffix determines format: .ipynb, .md, .txt, .jsonl)",
        ),
    ] = None,
    follow: Annotated[
        bool,
        typer.Option(
            "-f",
            "--follow",
            help=(
                "Follow the live stdout/stderr of a running `exec-async` "
                "background job (requires -s). Unlike the history view "
                "below, this streams in real time."
            ),
        ),
    ] = False,
    tail: Annotated[
        bool,
        typer.Option(
            "--tail",
            help=(
                "Print a running or finished `exec-async` job's current "
                "stdout/stderr once and exit (requires -s) -- unlike "
                "--follow, never blocks or polls. Combine with -n for the "
                "last N lines only."
            ),
        ),
    ] = False,
    since_offset: Annotated[
        Optional[int],
        typer.Option(
            "--since-offset",
            help=(
                "With --tail --json: only return log bytes written after "
                "this byte offset (the previous response's `next_offset`), "
                "instead of re-reading the whole file every poll."
            ),
        ),
    ] = None,
):
    """Manage and view session history logs"""
    if state.json_output and not tail:
        # `--json` only has a defined shape for `log --tail --json` (the
        # sidecar/pid-liveness envelope in `_tail_log_json`). Every other
        # mode here (`--follow`, or the default history listing) just
        # prints prose -- which the `_json_aware_echo` wrapper would
        # silently redirect entirely to stderr, leaving a `--json` caller's
        # stdout empty with no envelope and no warning. Same fallback this
        # codebase already uses for a `--json`-unsupported command
        # entirely (see cli.py's callback()), applied at flag-combination
        # granularity here since `log` is JSON-capable but only partially.
        state.json_output = False
        typer.echo(
            "[colab] --json has no effect on 'log' without --tail; "
            "supported: `log --tail --json`.",
            err=True,
        )
    if follow or tail:
        if not session:
            flag = "--follow" if follow else "--tail"
            if tail and state.json_output:
                emit_json(
                    build_envelope(
                        "error", "log", exit_code=2, reason="missing_session"
                    )
                )
            typer.echo(f"[colab] {flag} requires --session/-s.", err=True)
            raise typer.Exit(2)

        s = state.store.get(session)
        if tail and state.json_output:
            if s and s.exec_log_path:
                log_path, pid = s.exec_log_path, s.exec_pid
            elif not s:
                # The session record is gone (e.g. after `stop`), but the
                # default log path -- and its sidecar -- are derivable from
                # the session name alone and outlive that record. A custom
                # --output-log path is not recoverable here: only the
                # session record knew it.
                log_path = os.path.join(state.history.log_dir, f"{session}.exec.log")
                pid = None
            else:
                emit_json(
                    build_envelope("error", "log", exit_code=1, reason="no_job_found")
                )
                typer.echo(
                    f"[colab] No background exec-async job found for session "
                    f"'{session}'. Start one with `mighty-colab exec-async`.",
                    err=True,
                )
                raise typer.Exit(1)
            _tail_log_json(log_path, pid, since_offset)
            return

        if not s or not s.exec_log_path:
            typer.echo(
                f"[colab] No background exec-async job found for session "
                f"'{session}'. Start one with `mighty-colab exec-async`.",
                err=True,
            )
            raise typer.Exit(1)
        if follow:
            _follow_log(s.exec_log_path, s.exec_pid)
        else:
            _tail_log_once(s.exec_log_path, lines)
        return

    if not session:
        sessions_with_logs = state.history.list_sessions()
        if not sessions_with_logs:
            typer.echo("[colab] No session history found.")
        else:
            typer.echo("[colab] Sessions with history logs:")
            for n in sorted(sessions_with_logs):
                typer.echo(f"  {n}")
        return

    events = state.history.get_history(session)
    if not events:
        typer.echo(f"[colab] No history found for session '{session}'.")
        return

    if type:
        events = [e for e in events if e.get("event_type") == type]

    if lines:
        events = events[-lines:]

    if output:
        from colab_cli.converter import export_history

        export_history(events, session, output)
    else:
        for event in events:
            ts = event.get("timestamp", "").split(".")[0].replace("T", " ")
            etype = event.get("event_type", "unknown")

            if etype == "execution":
                preview = event.get("code", "").strip().split("\n")[0][:60]
                typer.echo(f"[{ts}] EXEC: {preview}...")
            elif etype == "file_operation":
                typer.echo(
                    f"[{ts}] FILE: {event.get('op')} {event.get('path', event.get('remote', ''))}"
                )
            elif etype == "automation":
                typer.echo(f"[{ts}] AUTO: {event.get('op')}")
            elif etype == "stdin_request":
                typer.echo(f"[{ts}] INPT: {event.get('prompt', '').strip()}")
            elif etype == "input_reply":
                typer.echo(f"[{ts}] RPLY: {event.get('value', '').strip()}")
            elif etype == "keep_alive_started":
                typer.echo(
                    f"[{ts}] KEEP: started endpoint={event.get('endpoint')} pid={event.get('pid')}"
                )
            elif etype == "keep_alive_error":
                msg = (
                    f"[{ts}] KEEP: error iter={event.get('iteration')} "
                    f"status={event.get('status_code')} "
                    f"type={event.get('error_type')} "
                    f"msg={event.get('error', '')[:120]}"
                )
                body = event.get("response_body")
                if body:
                    msg += f" body={body[:300]}"
                typer.echo(msg)
            elif etype == "keep_alive_stopped":
                msg = (
                    f"[{ts}] KEEP: stopped reason={event.get('reason')} "
                    f"iters={event.get('iterations')} "
                    f"duration={event.get('duration_seconds')}s"
                )
                last_err = event.get("last_error")
                if last_err:
                    msg += (
                        f" last_error=[status={last_err.get('status_code')} "
                        f"type={last_err.get('error_type')} "
                        f"msg={str(last_err.get('error', ''))[:120]}]"
                    )
                if event.get("expected_endpoint") or event.get("actual_endpoint"):
                    msg += (
                        f" expected={event.get('expected_endpoint')} "
                        f"actual={event.get('actual_endpoint')}"
                    )
                typer.echo(msg)
            else:
                typer.echo(f"[{ts}] EVENT: {etype}")


def whoami():
    """[debug] Print the active credentials' identity, scopes, and expiry.

    Mints an access token using the same path the rest of the CLI uses
    (`auth.get_credentials(...)` honoring the global `--auth=...` flag),
    then queries Google's tokeninfo endpoint and prints a human-readable
    summary. Useful when debugging "why is my call to
    colab.pa.googleapis.com 403-ing" — the answer is almost always a
    missing scope or a token whose `email` doesn't match what you
    expected.

    Hidden from `colab --help` because end users shouldn't need it; reach
    it via `colab whoami --help` or by knowing the name.
    """
    import json
    import urllib.error
    import urllib.parse
    import urllib.request

    from colab_cli.auth import get_credentials

    provider = state.auth_provider

    # Mint a fresh token. Some credential types (service-account, GCE, some
    # impersonated creds) don't populate `.token` until refresh() is called,
    # so we always refresh — cheap, ~1 RPC, and avoids a confusing
    # `creds.token is None` failure mode for valid credentials.
    sess = get_credentials(state.client_oauth_config, provider=provider)
    creds = sess.credentials
    try:
        from google.auth.transport.requests import Request as _GoogleAuthRequest

        creds.refresh(_GoogleAuthRequest())
    except Exception as e:
        typer.echo(f"[colab] whoami: failed to refresh credentials: {e}", err=True)
        raise typer.Exit(code=1)

    token = creds.token
    if not token:
        typer.echo(
            "[colab] whoami: credentials have no access token after refresh; "
            "the auth provider may have failed silently.",
            err=True,
        )
        raise typer.Exit(code=1)

    # Hit Google's tokeninfo endpoint. We use stdlib urllib (rather than the
    # already-authorized `sess`) deliberately: tokeninfo accepts the token as
    # a query parameter and does NOT want a Bearer header alongside it.
    qs = urllib.parse.urlencode({"access_token": token})
    url = f"https://oauth2.googleapis.com/tokeninfo?{qs}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            body = resp.read().decode("utf-8")
            info = json.loads(body)
    except urllib.error.HTTPError as e:
        # tokeninfo returns 400 for invalid/expired/revoked tokens with a
        # JSON body like {"error":"invalid_token","error_description":"..."}.
        # Surface that body so the developer can see *why* it was rejected.
        try:
            err_body = e.read().decode("utf-8")
        except Exception:
            err_body = ""
        typer.echo(
            f"[colab] whoami: tokeninfo returned HTTP {e.code}: {err_body or e.reason}",
            err=True,
        )
        raise typer.Exit(code=1)
    except Exception as e:
        typer.echo(f"[colab] whoami: tokeninfo request failed: {e}", err=True)
        raise typer.Exit(code=1)

    # Format. Provider name from the AuthProvider enum (e.g. "adc"); email
    # may be missing for tokens scoped without `userinfo.email`, in which
    # case we say so explicitly rather than printing "Email: None".
    email = info.get("email") or "<unavailable: token has no userinfo.email scope>"
    expires_in = info.get("expires_in")
    try:
        expires_min = int(expires_in) // 60
        expires_str = f"{expires_min}m"
    except (TypeError, ValueError):
        expires_str = str(expires_in) if expires_in else "<unknown>"

    audience = info.get("audience") or info.get("aud") or "<none>"
    scopes = (info.get("scope") or "").split()

    typer.echo(f"Auth provider: {provider.value}")
    typer.echo(f"Email:         {email}")
    typer.echo(f"Audience:      {audience}")
    typer.echo(f"Expires in:    {expires_str}")
    if scopes:
        typer.echo("Scopes:")
        for s in sorted(scopes):
            typer.echo(f"  - {s}")
    else:
        typer.echo("Scopes:        <none>")


def version_command():
    """Show the version of the Colab CLI"""
    typer.echo(f"Version: {get_app_version()}")


def update_command(
    install: Annotated[
        bool,
        typer.Option(
            "--install",
            help=(
                "After checking, run 'pip install -U mighty-colab' to "
                "upgrade the CLI in place. No-op if already up to date. "
                "Linux only."
            ),
        ),
    ] = False,
):
    """Check for the latest available version"""
    auto_update.check_for_updates(quiet=False)
    if not install:
        return

    if not auto_update.is_self_install_supported():
        typer.echo(
            "[colab] '--install' self-install is only supported on Linux and macOS.",
            err=True,
        )
        raise typer.Exit(code=1)

    # Skip the install when the current version already matches (or exceeds)
    # the latest known version, to avoid an unnecessary subprocess call.
    settings = state.settings_store.load()
    if settings.latest_version and not auto_update._is_newer(
        settings.latest_version, auto_update.get_app_version()
    ):
        return

    auto_update.self_install()


def _print_resource(filename: str) -> None:
    import importlib.resources
    import os

    content = None
    try:
        # Try reading from package resources
        ref = importlib.resources.files("colab_cli").joinpath(filename)
        if ref.is_file():
            content = ref.read_text(encoding="utf-8")
    except Exception:
        pass

    if not content:
        # Fallback to local file for development
        local_path = os.path.abspath(
            os.path.join(os.path.dirname(__file__), f"../../../{filename}")
        )
        if os.path.exists(local_path):
            try:
                with open(local_path, "r", encoding="utf-8") as f:
                    content = f.read()
            except Exception:
                pass

    if content:
        typer.echo(content)
    else:
        typer.echo(f"[colab] {filename} content not available.", err=True)
        raise typer.Exit(code=1)


def readme():
    """Print the bundled README.md file"""
    _print_resource("README.md")


def skill():
    """Print the bundled SKILL.md file"""
    _print_resource("SKILL.md")


def register(app: typer.Typer):
    app.command()(pay)
    app.command()(log)
    app.command(name="url")(url)
    app.command(name="version")(version_command)
    app.command(name="update")(update_command)
    # Developer-only debugging aid; hidden from `colab --help` but still
    # reachable via `colab whoami` / `colab whoami --help`.
    app.command(name="whoami", hidden=True)(whoami)
    app.command(name="readme")(readme)
    app.command(name="README", hidden=True)(readme)
    app.command(name="skill")(skill)
    app.command(name="SKILL", hidden=True)(skill)
