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

import os
import subprocess
import sys
import time
import uuid
from typing import Any, Dict, Optional
import typer
from typing_extensions import Annotated

from colab_cli.client import (
    Accelerator,
    ColabRequestError,
    PostAssignmentResponse,
    Variant,
)
from colab_cli.utils import get_status_code
from colab_cli.state import SessionState
from colab_cli.runtime import ColabRuntime


def _is_scope_error(e: Exception) -> bool:
    """True if a ColabRequestError's response body indicates a missing OAuth scope.

    The frontend returns a `google.rpc.Status` with `code=7` (PERMISSION_DENIED)
    and a `DebugInfo` payload mentioning `SCOPE_NOT_PERMITTED` /
    "insufficient authentication scopes". Match on either substring so we
    don't depend on the exact wording of one of them.
    """
    body = getattr(e, "response_body", None) or ""
    body_str = str(body)
    return (
        "SCOPE_NOT_PERMITTED" in body_str
        or "insufficient authentication scopes" in body_str
    )


def _scope_remediation_message(provider) -> str:
    """User-facing remediation hint, tailored per auth provider.

    Keep-alive is a Tunnel Frontend ping against the Colab session backend
    (colab.research.google.com), authenticated with the user's own Gaia bearer
    token — the same credential and host used to assign the VM. A missing-scope
    error here is rare (assignment would normally have failed first), but if it
    happens the fix is to re-authenticate with the standard Colab scopes.
    """
    # Importing locally to avoid a circular import at module load time.
    from colab_cli.auth import AuthProvider

    common = (
        "Keeping the session alive requires valid Colab credentials for "
        "colab.research.google.com."
    )
    if provider == AuthProvider.ADC:
        return (
            f"{common}\n"
            "Re-authenticate ADC with the standard Colab scopes (the "
            "cloud-platform and openid scopes are required by gcloud itself):\n"
            "  gcloud auth application-default login \\\n"
            "      --scopes=openid,"
            "https://www.googleapis.com/auth/cloud-platform,"
            "https://www.googleapis.com/auth/userinfo.email,"
            "https://www.googleapis.com/auth/colaboratory\n"
            "Then re-run `colab new`."
        )
    # OAuth2 (and any future provider) fallback.
    return (
        f"{common}\n"
        "Delete the cached token at ~/.config/colab-cli/token.json and "
        "re-run `colab new` to trigger a fresh consent flow."
    )


def _hardware_label(accelerator: str) -> str:
    """`NONE` -> `CPU`; everything else passes through."""
    return "CPU" if accelerator == "NONE" else accelerator


def _format_session_line(
    name: str,
    endpoint: str,
    accelerator: str,
    variant: str,
    status: Optional[str] = None,
) -> str:
    """Single source of truth for session display lines.

    Format: ``[name] endpoint | Hardware: X | Variant: Y[ | Status: Z]``.
    Use ``"?"`` as the name for orphaned server-side assignments with no local
    state.
    """
    parts = [
        f"[{name}] {endpoint}",
        f"Hardware: {_hardware_label(accelerator)}",
        f"Variant: {variant}",
    ]
    if status is not None:
        parts.append(f"Status: {status}")
    return " | ".join(parts)


def new(
    session: Annotated[
        Optional[str], typer.Option("-s", "--session", help="Session name")
    ] = None,
    tpu: Annotated[
        Optional[str],
        typer.Option(
            help="TPU accelerator variant. Supported: v5e1, v6e1.",
        ),
    ] = None,
    gpu: Annotated[
        Optional[str],
        typer.Option(
            help=(
                "GPU accelerator variant. Supported: T4, L4, G4, H100, A100."
                "\n\nIf omitted (along with --tpu), a CPU runtime is created."
                "\n\nAvailability varies by Colab subscription tier."
            ),
        ),
    ] = None,
):
    """Create a new session"""
    from colab_cli.common import build_envelope, emit_json, state
    from colab_cli.envelopes import NewSessionEnvelope

    name = session or uuid.uuid4().hex[:6]
    variant = Variant.DEFAULT
    accelerator = Accelerator.NONE

    if tpu:
        variant = Variant.TPU
        accelerator = Accelerator.V5E1 if tpu.lower() == "v5e1" else Accelerator.V6E1
    elif gpu:
        variant = Variant.GPU
        mapping = {
            "a100": Accelerator.A100,
            "h100": Accelerator.H100,
            "l4": Accelerator.L4,
            "t4": Accelerator.T4,
            "g4": Accelerator.G4,
        }
        accelerator = mapping.get(gpu.lower(), Accelerator.A100)

    typer.echo(f"[colab] Creating session '{name}'...")
    try:
        res = state.client.assign(
            uuid.uuid4(), variant=variant, accelerator=accelerator
        )
    except Exception as e:
        # The Colab backend returns 400 when the caller is not entitled to the
        # requested accelerator (e.g. no A100 quota). Translate that to a
        # friendly, actionable message instead of a raw traceback. We only
        # interpret it this way when an accelerator was actually requested;
        # otherwise we fall through to the generic wrap below. Confirmed live
        # (docs/AGENT_USABILITY_LEARNINGS.md): the 400 body is Google's
        # generic frontend error page, not structured JSON -- there's no
        # finer-grained reason (quota vs. entitlement) to recover here, so
        # one honest reason code, not two guessed ones.
        if (
            isinstance(e, ColabRequestError)
            and get_status_code(e) == 400
            and accelerator != Accelerator.NONE
        ):
            if state.json_output:
                emit_json(
                    build_envelope(
                        "error",
                        "new",
                        exit_code=1,
                        reason="accelerator_rejected",
                        http_status=400,
                    )
                )
            typer.echo(
                f"[colab] Backend rejected accelerator '{accelerator.value}'. "
                "You may not have quota or entitlement for this accelerator on "
                "your account. Try a different one (e.g. --gpu T4) or omit "
                "--gpu/--tpu for a CPU runtime.",
                err=True,
            )
            raise typer.Exit(code=1)
        # Genuine unexpected failure (any other ColabRequestError, or a
        # non-HTTP failure like a network error) -- best-effort: give
        # --json callers a JSON body instead of a bare traceback before it
        # propagates unchanged (non-json behavior is untouched by this).
        if state.json_output:
            emit_json(
                build_envelope(
                    "error",
                    "new",
                    exit_code=1,
                    reason="new_failed",
                    http_status=get_status_code(e),
                )
            )
        raise

    if isinstance(res, PostAssignmentResponse):
        token = res.runtime_proxy_info.token
        url = res.runtime_proxy_info.url
        endpoint = res.endpoint
    else:
        token = (
            res.runtime_proxy_info.token
            if hasattr(res, "runtime_proxy_info")
            else getattr(res, "runtime_proxy_token", "")
        )
        url = res.runtime_proxy_info.url if hasattr(res, "runtime_proxy_info") else ""
        endpoint = res.endpoint

    # Importing locally to avoid a top-level circular import via auth.

    s = SessionState(
        name=name,
        token=token,
        url=url,
        endpoint=endpoint,
        variant=variant.value,
        accelerator=accelerator.value,
    )

    # Pre-flight the keep-alive ping once. If it returns a 403 caused by
    # missing OAuth scopes we know the daemon will fail and the VM would be
    # idle-pruned. Catch it now so we (a) never leak a billable assignment,
    # (b) surface an actionable remediation instead of a session that quietly
    # disappears a few minutes later.
    try:
        state.client.keep_alive_assignment(endpoint)
    except ColabRequestError as e:
        if get_status_code(e) == 403 and _is_scope_error(e):
            if state.json_output:
                emit_json(
                    build_envelope(
                        "error",
                        "new",
                        exit_code=1,
                        reason="auth_scope_missing",
                        http_status=403,
                    )
                )
            typer.echo(
                "[colab] Keep-alive pre-flight failed: your credentials "
                "are missing an OAuth scope required by Colab.\n",
                err=True,
            )
            typer.echo(_scope_remediation_message(state.auth_provider), err=True)
            # Don't leak the assignment we just created.
            try:
                state.client.unassign(endpoint)
            except Exception:
                pass
            raise typer.Exit(code=1)
        # Other failures: don't block session creation — the daemon will
        # retry and log via the existing keep_alive_error event path.

    # Persist the session BEFORE spawning the daemon so the daemon's
    # initial `state.store.get(session_name)` check doesn't race and
    # exit with `reason=session_not_found`. We re-persist below to also
    # capture the daemon PID.
    state.store.add(s)
    s.keep_alive_pid = spawn_keep_alive(
        endpoint,
        name,
        auth_provider=state.auth_provider,
        config_path=state.config_path,
    )

    state.store.add(s)
    state.history.log_event(
        name,
        "session_created",
        {
            "endpoint": endpoint,
            "variant": variant.value,
            "accelerator": accelerator.value,
        },
    )
    typer.echo("[colab] Session READY.")
    if state.json_output:
        emit_json(
            build_envelope(
                "ok",
                "new",
                session=name,
                endpoint=endpoint,
                variant=variant.value,
                accelerator=accelerator.value,
            ),
            model=NewSessionEnvelope,
        )


def restart_kernel(
    session: Annotated[
        Optional[str], typer.Option("-s", "--session", help="Session name")
    ] = None,
):
    """Restart a session's kernel"""
    from colab_cli.common import state

    name = state.resolve_session(session)
    s = state.store.get(name)
    if not s:
        typer.echo(f"[colab] Session '{name}' not found.", err=True)
        raise typer.Exit(1)

    def on_started(kid):
        s.kernel_id = kid
        state.store.add(s)

    def on_sess_started(sid):
        s.session_id = sid
        state.store.add(s)

    runtime = ColabRuntime(
        s.url,
        s.token,
        kernel_id=s.kernel_id,
        session_id=s.session_id,
        on_kernel_started=on_started,
        on_session_started=on_sess_started,
    )

    try:
        runtime.restart()
    finally:
        runtime.stop()


def sessions_command():
    """List all active sessions"""
    from colab_cli.common import build_envelope, emit_json, state
    from colab_cli.envelopes import SessionListEnvelope

    sessions, assignments = state.sync_sessions()
    if not assignments:
        if state.json_output:
            emit_json(
                build_envelope("ok", "sessions", sessions=[]),
                model=SessionListEnvelope,
            )
        typer.echo("[colab] No active sessions found on server.")
        return

    # Build endpoint -> local-name lookup so we can lead with the friendly name.
    name_by_endpoint = {s.endpoint: s.name for s in sessions.values()}
    session_infos = []
    for a in assignments:
        name = name_by_endpoint.get(a.endpoint, "?")
        # `a.variant` is an int-valued AssignmentVariant (DEFAULT=0/GPU=1/TPU=2);
        # its `.name` matches the user-facing string Variant enum, which is what
        # `status` shows for locally-tracked sessions.
        typer.echo(
            _format_session_line(
                name=name,
                endpoint=a.endpoint,
                accelerator=a.accelerator.value,
                variant=a.variant.name,
            )
        )
        session_infos.append(
            {
                "name": name,
                "endpoint": a.endpoint,
                "accelerator": a.accelerator.value,
                "variant": a.variant.name,
            }
        )

    if state.json_output:
        emit_json(
            build_envelope("ok", "sessions", sessions=session_infos),
            model=SessionListEnvelope,
        )


def _print_status_for(s: SessionState) -> dict:
    """Print one session's status line plus optional last-execution detail.

    Returns the same data as a plain dict (a `SessionInfo`-shaped payload)
    for `--json` callers, so the human-readable and machine-readable paths
    can't drift apart from computing the busy/idle string twice.
    """
    status_str = f"BUSY ({s.running})" if s.running else "IDLE"
    typer.echo(
        _format_session_line(
            name=s.name,
            endpoint=s.endpoint,
            accelerator=s.accelerator,
            variant=s.variant,
            status=status_str,
        )
    )
    info = {
        "name": s.name,
        "endpoint": s.endpoint,
        "accelerator": s.accelerator,
        "variant": s.variant,
        "status": status_str,
    }
    if s.last_execution:
        exec_file, exec_cell, exec_time = s.last_execution
        cell_str = f" | Cell: {exec_cell}" if exec_cell else ""
        typer.echo(f"  Last Execution: {exec_file}{cell_str} at {exec_time}")
        info["last_execution_file"] = exec_file
        info["last_execution_cell"] = exec_cell
        info["last_execution_time"] = exec_time
    if s.exec_log_path:
        typer.echo(f"  Log: {s.exec_log_path}")
        info["exec_log_path"] = s.exec_log_path
    return info


def status(
    session: Annotated[
        Optional[str], typer.Option("-s", "--session", help="Session name")
    ] = None,
):
    """Show session status"""
    from colab_cli.common import build_envelope, emit_json, state
    from colab_cli.envelopes import SessionListEnvelope, StatusSingleEnvelope

    local_sessions, _ = state.sync_sessions()
    if session:
        s = state.store.get(session)
        if s:
            info = _print_status_for(s)
            if state.json_output:
                emit_json(
                    build_envelope("ok", "status", session=info),
                    model=StatusSingleEnvelope,
                )
        else:
            if state.json_output:
                # `status` is a query command -- per the design principle
                # already recorded in docs/AGENT_USABILITY_LEARNINGS.md
                # ("query commands should error on 'not found'; desired-
                # state commands like `stop` should not"), --json opts
                # into the correct behavior here, diverging from the
                # plain-text path's exit-0 no-op below (kept unchanged,
                # to avoid a breaking change for anything already parsing
                # today's prose output).
                emit_json(
                    build_envelope(
                        "error", "status", exit_code=1, reason="session_not_found"
                    )
                )
                typer.echo(f"[colab] Session '{session}' not found.", err=True)
                raise typer.Exit(1)
            typer.echo(f"[colab] Session '{session}' not found.")
        return

    if not local_sessions:
        if state.json_output:
            emit_json(
                build_envelope("ok", "status", sessions=[]),
                model=SessionListEnvelope,
            )
        typer.echo("[colab] No active sessions.")
        return
    infos = [_print_status_for(s) for s in local_sessions.values()]
    if state.json_output:
        emit_json(
            build_envelope("ok", "status", sessions=infos),
            model=SessionListEnvelope,
        )


def stop(
    session: Annotated[
        Optional[str], typer.Option("-s", "--session", help="Session name")
    ] = None,
):
    """Stop a session"""
    from colab_cli.common import build_envelope, emit_json, state
    from colab_cli.envelopes import StopEnvelope

    name = state.resolve_session(session, command="stop")
    s = state.store.get(name)
    if not s:
        # Idempotent by design: "not found" means the postcondition (no
        # session) already holds, not that teardown failed. Callers doing
        # unconditional cleanup after a possibly-failed `new`/`exec` must be
        # able to call `stop` without it turning "nothing to clean up" into
        # a false-positive failure/leak signal. --json mirrors that: still
        # status="ok", not an error -- exactly the example the consumer
        # agent behind this design gave for what a desired-state command
        # should report on an absent target.
        if state.json_output:
            emit_json(
                build_envelope(
                    "ok", "stop", session=name, reason="already_stopped"
                ),
                model=StopEnvelope,
            )
        typer.echo(f"[colab] Session '{name}' not found.")
        return

    typer.echo(f"[colab] Stopping session '{name}'...")
    if s.keep_alive_pid:
        from colab_cli.common import kill_process

        kill_process(s.keep_alive_pid)
    if s.exec_pid:
        from colab_cli.common import kill_process

        kill_process(s.exec_pid)

    try:
        runtime = ColabRuntime(s.url, s.token, kernel_id=s.kernel_id)
        runtime.stop(shutdown_kernel=True)
    except Exception:
        pass

    try:
        state.client.unassign(s.endpoint)
    except Exception as e:
        # Distinguish a genuine teardown failure from the idempotent
        # not-found case above: non-zero exit instead of an unhandled
        # traceback, and local state is kept (not removed) so the VM isn't
        # silently forgotten while it may still be billing.
        if state.json_output:
            emit_json(
                build_envelope(
                    "error",
                    "stop",
                    exit_code=1,
                    reason="unassign_failed",
                    http_status=get_status_code(e),
                    session=name,
                ),
                model=StopEnvelope,
            )
        typer.echo(
            f"[colab] Failed to unassign '{name}' -- the VM may still be "
            f"billing. Local tracking was kept so you can retry with "
            f"`colab stop -s {name}`.",
            err=True,
        )
        raise typer.Exit(1)

    state.store.remove(name)
    state.history.log_event(name, "session_terminated", {"reason": "user_requested"})
    typer.echo("[colab] Session terminated.")
    if state.json_output:
        emit_json(build_envelope("ok", "stop", session=name), model=StopEnvelope)


def spawn_keep_alive(
    endpoint: str, session_name: str, auth_provider=None, config_path=None
):
    """Spawns a detached keep-alive process.

    Both `auth_provider` and `config_path` are propagated as global flags
    so the detached child uses the same authentication strategy AND the
    same session state file as the parent that invoked `colab new`.
    Without this, the child inherits Typer's defaults (`--auth=oauth2`,
    `--config=~/.config/colab-cli/sessions.json`), which causes:
      (a) wrong auth backend, and
      (b) the daemon's `state.store.get(session_name)` check finds nothing
          and exits with `reason=session_not_found` when the parent used
          `--config` to write to a non-default path.
    """
    cmd = [sys.executable, "-m", "colab_cli.cli"]
    if auth_provider is not None:
        cmd.append(f"--auth={auth_provider.value}")
    if config_path is not None:
        cmd.extend(["--config", config_path])
    cmd.extend(["keep-alive", endpoint, session_name])
    # Detach process
    kwargs = {}
    if sys.platform != "win32":
        kwargs["start_new_session"] = True
    else:
        # https://stackoverflow.com/questions/1356540/how-can-i-make-a-python-script-run-in-the-background-as-a-service-on-windows
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        DETACHED_PROCESS = 0x00000008
        kwargs["creationflags"] = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP

    p = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        **kwargs,
    )
    return p.pid


def keep_alive(
    endpoint: Annotated[str, typer.Argument(help="Endpoint ID")],
    session_name: Annotated[str, typer.Argument(help="Session name")],
):
    """Hidden command to run keep-alive loop. Terminate after 24h."""
    from colab_cli.common import state

    state.history.log_event(
        session_name,
        "keep_alive_started",
        {"endpoint": endpoint, "pid": os.getpid()},
    )

    start_time = time.time()
    # 24 hours limit
    max_duration = 24 * 3600
    consecutive_4xx = 0
    iterations = 0
    last_error: Optional[Dict[str, Any]] = None

    reason = "time_limit_reached"
    extra: Dict[str, Any] = {}
    while time.time() - start_time < max_duration:
        iterations += 1
        # Check if session still exists in local state
        s = state.store.get(session_name)
        if not s:
            reason = "session_not_found"
            break
        if s.endpoint != endpoint:
            reason = "endpoint_mismatch"
            extra["expected_endpoint"] = endpoint
            extra["actual_endpoint"] = s.endpoint
            break

        try:
            state.client.keep_alive_assignment(endpoint)
            consecutive_4xx = 0
            last_error = None
        except Exception as e:
            code = get_status_code(e)
            response_body = getattr(e, "response_body", None)
            err_info = {
                "status_code": code,
                "error_type": type(e).__name__,
                "error": str(e)[:500],
                "response_body": (str(response_body)[:1000] if response_body else None),
            }
            last_error = err_info
            state.history.log_event(
                session_name,
                "keep_alive_error",
                {
                    **err_info,
                    "iteration": iterations,
                    "consecutive_4xx": consecutive_4xx
                    + (1 if code is not None and 400 <= code < 500 else 0),
                },
            )
            if code is not None and 400 <= code < 500:
                consecutive_4xx += 1
                if consecutive_4xx >= 2:
                    reason = "consecutive_4xx_errors"
                    break
            else:
                # For other errors (network), we retry and don't count as 4xx
                pass

        time.sleep(60)

    payload: Dict[str, Any] = {
        "reason": reason,
        "iterations": iterations,
        "duration_seconds": round(time.time() - start_time, 2),
    }
    if last_error is not None:
        payload["last_error"] = last_error
    payload.update(extra)
    state.history.log_event(session_name, "keep_alive_stopped", payload)


def register(app: typer.Typer):
    app.command()(new)
    app.command(name="sessions")(sessions_command)
    app.command(name="restart-kernel")(restart_kernel)
    app.command()(status)
    app.command()(stop)
    app.command(hidden=True)(keep_alive)
