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

from typing import Optional

import typer
from typing_extensions import Annotated


def adopt_session(
    endpoint: Annotated[
        str,
        typer.Option(
            "-e",
            "--endpoint",
            help="The endpoint string shown by 'colab sessions' (e.g., m-s-kkb-...)",
        ),
    ],
    name: Annotated[
        Optional[str],
        typer.Option("-n", "--name", help="Optional friendly name override for this session"),
    ] = None,
):
    """Adopt an orphaned server-side assignment"""
    from colab_cli.common import state
    from colab_cli.state import SessionState

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
    s = SessionState(
        name=session_name,
        token=match.runtime_proxy_info.token,
        url=match.runtime_proxy_info.url,
        endpoint=endpoint,
        variant=match.variant.name,
        accelerator=match.accelerator.value,
    )
    state.store.add(s)
    state.history.log_event(session_name, "session_adopted", {"endpoint": endpoint})

    typer.echo(f"[colab] Successfully adopted session as '{session_name}'.")


def register(app: typer.Typer):
    app.command()(adopt_session)
