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

"""`sessions --json`: list-shaped envelope, empty list is ok not an error,
orphaned server-side assignments marked "?" (matching the prose output)."""

import json

from unittest.mock import MagicMock

from typer.testing import CliRunner

from colab_cli.cli import app

runner = CliRunner()


def test_sessions_json_list_shape(mock_common_state, mocker):
    mocker.patch("colab_cli.auto_update.get_app_version", return_value="9.9.9")
    mock_common_state.json_output = True

    mock_assignment = MagicMock()
    mock_assignment.endpoint = "e1"
    mock_assignment.variant.name = "GPU"
    mock_assignment.accelerator.value = "T4"
    mock_assignment.machine_shape.name = "HIGH_RAM"

    mock_session_state = MagicMock()
    mock_session_state.name = "s1"
    mock_session_state.endpoint = "e1"
    mock_session_state.keep_alive_pid = 4242
    mock_session_state.last_keep_alive_ping = "2026-08-12T01:00:00+00:00"

    mock_common_state.sync_sessions.return_value = (
        {"s1": mock_session_state},
        [mock_assignment],
    )
    mocker.patch("colab_cli.common.pid_alive", return_value=True)

    result = runner.invoke(app, ["sessions"])
    assert result.exit_code == 0, result.output

    envelope = json.loads(result.stdout)
    assert envelope["schema_version"] == "1"
    assert envelope["cli_version"] == "9.9.9"
    assert envelope["command"] == "sessions"
    assert envelope["status"] == "ok"
    # The optional SessionInfo fields (status/last_execution_*/exec_log_path)
    # are validated as present-with-default-None by the model, but the raw
    # dict actually emitted only carries what sessions_command() put there
    # -- matching this codebase's existing convention (e.g. "reason" is
    # likewise omitted, not null, when not set).
    assert envelope["sessions"] == [
        {
            "name": "s1",
            "endpoint": "e1",
            "accelerator": "T4",
            "variant": "GPU",
            # Server-side truth (the listed assignment's machineShape), not
            # local state -- matching the plain-text Shape: column.
            "machine_shape": "HIGH_RAM",
            "keep_alive_pid": 4242,
            "last_keep_alive_ping": "2026-08-12T01:00:00+00:00",
        }
    ]


def test_sessions_json_keep_alive_none_for_orphaned_assignment(mock_common_state):
    """An assignment with no matching local `SessionState` (adopted, or
    created by a different machine/process) has genuinely unknown
    keep-alive status -- must not be conflated with "no keep-alive"."""
    mock_common_state.json_output = True

    mock_assignment = MagicMock()
    mock_assignment.endpoint = "orphan-ep"
    mock_assignment.variant.name = "DEFAULT"
    mock_assignment.accelerator.value = "NONE"
    mock_assignment.machine_shape.name = "STANDARD"

    mock_common_state.sync_sessions.return_value = ({}, [mock_assignment])

    result = runner.invoke(app, ["sessions"])
    assert result.exit_code == 0, result.output

    envelope = json.loads(result.stdout)
    assert "keep_alive_pid" not in envelope["sessions"][0]
    assert "last_keep_alive_ping" not in envelope["sessions"][0]


def test_sessions_json_orphaned_assignment_marked(mock_common_state):
    mock_common_state.json_output = True

    mock_assignment = MagicMock()
    mock_assignment.endpoint = "orphan-ep"
    mock_assignment.variant.name = "DEFAULT"
    mock_assignment.accelerator.value = "NONE"
    mock_assignment.machine_shape.name = "STANDARD"

    mock_common_state.sync_sessions.return_value = ({}, [mock_assignment])

    result = runner.invoke(app, ["sessions"])
    assert result.exit_code == 0, result.output

    envelope = json.loads(result.stdout)
    assert envelope["sessions"][0]["name"] == "?"


def test_sessions_json_no_assignments_is_ok_with_empty_list(mock_common_state):
    mock_common_state.json_output = True
    mock_common_state.sync_sessions.return_value = ({}, [])

    result = runner.invoke(app, ["sessions"])
    assert result.exit_code == 0, result.output

    envelope = json.loads(result.stdout)
    assert envelope["status"] == "ok"
    assert envelope["sessions"] == []


def test_sessions_without_json_unaffected(mock_common_state):
    mock_common_state.json_output = False
    mock_common_state.sync_sessions.return_value = ({}, [])

    result = runner.invoke(app, ["sessions"])
    assert result.exit_code == 0
    assert "{" not in result.stdout
    assert "No active sessions found on server." in result.output
