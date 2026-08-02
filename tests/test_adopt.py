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

from colab_cli.client import Accelerator, AssignmentVariant
from colab_cli.commands.adopt import adopt
from colab_cli.state import SessionState


def _listed_assignment(
    endpoint,
    variant=AssignmentVariant.DEFAULT,
    accelerator=Accelerator.NONE,
    token="tok",
    url="http://runtime",
):
    return MagicMock(
        endpoint=endpoint,
        variant=variant,
        accelerator=accelerator,
        runtime_proxy_info=MagicMock(token=token, url=url),
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
        "sess3", "session_adopted", {"endpoint": "e3"}
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


def test_adopt_is_idempotent_when_already_tracked(mock_common_state):
    """Re-running `adopt ENDPOINT` for an endpoint that's already local (whether
    adopted earlier or created via `colab new`) must be a no-op: no backend
    call, no re-persist, no history event, no error."""
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
    set) must not be overwritten by adopting its endpoint again."""
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
