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

from colab_cli.state import SessionState


def _find_by_endpoint(
    sessions: Dict[str, SessionState], endpoint: str
) -> Optional[SessionState]:
    return next((s for s in sessions.values() if s.endpoint == endpoint), None)


def _session_state_from_assignment(assignment, name: str) -> SessionState:
    return SessionState(
        name=name,
        token=assignment.runtime_proxy_info.token,
        url=assignment.runtime_proxy_info.url,
        endpoint=assignment.endpoint,
        variant=assignment.variant.name,
        accelerator=assignment.accelerator.value,
    )


def _adopt_endpoint(endpoint: str, name: Optional[str]):
    from colab_cli.common import state

    # Idempotent, and does not clobber a session already tracked locally
    # (whether previously adopted or created via `colab new`) -- re-adopting
    # would reset its kernel_id/session_id/keep_alive_pid/running state.
    existing = _find_by_endpoint(state.store.list(), endpoint)
    if existing is not None:
        typer.echo(
            f"[colab] Endpoint '{endpoint}' is already tracked locally as "
            f"'{existing.name}' -- nothing to do."
        )
        return

    typer.echo(f"[colab] Querying backend details for endpoint: {endpoint}...")

    assignments = state.client.list_assignments()
    match = next((a for a in assignments if a.endpoint == endpoint), None)
    if match is None:
        typer.echo(
            f"[colab] Error: No active assignment found for endpoint '{endpoint}'.",
            err=True,
        )
        raise typer.Exit(code=1)

    session_name = name or endpoint
    s = _session_state_from_assignment(match, session_name)
    state.store.add(s)
    state.history.log_event(session_name, "session_adopted", {"endpoint": endpoint})

    typer.echo(f"[colab] Successfully adopted session as '{session_name}'.")


def _adopt_all_orphans():
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
    for assignment in orphans:
        session_name = assignment.endpoint
        s = _session_state_from_assignment(assignment, session_name)
        state.store.add(s)
        state.history.log_event(
            session_name, "session_adopted", {"endpoint": assignment.endpoint}
        )
        typer.echo(f"[colab] Adopted '{session_name}'.")


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
):
    """Adopt an orphaned server-side assignment"""
    if orphanage and endpoint:
        typer.echo(
            "[colab] Error: Provide either ENDPOINT or --orphanage, not both.", err=True
        )
        raise typer.Exit(code=2)

    if orphanage:
        _adopt_all_orphans()
        return

    if not endpoint:
        typer.echo(
            "[colab] Error: Provide an ENDPOINT to adopt, or use --orphanage to adopt all.",
            err=True,
        )
        raise typer.Exit(code=2)

    _adopt_endpoint(endpoint, name)


def register(app: typer.Typer):
    app.command()(adopt)
