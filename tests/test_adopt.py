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

from unittest.mock import MagicMock

import pytest
import typer

from colab_cli.client import Accelerator, AssignmentVariant, ColabRequestError, Shape
from colab_cli.commands.adopt import adopt
from colab_cli.state import SessionState


def _listed_assignment(
    endpoint,
    variant=AssignmentVariant.DEFAULT,
    accelerator=Accelerator.NONE,
    token="tok",
    url="http://runtime",
    machine_shape=Shape.STANDARD,
):
    return MagicMock(
        endpoint=endpoint,
        variant=variant,
        accelerator=accelerator,
        machine_shape=machine_shape,
        runtime_proxy_info=MagicMock(token=token, url=url),
    )


def _scope_error():
    mock_response = MagicMock()
    mock_response.status_code = 403
    return ColabRequestError(
        "Forbidden",
        MagicMock(),
        mock_response,
        response_body=(
            '[7,"Request had insufficient authentication scopes.",[["type.'
            'googleapis.com/google.rpc.DebugInfo",[null,"Authentication error: '
            "2; Error Details: {AuthType:7,ErrorCode:2,DebugInfo:gaia_mint_"
            'exchange::SCOPE_NOT_PERMITTED}"]]]]'
        ),
    )


# --- single-endpoint adoption: `adopt ENDPOINT` -----------------------------


def test_adopt_persists_matching_assignment(mock_common_state):
    mock_common_state.store.list.return_value = {}
    mock_common_state.client.list_assignments.return_value = [
        _listed_assignment("other", variant=AssignmentVariant.DEFAULT),
        _listed_assignment(
            "e1",
            variant=AssignmentVariant.GPU,
            accelerator=Accelerator.T4,
            token="tok1",
            url="http://u1",
            machine_shape=Shape.HIGH_RAM,
        ),
    ]

    adopt(endpoint="e1", name="my-session")

    mock_common_state.store.add.assert_called_once()
    saved = mock_common_state.store.add.call_args.args[0]
    assert isinstance(saved, SessionState)
    assert saved.name == "my-session"
    assert saved.endpoint == "e1"
    assert saved.token == "tok1"
    assert saved.url == "http://u1"
    # AssignmentVariant.GPU.name -> "GPU", matching the Variant string enum.
    assert saved.variant == "GPU"
    assert saved.accelerator == "T4"
    # From the listed assignment's own machineShape -- an adopted high-RAM
    # session must not silently display "Standard".
    assert saved.machine_shape == "HIGH_RAM"
    # No --keep-alive requested: never touch the keep-alive RPC or daemon.
    mock_common_state.client.keep_alive_assignment.assert_not_called()
    assert saved.keep_alive_pid is None


def test_adopt_defaults_name_to_endpoint_when_omitted(mock_common_state):
    mock_common_state.store.list.return_value = {}
    mock_common_state.client.list_assignments.return_value = [
        _listed_assignment("e2"),
    ]

    adopt(endpoint="e2", name=None)

    saved = mock_common_state.store.add.call_args.args[0]
    assert saved.name == "e2"


def test_adopt_logs_history_event(mock_common_state):
    mock_common_state.store.list.return_value = {}
    mock_common_state.client.list_assignments.return_value = [
        _listed_assignment("e3"),
    ]

    adopt(endpoint="e3", name="sess3")

    mock_common_state.history.log_event.assert_called_once_with(
        "sess3", "session_adopted", {"endpoint": "e3", "keep_alive": False}
    )


def test_adopt_errors_when_endpoint_not_found(mock_common_state):
    mock_common_state.store.list.return_value = {}
    mock_common_state.client.list_assignments.return_value = [
        _listed_assignment("some-other-endpoint"),
    ]

    with pytest.raises(typer.Exit) as excinfo:
        adopt(endpoint="missing-endpoint", name=None)

    assert excinfo.value.exit_code == 1
    mock_common_state.store.add.assert_not_called()
    mock_common_state.history.log_event.assert_not_called()


def test_adopt_defaults_variant_and_accelerator(mock_common_state):
    """A plain CPU assignment must round-trip as DEFAULT/NONE, not be dropped."""
    mock_common_state.store.list.return_value = {}
    mock_common_state.client.list_assignments.return_value = [
        _listed_assignment(
            "e4", variant=AssignmentVariant.DEFAULT, accelerator=Accelerator.NONE
        ),
    ]

    adopt(endpoint="e4", name=None)

    saved = mock_common_state.store.add.call_args.args[0]
    assert saved.variant == "DEFAULT"
    assert saved.accelerator == "NONE"


def test_adopt_is_idempotent_when_tracked_under_a_different_name(mock_common_state):
    """Re-running `adopt ENDPOINT` for an endpoint that's already local under a
    *different* local name (whether adopted earlier or created via `colab new`)
    must be a no-op: no backend call, no re-persist, no history event, no error.
    Renaming a tracked session is out of scope for `adopt`."""
    already_tracked = SessionState(
        name="existing-name", token="t", url="u", endpoint="e1", keep_alive_pid=123
    )
    mock_common_state.store.list.return_value = {"existing-name": already_tracked}

    adopt(endpoint="e1", name="some-other-name")

    mock_common_state.client.list_assignments.assert_not_called()
    mock_common_state.store.add.assert_not_called()
    mock_common_state.history.log_event.assert_not_called()


def test_adopt_does_not_clobber_cli_created_session(mock_common_state):
    """A session created via `colab new` (with kernel_id/keep_alive_pid already
    set) must not be overwritten by adopting its endpoint again under a name
    that doesn't match."""
    live_session = SessionState(
        name="live",
        token="t",
        url="u",
        endpoint="e1",
        kernel_id="kid-1",
        session_id="sid-1",
        keep_alive_pid=999,
        running="some_file.py",
    )
    mock_common_state.store.list.return_value = {"live": live_session}

    adopt(endpoint="e1", name=None)

    mock_common_state.store.add.assert_not_called()


def test_adopt_refuses_to_steal_a_name_tracking_a_different_endpoint(
    mock_common_state,
):
    """`-n NAME` must never silently repoint a name that already tracks a
    different, still-local endpoint -- that would orphan the runtime that
    used to own the name (and leak its keep-alive daemon, if any)."""
    other = SessionState(name="taken", token="t", url="u", endpoint="e-old")
    mock_common_state.store.list.return_value = {"taken": other}

    with pytest.raises(typer.Exit) as excinfo:
        adopt(endpoint="e-new", name="taken")

    assert excinfo.value.exit_code == 1
    # Fails fast, before ever hitting the backend.
    mock_common_state.client.list_assignments.assert_not_called()
    mock_common_state.store.add.assert_not_called()


def test_adopt_errors_when_proxy_info_missing(mock_common_state):
    """An assignment with no (or expired) runtime proxy token/url can't be
    adopted -- surface an actionable error instead of an AttributeError."""
    mock_common_state.store.list.return_value = {}
    mock_common_state.client.list_assignments.return_value = [
        _listed_assignment("e5", token=None, url=None),
    ]

    with pytest.raises(typer.Exit) as excinfo:
        adopt(endpoint="e5", name=None)

    assert excinfo.value.exit_code == 1
    mock_common_state.store.add.assert_not_called()
    mock_common_state.history.log_event.assert_not_called()


# --- refreshing an already-adopted session: `adopt ENDPOINT` (same name) ----


def test_adopt_refreshes_matching_session_when_name_matches(mock_common_state):
    """Re-running `adopt ENDPOINT -n SAME-NAME` must refresh the runtime proxy
    token/url (it expires roughly hourly) while preserving kernel/session/
    keep-alive/running state -- unlike a fresh adopt, this is not a no-op."""
    existing = SessionState(
        name="my-session",
        token="stale-tok",
        url="http://stale",
        endpoint="e1",
        variant="DEFAULT",
        accelerator="NONE",
        kernel_id="kid-1",
        session_id="sid-1",
        keep_alive_pid=555,
        running="script.py",
    )
    mock_common_state.store.list.return_value = {"my-session": existing}
    mock_common_state.client.list_assignments.return_value = [
        _listed_assignment(
            "e1",
            variant=AssignmentVariant.GPU,
            accelerator=Accelerator.T4,
            token="fresh-tok",
            url="http://fresh",
        ),
    ]

    adopt(endpoint="e1", name="my-session")

    saved = mock_common_state.store.add.call_args.args[0]
    assert saved.token == "fresh-tok"
    assert saved.url == "http://fresh"
    assert saved.variant == "GPU"
    assert saved.accelerator == "T4"
    # Kernel/session/keep-alive/running state must survive a refresh.
    assert saved.kernel_id == "kid-1"
    assert saved.session_id == "sid-1"
    assert saved.keep_alive_pid == 555
    assert saved.running == "script.py"
    mock_common_state.history.log_event.assert_called_once_with(
        "my-session", "session_refreshed", {"endpoint": "e1"}
    )


def test_adopt_refresh_uses_default_name_when_omitted(mock_common_state):
    """`adopt ENDPOINT` with no `-n` refreshes an existing session tracked
    under a name equal to the endpoint (the default naming from a prior
    adopt), not just when `-n` is spelled out explicitly."""
    existing = SessionState(name="e1", token="stale", url="http://stale", endpoint="e1")
    mock_common_state.store.list.return_value = {"e1": existing}
    mock_common_state.client.list_assignments.return_value = [
        _listed_assignment("e1", token="fresh", url="http://fresh"),
    ]

    adopt(endpoint="e1", name=None)

    saved = mock_common_state.store.add.call_args.args[0]
    assert saved.token == "fresh"
    mock_common_state.history.log_event.assert_called_once_with(
        "e1", "session_refreshed", {"endpoint": "e1"}
    )


def test_adopt_refresh_errors_when_assignment_gone(mock_common_state):
    """If the backend assignment backing an already-tracked session has
    disappeared, refreshing must fail loudly rather than silently keep serving
    a stale token."""
    existing = SessionState(name="my-session", token="t", url="u", endpoint="e1")
    mock_common_state.store.list.return_value = {"my-session": existing}
    mock_common_state.client.list_assignments.return_value = []

    with pytest.raises(typer.Exit) as excinfo:
        adopt(endpoint="e1", name="my-session")

    assert excinfo.value.exit_code == 1
    mock_common_state.store.add.assert_not_called()
    mock_common_state.history.log_event.assert_not_called()


def test_adopt_refresh_errors_when_proxy_info_missing(mock_common_state):
    existing = SessionState(name="my-session", token="t", url="u", endpoint="e1")
    mock_common_state.store.list.return_value = {"my-session": existing}
    mock_common_state.client.list_assignments.return_value = [
        _listed_assignment("e1", token=None, url=None),
    ]

    with pytest.raises(typer.Exit) as excinfo:
        adopt(endpoint="e1", name="my-session")

    assert excinfo.value.exit_code == 1
    mock_common_state.store.add.assert_not_called()


# --- --keep-alive opt-in -----------------------------------------------------


def test_adopt_starts_keep_alive_when_requested(mock_common_state, mocker):
    spawn = mocker.patch(
        "colab_cli.commands.adopt.spawn_keep_alive", return_value=4242
    )
    mock_common_state.store.list.return_value = {}
    mock_common_state.client.list_assignments.return_value = [
        _listed_assignment("e7"),
    ]

    adopt(endpoint="e7", name=None, keep_alive=True)

    mock_common_state.client.keep_alive_assignment.assert_called_once_with("e7")
    spawn.assert_called_once()
    # Persisted twice: once before spawning (so the daemon's own
    # state.store.get(name) check doesn't race the parent), once after to
    # record the PID.
    assert mock_common_state.store.add.call_count == 2
    saved = mock_common_state.store.add.call_args.args[0]
    assert saved.keep_alive_pid == 4242
    mock_common_state.history.log_event.assert_called_once_with(
        "e7", "session_adopted", {"endpoint": "e7", "keep_alive": True}
    )


def test_adopt_keep_alive_scope_error_aborts_without_persisting(
    mock_common_state, mocker
):
    """A missing OAuth scope on the keep-alive pre-flight must abort the whole
    adopt -- the user explicitly asked for keep-alive, so silently adopting
    without it would be a worse outcome than failing loudly."""
    spawn = mocker.patch("colab_cli.commands.adopt.spawn_keep_alive")
    mock_common_state.client.keep_alive_assignment.side_effect = _scope_error()
    mock_common_state.store.list.return_value = {}
    mock_common_state.client.list_assignments.return_value = [
        _listed_assignment("e8"),
    ]

    with pytest.raises(typer.Exit) as excinfo:
        adopt(endpoint="e8", name=None, keep_alive=True)

    assert excinfo.value.exit_code == 1
    spawn.assert_not_called()
    mock_common_state.store.add.assert_not_called()
    mock_common_state.history.log_event.assert_not_called()


def test_adopt_keep_alive_tolerates_non_scope_preflight_error(
    mock_common_state, mocker
):
    """Non-scope pre-flight failures (transient 5xx etc.) must not block
    adoption -- the daemon retries on its own, same as `colab new`."""
    spawn = mocker.patch(
        "colab_cli.commands.adopt.spawn_keep_alive", return_value=9999
    )
    mock_response = MagicMock()
    mock_response.status_code = 503
    mock_common_state.client.keep_alive_assignment.side_effect = ColabRequestError(
        "Service Unavailable", MagicMock(), mock_response, response_body="timeout"
    )
    mock_common_state.store.list.return_value = {}
    mock_common_state.client.list_assignments.return_value = [
        _listed_assignment("e9"),
    ]

    adopt(endpoint="e9", name=None, keep_alive=True)

    spawn.assert_called_once()
    saved = mock_common_state.store.add.call_args.args[0]
    assert saved.keep_alive_pid == 9999


def test_adopt_refresh_can_start_keep_alive_when_requested_and_missing(
    mock_common_state, mocker
):
    """`--keep-alive` on a refresh should start a daemon that wasn't running
    yet, without disturbing an already-running one."""
    spawn = mocker.patch(
        "colab_cli.commands.adopt.spawn_keep_alive", return_value=7777
    )
    existing = SessionState(
        name="my-session", token="t", url="u", endpoint="e1", keep_alive_pid=None
    )
    mock_common_state.store.list.return_value = {"my-session": existing}
    mock_common_state.client.list_assignments.return_value = [
        _listed_assignment("e1", token="fresh", url="http://fresh"),
    ]

    adopt(endpoint="e1", name="my-session", keep_alive=True)

    spawn.assert_called_once()
    # Persisted twice: once before spawning (so the daemon's own
    # state.store.get(name) check doesn't race the parent), once after to
    # record the PID -- matching _adopt_endpoint's established pattern.
    assert mock_common_state.store.add.call_count == 2
    saved = mock_common_state.store.add.call_args.args[0]
    assert saved.keep_alive_pid == 7777


def test_adopt_refresh_does_not_restart_existing_keep_alive(mock_common_state, mocker):
    spawn = mocker.patch("colab_cli.commands.adopt.spawn_keep_alive")
    existing = SessionState(
        name="my-session", token="t", url="u", endpoint="e1", keep_alive_pid=111
    )
    mock_common_state.store.list.return_value = {"my-session": existing}
    mock_common_state.client.list_assignments.return_value = [
        _listed_assignment("e1", token="fresh", url="http://fresh"),
    ]

    adopt(endpoint="e1", name="my-session", keep_alive=True)

    spawn.assert_not_called()
    saved = mock_common_state.store.add.call_args.args[0]
    assert saved.keep_alive_pid == 111


# --- bulk adoption: `adopt --orphanage` -------------------------------------


def test_adopt_orphanage_persists_all_orphaned_assignments(mock_common_state):
    mock_common_state.store.list.return_value = {}
    mock_common_state.client.list_assignments.return_value = [
        _listed_assignment("other", variant=AssignmentVariant.DEFAULT),
        _listed_assignment(
            "e1",
            variant=AssignmentVariant.GPU,
            accelerator=Accelerator.T4,
            token="tok1",
            url="http://u1",
        ),
        _listed_assignment(
            "e2",
            variant=AssignmentVariant.GPU,
            accelerator=Accelerator.A100,
            token="tok2",
            url="http://u2",
        ),
    ]

    adopt(endpoint=None, orphanage=True)

    # Only one round-trip to list assignments, reused for all adoptions
    # (previously this was called once per assignment -- an N+1 bug).
    mock_common_state.client.list_assignments.assert_called_once()

    assert mock_common_state.store.add.call_count == 3
    saved = mock_common_state.store.add.call_args_list[1].args[0]
    assert isinstance(saved, SessionState)
    assert saved.name == saved.endpoint
    assert saved.endpoint == "e1"
    assert saved.token == "tok1"
    assert saved.url == "http://u1"
    assert saved.variant == "GPU"
    assert saved.accelerator == "T4"

    saved2 = mock_common_state.store.add.call_args_list[2].args[0]
    assert isinstance(saved2, SessionState)
    assert saved2.name == saved2.endpoint
    assert saved2.endpoint == "e2"
    assert saved2.token == "tok2"
    assert saved2.url == "http://u2"
    assert saved2.variant == "GPU"
    assert saved2.accelerator == "A100"


def test_adopt_orphanage_skips_endpoints_already_tracked_locally(mock_common_state):
    """Sessions already known locally (adopted before, or created via
    `colab new`) must not be re-adopted or touched by a bulk orphanage run."""
    already_tracked = SessionState(
        name="already-tracked", token="t", url="u", endpoint="e1", keep_alive_pid=1
    )
    mock_common_state.store.list.return_value = {"already-tracked": already_tracked}
    mock_common_state.client.list_assignments.return_value = [
        _listed_assignment("e1", token="tok1", url="http://u1"),  # already tracked
        _listed_assignment("e2", token="tok2", url="http://u2"),  # true orphan
    ]

    adopt(endpoint=None, orphanage=True)

    mock_common_state.store.add.assert_called_once()
    saved = mock_common_state.store.add.call_args.args[0]
    assert saved.endpoint == "e2"


def test_adopt_orphanage_reports_when_nothing_to_adopt(mock_common_state):
    already_tracked = SessionState(
        name="already-tracked", token="t", url="u", endpoint="e1"
    )
    mock_common_state.store.list.return_value = {"already-tracked": already_tracked}
    mock_common_state.client.list_assignments.return_value = [
        _listed_assignment("e1"),
    ]

    adopt(endpoint=None, orphanage=True)

    mock_common_state.store.add.assert_not_called()
    mock_common_state.history.log_event.assert_not_called()


def test_adopt_orphanage_skips_name_collision_but_adopts_the_rest(mock_common_state):
    """A degenerate case where an orphan's endpoint-as-name collides with a
    tracked session for a *different* endpoint must be skipped, not silently
    clobbered -- and must not abort the rest of the batch."""
    other = SessionState(name="e1", token="t", url="u", endpoint="not-e1")
    mock_common_state.store.list.return_value = {"e1": other}
    mock_common_state.client.list_assignments.return_value = [
        _listed_assignment("e1", token="tok1", url="http://u1"),
        _listed_assignment("e2", token="tok2", url="http://u2"),
    ]

    adopt(endpoint=None, orphanage=True)

    mock_common_state.store.add.assert_called_once()
    saved = mock_common_state.store.add.call_args.args[0]
    assert saved.endpoint == "e2"


def test_adopt_orphanage_skips_missing_proxy_info_but_adopts_the_rest(
    mock_common_state,
):
    mock_common_state.store.list.return_value = {}
    mock_common_state.client.list_assignments.return_value = [
        _listed_assignment("e1", token=None, url=None),
        _listed_assignment("e2", token="tok2", url="http://u2"),
    ]

    adopt(endpoint=None, orphanage=True)

    mock_common_state.store.add.assert_called_once()
    saved = mock_common_state.store.add.call_args.args[0]
    assert saved.endpoint == "e2"


def test_adopt_orphanage_starts_keep_alive_when_requested(mock_common_state, mocker):
    spawn = mocker.patch(
        "colab_cli.commands.adopt.spawn_keep_alive", return_value=333
    )
    mock_common_state.store.list.return_value = {}
    mock_common_state.client.list_assignments.return_value = [
        _listed_assignment("e1", token="tok1", url="http://u1"),
    ]

    adopt(endpoint=None, orphanage=True, keep_alive=True)

    mock_common_state.client.keep_alive_assignment.assert_called_once_with("e1")
    spawn.assert_called_once()
    saved = mock_common_state.store.add.call_args_list[-1].args[0]
    assert saved.keep_alive_pid == 333
    mock_common_state.history.log_event.assert_called_once_with(
        "e1", "session_adopted", {"endpoint": "e1", "keep_alive": True}
    )


# --- argument validation -----------------------------------------------------


def test_adopt_rejects_endpoint_and_orphanage_together(mock_common_state):
    with pytest.raises(typer.Exit) as excinfo:
        adopt(endpoint="e1", orphanage=True)

    assert excinfo.value.exit_code == 2
    mock_common_state.client.list_assignments.assert_not_called()
    mock_common_state.store.add.assert_not_called()


def test_adopt_rejects_neither_endpoint_nor_orphanage(mock_common_state):
    with pytest.raises(typer.Exit) as excinfo:
        adopt(endpoint=None, orphanage=False)

    assert excinfo.value.exit_code == 2
    mock_common_state.client.list_assignments.assert_not_called()
    mock_common_state.store.add.assert_not_called()
