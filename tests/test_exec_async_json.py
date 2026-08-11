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

"""`exec-async --json`: the synchronous submission envelope, and the
`--json-result-path` sidecar propagated to the spawned child so its
terminal result survives independently of the CLI/session."""

import json

from unittest.mock import MagicMock, patch

import pytest
import typer

# Importing colab_cli.cli installs the `--json`-aware `typer.echo` wrapper as
# a side effect of module load. This test calls `exec_async` directly
# (bypassing `colab_cli.cli.app`/`CliRunner`), so the import must be explicit
# here rather than relying on another test module having pulled it in first.
import colab_cli.cli  # noqa: F401
from colab_cli.commands.execution import exec_async, spawn_exec_async


@pytest.fixture
def mock_store(mock_common_state):
    return mock_common_state.store


@patch("colab_cli.commands.execution.spawn_exec_async")
def test_exec_async_json_propagates_sidecar_path_to_child(
    mock_spawn, mock_store, mock_common_state
):
    mock_session = MagicMock()
    mock_session.name = "s1"
    mock_session.exec_pid = None
    mock_store.get.return_value = mock_session
    mock_store.list.return_value = {"s1": mock_session}
    mock_common_state.resolve_session.return_value = "s1"
    mock_common_state.history.log_dir = "/tmp/history"
    mock_common_state.json_output = True
    mock_spawn.return_value = 5555

    exec_async(session="s1", file="script.py")

    assert (
        mock_spawn.call_args.kwargs["json_result_path"]
        == "/tmp/history/s1.exec.log.json"
    )


@patch("colab_cli.commands.execution.spawn_exec_async")
def test_exec_async_without_json_does_not_propagate_sidecar_path(
    mock_spawn, mock_store, mock_common_state
):
    """Regression guard: the default (non-`--json`) path is unaffected."""
    mock_session = MagicMock()
    mock_session.name = "s1"
    mock_session.exec_pid = None
    mock_store.get.return_value = mock_session
    mock_store.list.return_value = {"s1": mock_session}
    mock_common_state.resolve_session.return_value = "s1"
    mock_common_state.history.log_dir = "/tmp/history"
    mock_common_state.json_output = False
    mock_spawn.return_value = 5555

    exec_async(session="s1", file="script.py")

    assert mock_spawn.call_args.kwargs["json_result_path"] is None


@patch("colab_cli.commands.execution.spawn_exec_async")
def test_exec_async_json_submission_envelope(
    mock_spawn, mock_store, mock_common_state, capsys, mocker
):
    mocker.patch("colab_cli.auto_update.get_app_version", return_value="9.9.9")
    mock_session = MagicMock()
    mock_session.name = "s1"
    mock_session.exec_pid = None
    mock_store.get.return_value = mock_session
    mock_store.list.return_value = {"s1": mock_session}
    mock_common_state.resolve_session.return_value = "s1"
    mock_common_state.history.log_dir = "/tmp/history"
    mock_common_state.json_output = True
    mock_spawn.return_value = 5555

    exec_async(session="s1", file="script.py")

    captured = capsys.readouterr()
    envelope = json.loads(captured.out)
    assert envelope == {
        "schema_version": "1",
        "cli_version": "9.9.9",
        "status": "started",
        "exit_code": 0,
        "pid": 5555,
        "log_path": "/tmp/history/s1.exec.log",
    }


def test_exec_async_json_already_running_error_envelope(
    mock_store, mock_common_state, capsys
):
    mock_session = MagicMock()
    mock_session.name = "s1"
    mock_session.exec_pid = 111
    mock_store.get.return_value = mock_session
    mock_common_state.resolve_session.return_value = "s1"
    mock_common_state.json_output = True

    with patch("colab_cli.commands.execution.pid_alive", return_value=True):
        with pytest.raises(typer.Exit):
            exec_async(session="s1", file="script.py")

    captured = capsys.readouterr()
    envelope = json.loads(captured.out)
    assert envelope["status"] == "error"
    assert envelope["reason"] == "already_running"


def test_exec_async_json_session_not_found_error_envelope(
    mock_store, mock_common_state, capsys
):
    mock_store.get.return_value = None
    mock_common_state.resolve_session.return_value = "missing"
    mock_common_state.json_output = True

    with pytest.raises(typer.Exit):
        exec_async(session="missing", file="script.py")

    captured = capsys.readouterr()
    envelope = json.loads(captured.out)
    assert envelope["status"] == "error"
    assert envelope["reason"] == "session_not_found"


def test_spawn_exec_async_appends_json_result_path_flag(mocker):
    mock_popen = mocker.patch("colab_cli.commands.execution.subprocess.Popen")
    mock_popen.return_value.pid = 12345
    mocker.patch("builtins.open", mocker.mock_open())

    spawn_exec_async(
        "script.py",
        "sess1",
        "/tmp/sess1.exec.log",
        json_result_path="/tmp/sess1.exec.log.json",
    )

    cmd = mock_popen.call_args.args[0]
    assert "--json-result-path" in cmd
    assert cmd[cmd.index("--json-result-path") + 1] == "/tmp/sess1.exec.log.json"
    # The child must never receive --json itself -- only the sidecar path --
    # or its output_hook would be suppressed and live rendering would break.
    assert "--json" not in cmd


def test_spawn_exec_async_omits_flag_when_no_json_result_path(mocker):
    mock_popen = mocker.patch("colab_cli.commands.execution.subprocess.Popen")
    mock_popen.return_value.pid = 12345
    mocker.patch("builtins.open", mocker.mock_open())

    spawn_exec_async("script.py", "sess1", "/tmp/sess1.exec.log")

    cmd = mock_popen.call_args.args[0]
    assert "--json-result-path" not in cmd


@patch("colab_cli.commands.execution.spawn_exec_async")
def test_exec_async_removes_stale_sidecar_before_restart(
    mock_spawn, mock_store, mock_common_state, tmp_path
):
    """A finished run #1 leaves `<log_path>.json` behind. A restarted run #2
    at the same log path must not let that stale sidecar shadow its own
    in-flight state -- `log --tail --json` checks the sidecar before pid
    liveness, so a poller during run #2 would otherwise see run #1's
    terminal verdict while the new job is still executing."""
    sidecar_path = tmp_path / "s1.exec.log.json"
    sidecar_path.write_text('{"status": "ok", "exit_code": 0}')

    mock_session = MagicMock()
    mock_session.name = "s1"
    mock_session.exec_pid = None
    mock_store.get.return_value = mock_session
    mock_store.list.return_value = {"s1": mock_session}
    mock_common_state.resolve_session.return_value = "s1"
    mock_common_state.history.log_dir = str(tmp_path)
    mock_common_state.json_output = False
    mock_spawn.return_value = 6666

    exec_async(session="s1", file="script.py")

    assert not sidecar_path.exists()


@patch("colab_cli.commands.execution.spawn_exec_async")
def test_exec_async_no_error_when_no_stale_sidecar(
    mock_spawn, mock_store, mock_common_state, tmp_path
):
    """Regression guard: the common case (no leftover sidecar) must not
    raise just because there's nothing to remove."""
    mock_session = MagicMock()
    mock_session.name = "s1"
    mock_session.exec_pid = None
    mock_store.get.return_value = mock_session
    mock_store.list.return_value = {"s1": mock_session}
    mock_common_state.resolve_session.return_value = "s1"
    mock_common_state.history.log_dir = str(tmp_path)
    mock_common_state.json_output = False
    mock_spawn.return_value = 7777

    exec_async(session="s1", file="script.py")

    mock_spawn.assert_called_once()
