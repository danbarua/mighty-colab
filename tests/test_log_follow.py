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

import pytest
import typer

from colab_cli.state import SessionState


def test_log_follow_requires_session():
    from colab_cli.commands.utility import log

    with pytest.raises(typer.Exit) as excinfo:
        log(session=None, follow=True)
    assert excinfo.value.exit_code == 2


def test_log_follow_errors_when_no_async_job(mocker):
    from colab_cli.commands.utility import log

    mock_state = mocker.patch("colab_cli.commands.utility.state")
    mock_state.store.get.return_value = SessionState(
        name="s1", token="t", url="u", endpoint="e"
    )

    with pytest.raises(typer.Exit) as excinfo:
        log(session="s1", follow=True)
    assert excinfo.value.exit_code == 1


def test_log_follow_streams_file_and_stops_when_process_exits(mocker, tmp_path, capsys):
    from colab_cli.commands.utility import log

    log_file = tmp_path / "s1.exec.log"
    log_file.write_text("hello\n")

    mock_state = mocker.patch("colab_cli.commands.utility.state")
    mock_state.store.get.return_value = SessionState(
        name="s1",
        token="t",
        url="u",
        endpoint="e",
        exec_pid=999,
        exec_log_path=str(log_file),
    )

    # First liveness check: alive (so we enter the read loop and pick up
    # "hello\n"); second: dead, so the loop reads any remaining bytes then
    # exits instead of polling forever.
    mocker.patch(
        "colab_cli.commands.utility.pid_alive", side_effect=[True, False, False]
    )
    mocker.patch("time.sleep")

    log(session="s1", follow=True)

    out = capsys.readouterr().out
    assert "hello\n" in out


def test_log_follow_picks_up_appended_content(mocker, tmp_path, capsys):
    from colab_cli.commands.utility import log

    log_file = tmp_path / "s1.exec.log"
    log_file.write_text("first\n")

    mock_state = mocker.patch("colab_cli.commands.utility.state")
    mock_state.store.get.return_value = SessionState(
        name="s1",
        token="t",
        url="u",
        endpoint="e",
        exec_pid=999,
        exec_log_path=str(log_file),
    )

    calls = {"n": 0}

    def fake_sleep(_):
        calls["n"] += 1
        if calls["n"] == 1:
            with open(log_file, "a") as f:
                f.write("second\n")

    mocker.patch(
        "colab_cli.commands.utility.pid_alive", side_effect=[True, True, False, False]
    )
    mocker.patch("time.sleep", side_effect=fake_sleep)

    log(session="s1", follow=True)

    out = capsys.readouterr().out
    assert "first\n" in out
    assert "second\n" in out
