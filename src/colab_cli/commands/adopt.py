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

from typing import Dict, Optional

import typer
from typing_extensions import Annotated

from colab_cli.client import ColabRequestError
from colab_cli.commands.session import (
    _is_scope_error,
    _scope_remediation_message,
    spawn_keep_alive,
)
from colab_cli.state import SessionState
from colab_cli.utils import get_status_code


def _find_by_endpoint(
    sessions: Dict[str, SessionState], endpoint: str
) -> Optional[SessionState]:
    return next((s for s in sessions.values() if s.endpoint == endpoint), None)


def _has_proxy_info(assignment) -> bool:
    proxy = getattr(assignment, "runtime_proxy_info", None)
    return bool(getattr(proxy, "token", None)) and bool(getattr(proxy, "url", None))


def _session_state_from_assignment(assignment, name: str) -> SessionState:
    return SessionState(
        name=name,
        token=assignment.runtime_proxy_info.token,
        url=assignment.runtime_proxy_info.url,
        endpoint=assignment.endpoint,
        variant=assignment.variant.name,
        accelerator=assignment.accelerator.value,
    )


def _preflight_keep_alive(endpoint: str, session_name: str):
    """Verify the keep-alive RPC succeeds before persisting anything, so a
    missing OAuth scope surfaces immediately instead of a session that's
    silently missing a working keep-alive daemon.

    Unlike `colab new`, adopt never unassigns on failure -- it didn't create
    the assignment, so it doesn't own the decision to tear it down.
    """
    from colab_cli.common import state

    try:
        state.client.keep_alive_assignment(endpoint)
    except ColabRequestError as e:
        if get_status_code(e) == 403 and _is_scope_error(e):
            typer.echo(
                f"[colab] Keep-alive pre-flight failed for '{session_name}': "
                "your credentials are missing an OAuth scope required by "
                "Colab.\n",
                err=True,
            )
            typer.echo(_scope_remediation_message(state.auth_provider), err=True)
            raise typer.Exit(code=1)
        # Other failures: don't block adoption -- the daemon retries on its own.


def _start_keep_alive(endpoint: str, session_name: str) -> int:
    from colab_cli.common import state

    return spawn_keep_alive(
        endpoint,
        session_name,
        auth_provider=state.auth_provider,
        config_path=state.config_path,
    )


def _refresh_endpoint(existing: SessionState, keep_alive: bool):
    """Re-fetch backend details for an endpoint that's already tracked
    locally under the requested name.

    The runtime proxy token expires roughly hourly, so a plain re-adopt would
    otherwise leave `exec`/`status`/etc. silently using a dead token forever.
    kernel_id/session_id/keep_alive_pid/running all carry over untouched.
    """
    from colab_cli.common import state

    assignments = state.client.list_assignments()
    match = next((a for a in assignments if a.endpoint == existing.endpoint), None)
    if match is None:
        typer.echo(
            f"[colab] Backend assignment for '{existing.endpoint}' is no "
            f"longer active. Run 'colab stop -s {existing.name}' to clean up "
            "the stale local session.",
            err=True,
        )
        raise typer.Exit(code=1)

    if not _has_proxy_info(match):
        typer.echo(
            f"[colab] Assignment '{existing.endpoint}' has missing or "
            "expired runtime proxy information and cannot be refreshed.",
            err=True,
        )
        raise typer.Exit(code=1)

    existing.token = match.runtime_proxy_info.token
    existing.url = match.runtime_proxy_info.url
    existing.variant = match.variant.name
    existing.accelerator = match.accelerator.value

    if keep_alive and not existing.keep_alive_pid:
        _preflight_keep_alive(existing.endpoint, existing.name)
        existing.keep_alive_pid = _start_keep_alive(existing.endpoint, existing.name)

    state.store.add(existing)
    state.history.log_event(
        existing.name, "session_refreshed", {"endpoint": existing.endpoint}
    )
    typer.echo(f"[colab] Refreshed session '{existing.name}'.")


def _adopt_endpoint(endpoint: str, name: Optional[str], keep_alive: bool):
    from colab_cli.common import state

    sessions = state.store.list()
    existing = _find_by_endpoint(sessions, endpoint)
    session_name = name or endpoint

    if existing is not None and existing.name == session_name:
        _refresh_endpoint(existing, keep_alive)
        return

    if existing is not None:
        # Tracked locally under a different name already -- idempotent, and
        # never clobbers a session created via `colab new` either. Renaming
        # a tracked session is out of scope for `adopt`.
        typer.echo(
            f"[colab] Endpoint '{endpoint}' is already tracked locally as "
            f"'{existing.name}' -- nothing to do."
        )
        return

    # Refuse to steal a name that already tracks a *different* endpoint --
    # `store.add` overwrites unconditionally, which would silently orphan
    # whatever session (and keep-alive daemon) currently owns that name.
    clash = sessions.get(session_name)
    if clash is not None:
        typer.echo(
            f"[colab] Session name '{session_name}' already tracks endpoint "
            f"'{clash.endpoint}'. Choose a different name, or run "
            f"'colab stop -s {session_name}' first.",
            err=True,
        )
        raise typer.Exit(code=1)

    typer.echo(f"[colab] Querying backend details for endpoint: {endpoint}...")

    assignments = state.client.list_assignments()
    match = next((a for a in assignments if a.endpoint == endpoint), None)
    if match is None:
        typer.echo(
            f"[colab] Error: No active assignment found for endpoint '{endpoint}'.",
            err=True,
        )
        raise typer.Exit(code=1)

    if not _has_proxy_info(match):
        typer.echo(
            f"[colab] Assignment '{endpoint}' has missing or expired runtime "
            "proxy information and cannot be adopted.",
            err=True,
        )
        raise typer.Exit(code=1)

    s = _session_state_from_assignment(match, session_name)
    if keep_alive:
        # Pre-flight before persisting: a missing scope should abort the
        # whole adopt, not leave a session quietly missing its daemon.
        _preflight_keep_alive(endpoint, session_name)
        # Persist BEFORE spawning so the daemon's own state.store.get(name)
        # check doesn't race the parent.
        state.store.add(s)
        s.keep_alive_pid = _start_keep_alive(endpoint, session_name)

    state.store.add(s)
    state.history.log_event(
        session_name,
        "session_adopted",
        {"endpoint": endpoint, "keep_alive": keep_alive},
    )

    typer.echo(f"[colab] Successfully adopted session as '{session_name}'.")


def _adopt_all_orphans(keep_alive: bool):
    from colab_cli.common import state

    local_sessions = state.store.list()
    assignments = state.client.list_assignments()
    orphans = [
        a for a in assignments if _find_by_endpoint(local_sessions, a.endpoint) is None
    ]

    if not orphans:
        typer.echo("[colab] No orphaned sessions found.")
        return

    typer.echo(f"[colab] Adopting {len(orphans)} orphaned session(s)...")
    adopted = 0
    for assignment in orphans:
        session_name = assignment.endpoint

        clash = local_sessions.get(session_name)
        if clash is not None:
            typer.echo(
                f"[colab] Skipping '{assignment.endpoint}': name "
                f"'{session_name}' already tracks a different endpoint "
                f"('{clash.endpoint}').",
                err=True,
            )
            continue

        if not _has_proxy_info(assignment):
            typer.echo(
                f"[colab] Skipping '{assignment.endpoint}': missing or "
                "expired runtime proxy information.",
                err=True,
            )
            continue

        s = _session_state_from_assignment(assignment, session_name)
        if keep_alive:
            _preflight_keep_alive(assignment.endpoint, session_name)
            state.store.add(s)
            s.keep_alive_pid = _start_keep_alive(assignment.endpoint, session_name)

        state.store.add(s)
        state.history.log_event(
            session_name,
            "session_adopted",
            {"endpoint": assignment.endpoint, "keep_alive": keep_alive},
        )
        typer.echo(f"[colab] Adopted '{session_name}'.")
        adopted += 1

    if adopted == 0:
        typer.echo("[colab] No orphaned sessions were adopted.")


def adopt(
    endpoint: Annotated[
        Optional[str],
        typer.Argument(
            help="The endpoint string shown by 'colab sessions' (e.g., m-s-kkb-...). "
            "Omit when using --orphanage."
        ),
    ] = None,
    orphanage: Annotated[
        bool,
        typer.Option(
            "--orphanage",
            help="Adopt every orphaned server-side assignment instead of a single ENDPOINT.",
        ),
    ] = False,
    name: Annotated[
        Optional[str],
        typer.Option(
            "-n", "--name", help="Optional friendly name override (ignored with --orphanage)"
        ),
    ] = None,
    keep_alive: Annotated[
        bool,
        typer.Option(
            "--keep-alive",
            help="Start a local keep-alive daemon for the adopted session(s). "
            "Off by default -- whoever created the runtime (e.g. a Colab "
            "browser tab) is assumed to already be keeping it alive.",
        ),
    ] = False,
):
    """Adopt an orphaned server-side assignment"""
    if orphanage and endpoint:
        typer.echo(
            "[colab] Error: Provide either ENDPOINT or --orphanage, not both.", err=True
        )
        raise typer.Exit(code=2)

    if orphanage:
        _adopt_all_orphans(keep_alive)
        return

    if not endpoint:
        typer.echo(
            "[colab] Error: Provide an ENDPOINT to adopt, or use --orphanage to adopt all.",
            err=True,
        )
        raise typer.Exit(code=2)

    _adopt_endpoint(endpoint, name, keep_alive)


def register(app: typer.Typer):
    app.command()(adopt)
