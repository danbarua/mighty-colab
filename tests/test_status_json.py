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

"""`status --json`: single-session shape, list-shape (no -s), and the
session_not_found divergence from today's exit-0 prose behavior (query
commands should error on "not found" -- opt-in under --json only)."""

import json

from unittest.mock import MagicMock

from typer.testing import CliRunner

from colab_cli.cli import app

runner = CliRunner()


def test_status_json_single_session_found(mock_common_state, mocker):
    mocker.patch("colab_cli.auto_update.get_app_version", return_value="9.9.9")
    mock_common_state.json_output = True

    mock_session_state = MagicMock()
    mock_session_state.name = "s1"
    mock_session_state.endpoint = "e1"
    mock_session_state.accelerator = "T4"
    mock_session_state.variant = "GPU"
    mock_session_state.running = "exec.py"
    mock_session_state.last_execution = ("script.py", None, "2026-08-12 01:00:00")
    mock_session_state.exec_log_path = "/tmp/s1.exec.log"
    mock_session_state.keep_alive_pid = 4242
    mock_session_state.last_keep_alive_ping = "2026-08-12T01:00:00+00:00"
    mock_common_state.store.get.return_value = mock_session_state
    mock_common_state.sync_sessions.return_value = ({"s1": mock_session_state}, [])
    mocker.patch("colab_cli.common.pid_alive", return_value=True)

    result = runner.invoke(app, ["status", "-s", "s1"])
    assert result.exit_code == 0, result.output

    envelope = json.loads(result.stdout)
    assert envelope["schema_version"] == "1"
    assert envelope["cli_version"] == "9.9.9"
    assert envelope["command"] == "status"
    assert envelope["status"] == "ok"
    assert envelope["session"] == {
        "name": "s1",
        "endpoint": "e1",
        "accelerator": "T4",
        "variant": "GPU",
        "status": "BUSY (exec.py)",
        "last_execution_file": "script.py",
        "last_execution_cell": None,
        "last_execution_time": "2026-08-12 01:00:00",
        "exec_log_path": "/tmp/s1.exec.log",
        "keep_alive_pid": 4242,
        "last_keep_alive_ping": "2026-08-12T01:00:00+00:00",
    }


def test_status_json_keep_alive_pid_hidden_when_daemon_confirmed_dead(
    mock_common_state, mocker
):
    """A stored `keep_alive_pid` for a daemon that's actually dead must not
    be reported as though it were still running -- only what `pid_alive()`
    can currently confirm is surfaced."""
    mock_session_state = MagicMock()
    mock_session_state.name = "s1"
    mock_session_state.endpoint = "e1"
    mock_session_state.accelerator = "NONE"
    mock_session_state.variant = "DEFAULT"
    mock_session_state.running = None
    mock_session_state.last_execution = None
    mock_session_state.exec_log_path = None
    mock_session_state.keep_alive_pid = 4242
    mock_session_state.last_keep_alive_ping = "2026-08-12T01:00:00+00:00"
    mock_common_state.store.get.return_value = mock_session_state
    mock_common_state.sync_sessions.return_value = ({"s1": mock_session_state}, [])
    mock_common_state.json_output = True
    mocker.patch("colab_cli.common.pid_alive", return_value=False)

    result = runner.invoke(app, ["status", "-s", "s1"])
    assert result.exit_code == 0, result.output

    envelope = json.loads(result.stdout)
    assert "keep_alive_pid" not in envelope["session"]
    # The last-ping record itself is still an observed fact, independent
    # of whether the daemon happens to be alive right now.
    assert envelope["session"]["last_keep_alive_ping"] == "2026-08-12T01:00:00+00:00"


def test_status_json_single_session_not_found_is_error_and_exits_nonzero(
    mock_common_state,
):
    """Diverges from today's plain-text status (exit 0, prose message) --
    status is a query command, so --json opts into the correct 'not found
    is an error' behavior per the design principle already recorded in
    docs/AGENT_USABILITY_LEARNINGS.md."""
    mock_common_state.json_output = True
    mock_common_state.store.get.return_value = None
    mock_common_state.sync_sessions.return_value = ({}, [])

    result = runner.invoke(app, ["status", "-s", "missing"])
    assert result.exit_code == 1

    envelope = json.loads(result.stdout)
    assert envelope["status"] == "error"
    assert envelope["reason"] == "session_not_found"
    assert "not found" in result.stderr


def test_status_json_no_session_flag_lists_all(mock_common_state):
    mock_common_state.json_output = True

    mock_session_state = MagicMock()
    mock_session_state.name = "s1"
    mock_session_state.endpoint = "e1"
    mock_session_state.accelerator = "NONE"
    mock_session_state.variant = "DEFAULT"
    mock_session_state.running = None
    mock_session_state.last_execution = None
    mock_session_state.exec_log_path = None
    mock_session_state.keep_alive_pid = None
    mock_session_state.last_keep_alive_ping = None
    mock_common_state.sync_sessions.return_value = ({"s1": mock_session_state}, [])

    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output

    envelope = json.loads(result.stdout)
    assert envelope["status"] == "ok"
    assert envelope["sessions"] == [
        {
            "name": "s1",
            "endpoint": "e1",
            "accelerator": "NONE",
            "variant": "DEFAULT",
            "status": "IDLE",
        }
    ]


def test_status_json_no_sessions_is_ok_with_empty_list(mock_common_state):
    mock_common_state.json_output = True
    mock_common_state.sync_sessions.return_value = ({}, [])

    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output

    envelope = json.loads(result.stdout)
    assert envelope["status"] == "ok"
    assert envelope["sessions"] == []


def test_status_without_json_not_found_stays_exit_zero(mock_common_state):
    """Regression guard: the plain-text path must be byte-for-byte
    unchanged -- not found is still an idempotent-looking no-op there."""
    mock_common_state.json_output = False
    mock_common_state.store.get.return_value = None
    mock_common_state.sync_sessions.return_value = ({}, [])

    result = runner.invoke(app, ["status", "-s", "missing"])
    assert result.exit_code == 0
    assert "not found" in result.output
    assert "{" not in result.stdout
