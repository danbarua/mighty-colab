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

import datetime
import os
from unittest.mock import MagicMock, patch

import pytest
import typer

from colab_cli.state import SessionState
from colab_cli.commands.execution import exec_async, spawn_exec_async


def test_session_state_with_exec_pid():
    s = SessionState(
        name="test",
        token="tok",
        url="http://",
        endpoint="end",
        exec_pid=4321,
        exec_log_path="/tmp/test.exec.log",
    )
    data = s.model_dump()
    assert data["exec_pid"] == 4321
    assert data["exec_log_path"] == "/tmp/test.exec.log"

    s2 = SessionState(**data)
    assert s2.exec_pid == 4321
    assert s2.exec_log_path == "/tmp/test.exec.log"


def test_spawn_exec_async_command_includes_auth_and_config(mocker):
    """Mirrors spawn_keep_alive: global flags must precede the subcommand,
    and the daemon must run the actual `exec` command so it reuses all of
    its existing state bookkeeping (running/last_execution/history)."""
    from colab_cli.auth import AuthProvider

    mock_popen = mocker.patch("colab_cli.commands.execution.subprocess.Popen")
    mock_popen.return_value.pid = 12345
    mock_open = mocker.patch("builtins.open", mocker.mock_open())

    spawn_exec_async(
        "script.py",
        "sess1",
        "/tmp/sess1.exec.log",
        timeout=45.0,
        env=["A=1"],
        auth_provider=AuthProvider.ADC,
        config_path="/tmp/sessions.json",
    )

    mock_open.assert_called_once_with("/tmp/sess1.exec.log", "wb")
    cmd = mock_popen.call_args.args[0]
    assert "--auth=adc" in cmd
    assert "--config" in cmd
    cfg_idx = cmd.index("--config")
    assert cmd[cfg_idx + 1] == "/tmp/sessions.json"
    exec_idx = cmd.index("exec")
    assert cmd.index("--auth=adc") < exec_idx
    assert cfg_idx < exec_idx
    assert cmd[exec_idx + 1 : exec_idx + 3] == ["-s", "sess1"]
    assert "-f" in cmd
    assert cmd[cmd.index("-f") + 1] == "script.py"
    assert "--timeout" in cmd
    assert cmd[cmd.index("--timeout") + 1] == "45.0"
    assert "--env" in cmd
    assert cmd[cmd.index("--env") + 1] == "A=1"

    kwargs = mock_popen.call_args.kwargs
    assert kwargs["stderr"].__class__.__name__ == "int" or kwargs["stderr"] is not None
    assert kwargs["stdin"] is not None


def test_spawn_exec_async_always_invokes_exec_with_a_real_file(mocker):
    """`exec-async` never introduces a second execution path -- its child
    command is always `exec -f <file>`, a real file path, never piped
    stdin. That's what lets it inherit `exec`'s *entire* file-backed
    behavior for free, with zero duplicated logic, including the
    `sys.argv`/`__name__`/`__file__` script prelude `_build_script_prelude`
    adds to `exec_command`'s `if file and not is_nb:` branch (see
    docs/02_execution_and_interactive.md, 2b). `exec_async()` itself
    guarantees `file` is always populated by this point -- piped stdin is
    materialized to a temp `.py` file before `spawn_exec_async` is ever
    called -- so this invariant, not any exec-async-specific code, is the
    entire reason the fix applies there too."""
    mock_popen = mocker.patch("colab_cli.commands.execution.subprocess.Popen")
    mock_popen.return_value.pid = 12345
    mocker.patch("builtins.open", mocker.mock_open())

    spawn_exec_async("driver.py", "sess1", "/tmp/sess1.exec.log")

    cmd = mock_popen.call_args.args[0]
    exec_idx = cmd.index("exec")
    assert cmd[exec_idx + 1 : exec_idx + 5] == ["-s", "sess1", "-f", "driver.py"]


def test_spawn_exec_async_redirects_stdout_to_log_file(mocker):
    mock_popen = mocker.patch("colab_cli.commands.execution.subprocess.Popen")
    mock_popen.return_value.pid = 999
    fake_fp = MagicMock()
    mocker.patch("builtins.open", return_value=fake_fp)

    spawn_exec_async("script.py", "sess1", "/tmp/sess1.exec.log")

    assert mock_popen.call_args.kwargs["stdout"] is fake_fp
    fake_fp.close.assert_called_once()


@pytest.fixture
def mock_store(mock_common_state):
    return mock_common_state.store


@patch("colab_cli.commands.execution.spawn_exec_async")
def test_exec_async_spawns_and_records_pid(mock_spawn, mock_store, mock_common_state):
    mock_session = MagicMock()
    mock_session.name = "s1"
    mock_session.exec_pid = None
    mock_store.get.return_value = mock_session
    mock_store.list.return_value = {"s1": mock_session}
    mock_common_state.resolve_session.return_value = "s1"
    mock_common_state.history.log_dir = "/tmp/history"
    mock_spawn.return_value = 5555

    exec_async(session="s1", file="script.py")

    assert mock_spawn.called
    assert mock_spawn.call_args.args[0] == "script.py"
    assert mock_spawn.call_args.args[1] == "s1"
    assert "auth_provider" in mock_spawn.call_args.kwargs
    assert "config_path" in mock_spawn.call_args.kwargs

    assert mock_session.exec_pid == 5555
    assert mock_session.exec_log_path == "/tmp/history/s1.exec.log"
    assert mock_store.add.call_count == 2


@patch("colab_cli.commands.execution.spawn_exec_async")
def test_exec_async_output_log_overrides_default_path(
    mock_spawn, mock_store, mock_common_state, tmp_path
):
    """--output-log gives the caller (typically an autonomous agent) full
    control of where this run's raw stdout/stderr lands -- e.g. a sandboxed
    scratch dir it actually has write access to, instead of
    ~/.config/colab-cli/history. The parent directory may not exist yet."""
    mock_session = MagicMock()
    mock_session.name = "s1"
    mock_session.exec_pid = None
    mock_store.get.return_value = mock_session
    mock_store.list.return_value = {"s1": mock_session}
    mock_common_state.resolve_session.return_value = "s1"
    mock_spawn.return_value = 4321

    custom_log = tmp_path / "nested" / "log1.txt"
    assert not custom_log.parent.exists()

    exec_async(session="s1", file="script.py", output_log=str(custom_log))

    mock_spawn.assert_called_once()
    assert mock_spawn.call_args.args[2] == str(custom_log)
    assert mock_session.exec_log_path == str(custom_log)
    assert custom_log.parent.is_dir()


@patch("colab_cli.commands.execution.spawn_exec_async")
def test_exec_async_output_log_expands_user(
    mock_spawn, mock_store, mock_common_state, tmp_path, monkeypatch
):
    mock_session = MagicMock()
    mock_session.name = "s1"
    mock_session.exec_pid = None
    mock_store.get.return_value = mock_session
    mock_store.list.return_value = {"s1": mock_session}
    mock_common_state.resolve_session.return_value = "s1"
    mock_spawn.return_value = 111
    monkeypatch.setenv("HOME", str(tmp_path))

    exec_async(session="s1", file="script.py", output_log="~/mylog.txt")

    expected = str(tmp_path / "mylog.txt")
    assert mock_spawn.call_args.args[2] == expected
    assert mock_session.exec_log_path == expected


@patch("colab_cli.commands.execution.spawn_exec_async")
def test_exec_async_output_log_existing_directory_generates_unique_filename(
    mock_spawn, mock_store, mock_common_state, tmp_path
):
    """Pointing --output-log at a directory that already exists must not
    be treated as a literal file path -- a relaunch of the same driver
    would otherwise silently truncate the previous run's log AND its
    .json sidecar, which is the exact failure this feature exists to
    close. Directory mode generates a name from the session + a UTC
    timestamp instead."""
    mock_session = MagicMock()
    mock_session.name = "s1"
    mock_session.exec_pid = None
    mock_store.get.return_value = mock_session
    mock_store.list.return_value = {"s1": mock_session}
    mock_common_state.resolve_session.return_value = "s1"
    mock_spawn.return_value = 4321

    log_dir = tmp_path / "logs"
    log_dir.mkdir()

    exec_async(session="s1", file="script.py", output_log=str(log_dir))

    mock_spawn.assert_called_once()
    resolved = mock_spawn.call_args.args[2]
    assert resolved == mock_session.exec_log_path
    assert os.path.dirname(resolved) == str(log_dir)
    basename = os.path.basename(resolved)
    assert basename.startswith("s1_")
    assert basename.endswith(".log")
    # <session>_<YYYYmmddTHHMMSSffffffZ>.log (microsecond precision, so
    # back-to-back relaunches within the same second still get distinct
    # filenames -- see test_..._relaunch_does_not_clobber below).
    timestamp = basename[len("s1_") : -len(".log")]
    datetime.datetime.strptime(timestamp, "%Y%m%dT%H%M%S%fZ")


@patch("colab_cli.commands.execution.spawn_exec_async")
def test_exec_async_output_log_trailing_slash_creates_directory(
    mock_spawn, mock_store, mock_common_state, tmp_path
):
    """A trailing separator signals directory mode even when the directory
    doesn't exist yet -- it gets created, mirroring the existing
    make-the-parent-dirs behavior for the file-path branch."""
    mock_session = MagicMock()
    mock_session.name = "s1"
    mock_session.exec_pid = None
    mock_store.get.return_value = mock_session
    mock_store.list.return_value = {"s1": mock_session}
    mock_common_state.resolve_session.return_value = "s1"
    mock_spawn.return_value = 4321

    log_dir = tmp_path / "not-yet-created"
    assert not log_dir.exists()

    exec_async(session="s1", file="script.py", output_log=str(log_dir) + os.sep)

    resolved = mock_spawn.call_args.args[2]
    assert log_dir.is_dir()
    assert os.path.dirname(resolved) == str(log_dir)
    assert os.path.basename(resolved).startswith("s1_")


@patch("colab_cli.commands.execution.spawn_exec_async")
def test_exec_async_output_log_directory_mode_relaunch_does_not_clobber(
    mock_spawn, mock_store, mock_common_state, tmp_path
):
    """Two relaunches against the same directory must produce two distinct
    files -- the entire point of this feature."""
    mock_session = MagicMock()
    mock_session.name = "s1"
    mock_session.exec_pid = None
    mock_store.get.return_value = mock_session
    mock_store.list.return_value = {"s1": mock_session}
    mock_common_state.resolve_session.return_value = "s1"
    mock_spawn.return_value = 1

    log_dir = tmp_path / "logs"
    log_dir.mkdir()

    exec_async(session="s1", file="script.py", output_log=str(log_dir))
    first = mock_spawn.call_args.args[2]

    mock_session.exec_pid = None  # simulate the previous run having finished
    exec_async(session="s1", file="script.py", output_log=str(log_dir))
    second = mock_spawn.call_args.args[2]

    assert first != second


@patch("colab_cli.commands.execution.spawn_exec_async")
def test_exec_async_without_output_log_uses_default_path(
    mock_spawn, mock_store, mock_common_state
):
    """Regression guard: omitting --output-log must be byte-for-byte the
    same default-path behavior as before it existed."""
    mock_session = MagicMock()
    mock_session.name = "s1"
    mock_session.exec_pid = None
    mock_store.get.return_value = mock_session
    mock_store.list.return_value = {"s1": mock_session}
    mock_common_state.resolve_session.return_value = "s1"
    mock_common_state.history.log_dir = "/tmp/history"
    mock_spawn.return_value = 222

    exec_async(session="s1", file="script.py")

    assert mock_spawn.call_args.args[2] == "/tmp/history/s1.exec.log"
    assert mock_session.exec_log_path == "/tmp/history/s1.exec.log"


@patch("colab_cli.commands.execution.spawn_exec_async")
def test_exec_async_refuses_when_already_running(
    mock_spawn, mock_store, mock_common_state, mocker
):
    mock_session = MagicMock()
    mock_session.name = "s1"
    mock_session.exec_pid = 4242
    mock_store.get.return_value = mock_session
    mock_common_state.resolve_session.return_value = "s1"
    mocker.patch("colab_cli.commands.execution.pid_alive", return_value=True)

    with pytest.raises(typer.Exit) as excinfo:
        exec_async(session="s1", file="script.py")

    assert excinfo.value.exit_code == 1
    mock_spawn.assert_not_called()

@patch("colab_cli.commands.execution.spawn_exec_async")
def test_exec_async_does_not_refuse_when_exec_async_called_on_different_session(
    mock_spawn, mock_store, mock_common_state, mocker
):
    """The 'already running' guard is per-session: s1's live exec_pid must
    not block a fresh exec-async on an unrelated session s2."""
    mock_session1 = MagicMock()
    mock_session1.name = "s1"
    mock_session1.exec_pid = 4242
    mock_session1.exec_log_path = "/tmp/history/s1.exec.log"

    mock_session2 = MagicMock()
    mock_session2.name = "s2"
    mock_session2.exec_pid = None

    sessions = {"s1": mock_session1, "s2": mock_session2}
    mock_store.get.side_effect = lambda name: sessions[name]
    mock_store.list.return_value = sessions
    mock_common_state.resolve_session.side_effect = lambda session, **kwargs: session
    mock_common_state.history.log_dir = "/tmp/history"
    # s1's pid is alive -- if the guard were mistakenly global rather than
    # per-session, this would cause a false refusal on s2 too.
    mocker.patch("colab_cli.commands.execution.pid_alive", return_value=True)
    mock_spawn.return_value = 9999

    exec_async(session="s2", file="script.py")

    mock_spawn.assert_called_once()
    assert mock_spawn.call_args.args[1] == "s2"
    assert mock_session2.exec_pid == 9999
    # s1's own tracked state is untouched.
    assert mock_session1.exec_pid == 4242


@patch("colab_cli.commands.execution.spawn_exec_async")
def test_exec_async_refuses_when_log_path_collides_with_another_live_session(
    mock_spawn, mock_store, mock_common_state, mocker
):
    """Defensive invariant check: exec_async computes its log path from the
    session name alone. If some other live session's tracked exec_log_path
    already points at that exact file (hand-edited state, a future rename
    flow, a case-insensitive filesystem collision, ...), spawning must
    refuse loudly rather than silently truncating a file another live
    worker is actively writing -- the destructive part is `spawn_exec_async`
    opening the file in 'wb' mode, so this guard has to run before that
    call, not after."""
    mock_session1 = MagicMock()
    mock_session1.name = "s1"
    mock_session1.exec_pid = 4242
    # Collision: s1 already claims the exact path s2 is about to compute.
    mock_session1.exec_log_path = "/tmp/history/s2.exec.log"

    mock_session2 = MagicMock()
    mock_session2.name = "s2"
    mock_session2.exec_pid = None

    sessions = {"s1": mock_session1, "s2": mock_session2}
    mock_store.get.side_effect = lambda name: sessions[name]
    mock_store.list.return_value = sessions
    mock_common_state.resolve_session.side_effect = lambda session, **kwargs: session
    mock_common_state.history.log_dir = "/tmp/history"
    mocker.patch("colab_cli.commands.execution.pid_alive", return_value=True)

    with pytest.raises(typer.Exit) as excinfo:
        exec_async(session="s2", file="script.py")

    assert excinfo.value.exit_code == 1
    mock_spawn.assert_not_called()
    # Must fail before ever persisting s2's own state over the collision.
    assert mock_session2.exec_pid is None


@patch("colab_cli.commands.execution.spawn_exec_async")
def test_exec_async_allows_restart_when_previous_pid_dead(
    mock_spawn, mock_store, mock_common_state, mocker
):
    mock_session = MagicMock()
    mock_session.name = "s1"
    mock_session.exec_pid = 4242
    mock_store.get.return_value = mock_session
    mock_store.list.return_value = {"s1": mock_session}
    mock_common_state.resolve_session.return_value = "s1"
    mock_common_state.history.log_dir = "/tmp/history"
    mocker.patch("colab_cli.commands.execution.pid_alive", return_value=False)
    mock_spawn.return_value = 7777

    exec_async(session="s1", file="script.py")

    mock_spawn.assert_called_once()
    assert mock_session.exec_pid == 7777


def test_exec_async_session_not_found(mock_store, mock_common_state):
    mock_store.get.return_value = None
    mock_common_state.resolve_session.return_value = "missing"

    with pytest.raises(typer.Exit) as excinfo:
        exec_async(session="missing", file="script.py")
    assert excinfo.value.exit_code == 1


def test_exec_async_requires_file_when_stdin_is_tty(mock_store, mock_common_state, mocker):
    mock_session = MagicMock()
    mock_session.name = "s1"
    mock_session.exec_pid = None
    mock_store.get.return_value = mock_session
    mock_common_state.resolve_session.return_value = "s1"
    mocker.patch("colab_cli.commands.execution.is_stdin_tty", return_value=True)

    with pytest.raises(typer.Exit) as excinfo:
        exec_async(session="s1", file=None)
    assert excinfo.value.exit_code == 1


@patch("colab_cli.commands.execution.spawn_exec_async")
def test_exec_async_materializes_piped_stdin_to_a_temp_file(
    mock_spawn, mock_store, mock_common_state, mocker, tmp_path
):
    mock_session = MagicMock()
    mock_session.name = "s1"
    mock_session.exec_pid = None
    mock_store.get.return_value = mock_session
    mock_store.list.return_value = {"s1": mock_session}
    mock_common_state.resolve_session.return_value = "s1"
    mock_common_state.history.log_dir = "/tmp/history"
    mocker.patch("colab_cli.commands.execution.is_stdin_tty", return_value=False)
    mocker.patch("sys.stdin.read", return_value="print('hi')")
    mocker.patch(
        "colab_cli.commands.execution._exec_async_dir", return_value=str(tmp_path)
    )
    mock_spawn.return_value = 1234

    exec_async(session="s1", file=None)

    written_path = mock_spawn.call_args.args[0]
    assert written_path.startswith(str(tmp_path))
    with open(written_path) as f:
        assert f.read() == "print('hi')"
