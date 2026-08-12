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

"""`stop --json`: idempotent not-found stays status="ok", success, and the
genuine unassign-failure error path."""

import json

from unittest.mock import MagicMock

import pytest
import typer

# Importing colab_cli.cli installs the `--json`-aware `typer.echo` wrapper.
import colab_cli.cli  # noqa: F401
from colab_cli.commands.session import stop


def test_stop_json_idempotent_not_found_is_ok(mock_common_state, capsys, mocker):
    mocker.patch("colab_cli.auto_update.get_app_version", return_value="9.9.9")
    mock_common_state.json_output = True
    mock_common_state.store.get.return_value = None
    mock_common_state.resolve_session.return_value = "missing"

    stop(session="missing")

    envelope = json.loads(capsys.readouterr().out)
    assert envelope["schema_version"] == "1"
    assert envelope["cli_version"] == "9.9.9"
    assert envelope["command"] == "stop"
    assert envelope["status"] == "ok"
    assert envelope["reason"] == "already_stopped"
    assert envelope["session"] == "missing"


def test_stop_json_success(mock_common_state, capsys):
    mock_common_state.json_output = True
    mock_session = MagicMock()
    mock_session.name = "s1"
    mock_session.endpoint = "ep-1"
    mock_session.keep_alive_pid = None
    mock_session.exec_pid = None
    mock_common_state.store.get.return_value = mock_session
    mock_common_state.resolve_session.return_value = "s1"

    with pytest.MonkeyPatch().context() as m:
        m.setattr("colab_cli.commands.session.ColabRuntime", MagicMock())
        stop(session="s1")

    envelope = json.loads(capsys.readouterr().out)
    assert envelope["status"] == "ok"
    assert envelope["session"] == "s1"
    assert "reason" not in envelope
    mock_common_state.store.remove.assert_called_once_with("s1")


def test_stop_json_unassign_failure_emits_error_envelope(mock_common_state, capsys):
    mock_common_state.json_output = True
    mock_session = MagicMock()
    mock_session.name = "s1"
    mock_session.endpoint = "ep-1"
    mock_session.keep_alive_pid = None
    mock_session.exec_pid = None
    mock_common_state.store.get.return_value = mock_session
    mock_common_state.resolve_session.return_value = "s1"
    mock_common_state.client.unassign.side_effect = RuntimeError("boom")

    with pytest.MonkeyPatch().context() as m:
        m.setattr("colab_cli.commands.session.ColabRuntime", MagicMock())
        with pytest.raises(typer.Exit) as excinfo:
            stop(session="s1")
        assert excinfo.value.exit_code == 1

    envelope = json.loads(capsys.readouterr().out)
    assert envelope["status"] == "error"
    assert envelope["reason"] == "unassign_failed"
    assert envelope["session"] == "s1"
    mock_common_state.store.remove.assert_not_called()


def test_stop_without_json_unaffected(mock_common_state, capsys):
    """Regression guard: default (non-`--json`) path emits no JSON."""
    mock_common_state.json_output = False
    mock_common_state.store.get.return_value = None
    mock_common_state.resolve_session.return_value = "missing"

    stop(session="missing")

    out = capsys.readouterr().out
    assert "{" not in out
