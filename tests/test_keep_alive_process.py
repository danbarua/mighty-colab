"""Process and keep-alive helpers shared by `job` and the session commands."""

from __future__ import annotations


def test_kill_process_reports_a_process_that_will_not_stop(caplog):
    import subprocess
    import sys

    from colab_cli.common import kill_process

    stubborn = subprocess.Popen(
        [sys.executable, "-c",
         "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); time.sleep(30)"],
        stdout=subprocess.PIPE, text=True,
    )
    try:
        stubborn.stdout.readline()
        with caplog.at_level("WARNING"):
            assert kill_process(stubborn.pid) is False
        assert f"pid {stubborn.pid} still running 0.5s after SIGTERM" in caplog.text
    finally:
        stubborn.kill()
        stubborn.wait()

    polite = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    assert kill_process(polite.pid) is True


def test_the_keep_alive_daemon_writes_its_stderr_beside_the_session_file(tmp_path, monkeypatch):
    from colab_cli.commands import session as session_command

    seen = {}

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            seen["stderr"] = kwargs["stderr"].name
            self.pid = 99

    monkeypatch.setattr(session_command.subprocess, "Popen", FakePopen)
    config = tmp_path / "cfg" / "sessions.json"

    assert session_command.spawn_keep_alive("m-s-x", "job-x", config_path=str(config)) == 99
    assert seen["stderr"] == str(tmp_path / "cfg" / "keep-alive" / "job-x.log")
