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

"""`log --tail --json`: the three-state sidecar-aware response
(running / finished-via-sidecar / worker_terminated), the `--since-offset`
byte cursor, and resolving the default log+sidecar path after the session
record has been removed (e.g. by `stop`)."""

import json

import pytest
import typer

from colab_cli.state import SessionState

# Importing colab_cli.cli installs the `--json`-aware `typer.echo` wrapper
# (log() calls emit_json -> typer.echo(..., file=sys.stdout), unaffected by
# the wrapper either way, but this keeps the module import consistent with
# the other direct-call test files in this suite).
import colab_cli.cli  # noqa: F401
from colab_cli.commands.utility import log


def _mock_state(mocker, **overrides):
    mock_state = mocker.patch("colab_cli.commands.utility.state")
    mock_state.json_output = True
    for key, value in overrides.items():
        setattr(mock_state, key, value)
    return mock_state


def test_log_tail_json_running_no_sidecar(mocker, tmp_path, capsys):
    log_file = tmp_path / "s1.exec.log"
    log_file.write_text("step 0\n")

    mock_state = _mock_state(mocker)
    mock_state.store.get.return_value = SessionState(
        name="s1", token="t", url="u", endpoint="e", exec_pid=999,
        exec_log_path=str(log_file),
    )
    mocker.patch("colab_cli.commands.utility.pid_alive", return_value=True)
    mocker.patch("colab_cli.auto_update.get_app_version", return_value="9.9.9")

    log(session="s1", tail=True)

    envelope = json.loads(capsys.readouterr().out)
    assert envelope["status"] == "running"
    assert envelope["content"] == "step 0\n"
    assert envelope["next_offset"] == len("step 0\n")
    assert envelope["schema_version"] == "1"
    assert envelope["cli_version"] == "9.9.9"


def test_log_tail_json_dead_pid_no_sidecar_is_worker_terminated(mocker, tmp_path, capsys):
    log_file = tmp_path / "s1.exec.log"
    log_file.write_text("partial\n")

    mock_state = _mock_state(mocker)
    mock_state.store.get.return_value = SessionState(
        name="s1", token="t", url="u", endpoint="e", exec_pid=999,
        exec_log_path=str(log_file),
    )
    mocker.patch("colab_cli.commands.utility.pid_alive", return_value=False)

    log(session="s1", tail=True)

    envelope = json.loads(capsys.readouterr().out)
    assert envelope["status"] == "error"
    assert envelope["reason"] == "worker_terminated"
    assert envelope["content"] == "partial\n"


def test_log_tail_json_sidecar_present_returns_its_fields(mocker, tmp_path, capsys):
    log_file = tmp_path / "s1.exec.log"
    log_file.write_text("all done\n")
    sidecar = tmp_path / "s1.exec.log.json"
    sidecar.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "cli_version": "9.9.9",
                "status": "ok",
                "exit_code": 0,
                "blocks": [],
            }
        )
    )

    mock_state = _mock_state(mocker)
    mock_state.store.get.return_value = SessionState(
        name="s1", token="t", url="u", endpoint="e", exec_pid=999,
        exec_log_path=str(log_file),
    )
    # pid liveness must be irrelevant once a sidecar exists.
    mock_pid_alive = mocker.patch("colab_cli.commands.utility.pid_alive")

    log(session="s1", tail=True)

    envelope = json.loads(capsys.readouterr().out)
    assert envelope["status"] == "ok"
    assert envelope["exit_code"] == 0
    assert envelope["blocks"] == []
    assert envelope["content"] == "all done\n"
    assert envelope["next_offset"] == len("all done\n")
    mock_pid_alive.assert_not_called()


def test_log_tail_json_since_offset_returns_only_new_bytes(mocker, tmp_path, capsys):
    log_file = tmp_path / "s1.exec.log"
    log_file.write_text("first\nsecond\n")

    mock_state = _mock_state(mocker)
    mock_state.store.get.return_value = SessionState(
        name="s1", token="t", url="u", endpoint="e", exec_pid=999,
        exec_log_path=str(log_file),
    )
    mocker.patch("colab_cli.commands.utility.pid_alive", return_value=True)

    log(session="s1", tail=True, since_offset=len("first\n"))

    envelope = json.loads(capsys.readouterr().out)
    assert envelope["content"] == "second\n"
    assert envelope["next_offset"] == len("first\nsecond\n")


def test_log_tail_json_resolves_default_path_after_session_removed(
    mocker, tmp_path, capsys
):
    """After `stop` removes the session record, the default log+sidecar
    path is still derivable from the session name alone -- confirms the
    result survives teardown."""
    log_dir = tmp_path
    log_file = log_dir / "s1.exec.log"
    log_file.write_text("done\n")
    sidecar = log_dir / "s1.exec.log.json"
    sidecar.write_text(json.dumps({"status": "ok", "exit_code": 0}))

    mock_state = _mock_state(mocker)
    mock_state.store.get.return_value = None  # session record gone
    mock_state.history.log_dir = str(log_dir)

    log(session="s1", tail=True)

    envelope = json.loads(capsys.readouterr().out)
    assert envelope["status"] == "ok"
    assert envelope["content"] == "done\n"


def test_log_tail_json_no_job_found(mocker):
    mock_state = _mock_state(mocker)
    mock_state.store.get.return_value = SessionState(
        name="s1", token="t", url="u", endpoint="e"
    )

    with pytest.raises(typer.Exit) as excinfo:
        log(session="s1", tail=True)
    assert excinfo.value.exit_code == 1


def test_log_tail_json_requires_session(mocker):
    _mock_state(mocker)

    with pytest.raises(typer.Exit) as excinfo:
        log(session=None, tail=True)
    assert excinfo.value.exit_code == 2


def test_log_tail_without_json_is_unaffected(mocker, tmp_path, capsys):
    """Regression guard: the plain-text --tail path (no --json) must be
    byte-for-byte unchanged."""
    log_file = tmp_path / "s1.exec.log"
    log_file.write_text("plain text\n")

    mock_state = mocker.patch("colab_cli.commands.utility.state")
    mock_state.json_output = False
    mock_state.store.get.return_value = SessionState(
        name="s1", token="t", url="u", endpoint="e", exec_pid=999,
        exec_log_path=str(log_file),
    )

    log(session="s1", tail=True)

    out = capsys.readouterr().out
    assert out == "plain text\n"
