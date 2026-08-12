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

"""`exec --json`: envelope shape, the SystemExit(0)->ok rule, sync output
suppression, ANSI-stripped tracebacks, and the sidecar-file mode used by
`exec-async`'s spawned child (`--json-result-path`)."""

import json

from unittest.mock import MagicMock, ANY

import pytest
from typer.testing import CliRunner

from colab_cli.cli import app

runner = CliRunner()


@pytest.fixture
def mock_store(mock_common_state):
    return mock_common_state.store


@pytest.fixture
def mock_runtime_class(mocker):
    return mocker.patch("colab_cli.commands.execution.ColabRuntime")


@pytest.fixture
def mock_session(mock_store):
    s = MagicMock()
    s.name = "s1"
    s.url = "http://url"
    s.token = "token"
    s.kernel_id = None
    s.session_id = None
    mock_store.get.return_value = s
    return s


def _systemexit_output(evalue: str):
    return {
        "output_type": "error",
        "ename": "SystemExit",
        "evalue": evalue,
        "traceback": [f"\x1b[0;31mSystemExit\x1b[0m: {evalue}\n"],
    }


def test_exec_json_success_envelope_shape(
    mock_session, mock_runtime_class, mock_common_state, mocker
):
    mocker.patch("colab_cli.auto_update.get_app_version", return_value="9.9.9")
    mock_common_state.json_output = True
    mock_common_state.resolve_session.return_value = "s1"
    mock_runtime = mock_runtime_class.return_value
    mock_runtime.execute_code.return_value = [{"output_type": "stream", "text": "hi\n"}]

    result = runner.invoke(app, ["exec", "-s", "s1"], input="print('hi')")
    assert result.exit_code == 0, result.output

    envelope = json.loads(result.stdout)
    assert envelope["schema_version"] == "1"
    assert envelope["cli_version"] == "9.9.9"
    assert envelope["status"] == "ok"
    assert envelope["exit_code"] == 0
    assert "reason" not in envelope
    assert len(envelope["blocks"]) == 1
    assert envelope["blocks"][0]["outputs"] == [
        {"output_type": "stream", "text": "hi\n"}
    ]


def test_exec_json_systemexit_zero_is_ok_not_job_raised(
    mock_session, mock_runtime_class, mock_common_state
):
    """The exact incident that motivated this design: `raise SystemExit(0)`
    at the end of an otherwise-successful script must resolve to
    `status='ok'`, never `job_raised` -- even though IPython reports it as
    an `error`-type output."""
    mock_common_state.json_output = True
    mock_common_state.resolve_session.return_value = "s1"
    mock_runtime = mock_runtime_class.return_value
    mock_runtime.execute_code.return_value = [_systemexit_output("0")]

    result = runner.invoke(app, ["exec", "-s", "s1"], input="raise SystemExit(0)")
    assert result.exit_code == 0, result.output

    envelope = json.loads(result.stdout)
    assert envelope["status"] == "ok"
    assert envelope["exit_code"] == 0
    assert "reason" not in envelope


def test_exec_json_job_raised_keeps_process_exit_zero(
    mock_session, mock_runtime_class, mock_common_state
):
    """A genuine job failure must be visible in the envelope (status,
    exit_code) but must NOT make the CLI process itself exit non-zero --
    that's the whole point of separating "did the CLI do its job" from
    "did the remote code succeed"."""
    mock_common_state.json_output = True
    mock_common_state.resolve_session.return_value = "s1"
    mock_runtime = mock_runtime_class.return_value
    mock_runtime.execute_code.return_value = [
        {
            "output_type": "error",
            "ename": "ValueError",
            "evalue": "boom",
            "traceback": ["ValueError: boom\n"],
        }
    ]

    result = runner.invoke(app, ["exec", "-s", "s1"], input="raise ValueError('boom')")
    assert result.exit_code == 0, result.output

    envelope = json.loads(result.stdout)
    assert envelope["status"] == "job_raised"
    assert envelope["exit_code"] == 1
    assert envelope["reason"] == "job_raised"


def test_exec_json_systemexit_nonzero_is_job_raised_with_code(
    mock_session, mock_runtime_class, mock_common_state
):
    mock_common_state.json_output = True
    mock_common_state.resolve_session.return_value = "s1"
    mock_runtime = mock_runtime_class.return_value
    mock_runtime.execute_code.return_value = [_systemexit_output("7")]

    result = runner.invoke(app, ["exec", "-s", "s1"], input="raise SystemExit(7)")
    assert result.exit_code == 0, result.output

    envelope = json.loads(result.stdout)
    assert envelope["status"] == "job_raised"
    assert envelope["exit_code"] == 7


def test_exec_json_session_not_found_is_error_and_exits_nonzero(mock_common_state):
    mock_common_state.json_output = True
    mock_common_state.store.get.return_value = None
    mock_common_state.resolve_session.return_value = "missing"

    result = runner.invoke(app, ["exec", "-s", "missing"])
    assert result.exit_code == 1

    envelope = json.loads(result.stdout)
    assert envelope["status"] == "error"
    assert envelope["reason"] == "session_not_found"
    assert envelope["exit_code"] == 1
    # Human chatter still goes to stderr, not mixed into the JSON stdout.
    assert "not found" in result.stderr


def test_exec_json_no_input_is_error_and_exits_nonzero(mock_session, mock_common_state, mocker):
    mock_common_state.json_output = True
    mock_common_state.resolve_session.return_value = "s1"
    mocker.patch("colab_cli.commands.execution.is_stdin_tty", return_value=True)

    result = runner.invoke(app, ["exec", "-s", "s1"])
    assert result.exit_code == 1

    envelope = json.loads(result.stdout)
    assert envelope["status"] == "error"
    assert envelope["reason"] == "no_input"


def test_exec_json_preflight_session_lost_emits_error_envelope(
    mock_session, mock_runtime_class, mock_common_state
):
    """A 404/401 during the `/content` preflight call must still leave
    stdout carrying a JSON error envelope (reason=session_lost), not a
    bare traceback -- and must still prune local state."""
    mock_common_state.json_output = True
    mock_common_state.resolve_session.return_value = "s1"
    mock_runtime = mock_runtime_class.return_value
    mock_runtime.execute_code.side_effect = Exception("404 Not Found")

    result = runner.invoke(app, ["exec", "-s", "s1"], input="print(1)")
    assert result.exit_code == 1

    envelope = json.loads(result.stdout)
    assert envelope["status"] == "error"
    assert envelope["reason"] == "session_lost"
    mock_common_state.prune_session.assert_called_once_with("s1")


def test_exec_json_preflight_session_lost_includes_http_status(
    mock_session, mock_runtime_class, mock_common_state
):
    """session_lost collapses 404 and 401 into one reason even though they
    mean different things (session gone vs. auth expired) -- http_status
    recovers that distinction without inventing a second reason code."""
    mock_common_state.json_output = True
    mock_common_state.resolve_session.return_value = "s1"
    mock_runtime = mock_runtime_class.return_value

    class _FakeHttpError(Exception):
        def __init__(self, message, status_code):
            super().__init__(message)
            self.status_code = status_code

    mock_runtime.execute_code.side_effect = _FakeHttpError("Not Found", 404)

    result = runner.invoke(app, ["exec", "-s", "s1"], input="print(1)")
    assert result.exit_code == 1

    envelope = json.loads(result.stdout)
    assert envelope["reason"] == "session_lost"
    assert envelope["http_status"] == 404


def test_exec_json_preflight_nonterminal_failure_emits_error_envelope(
    mock_session, mock_runtime_class, mock_common_state
):
    """A non-terminal transport failure during the `/content` preflight
    call (distinct from the main-execution-loop failure, reason=
    execution_failed) must emit its own error envelope before the
    exception propagates."""
    mock_common_state.json_output = True
    mock_common_state.resolve_session.return_value = "s1"
    mock_runtime = mock_runtime_class.return_value
    mock_runtime.execute_code.side_effect = RuntimeError("connection reset")

    result = runner.invoke(app, ["exec", "-s", "s1"], input="print(1)")
    assert result.exit_code != 0

    envelope = json.loads(result.stdout)
    assert envelope["status"] == "error"
    assert envelope["reason"] == "preflight_failed"


def test_exec_json_output_hook_suppressed(
    mock_session, mock_runtime_class, mock_common_state
):
    """stdout must carry the JSON only -- raw kernel stdout/ANSI must not be
    interleaved into it, so the hook is suppressed entirely under --json."""
    mock_common_state.json_output = True
    mock_common_state.resolve_session.return_value = "s1"
    mock_runtime = mock_runtime_class.return_value
    mock_runtime.execute_code.return_value = []

    result = runner.invoke(app, ["exec", "-s", "s1"], input="print('hi')")
    assert result.exit_code == 0, result.output
    mock_runtime.execute_code.assert_any_call(
        "print('hi')", output_hook=None, timeout=30.0
    )


def test_exec_without_json_output_hook_unchanged(
    mock_session, mock_runtime_class, mock_common_state
):
    """Regression guard: the default (non-`--json`) path must still pass
    the human-display hook -- confirms the branch is additive."""
    mock_common_state.json_output = False
    mock_common_state.resolve_session.return_value = "s1"
    mock_runtime = mock_runtime_class.return_value
    mock_runtime.execute_code.return_value = []

    result = runner.invoke(app, ["exec", "-s", "s1"], input="print('hi')")
    assert result.exit_code == 0, result.output
    mock_runtime.execute_code.assert_any_call(
        "print('hi')", output_hook=ANY, timeout=30.0
    )
    assert mock_runtime.execute_code.call_args.kwargs["output_hook"] is not None


def test_exec_json_traceback_ansi_stripped_with_raw_preserved(
    mock_session, mock_runtime_class, mock_common_state
):
    mock_common_state.json_output = True
    mock_common_state.resolve_session.return_value = "s1"
    mock_runtime = mock_runtime_class.return_value
    raw_tb = ["\x1b[0;31mValueError\x1b[0m\x1b[0;31m:\x1b[0m boom\n"]
    mock_runtime.execute_code.return_value = [
        {
            "output_type": "error",
            "ename": "ValueError",
            "evalue": "boom",
            "traceback": raw_tb,
        }
    ]

    result = runner.invoke(app, ["exec", "-s", "s1"], input="raise ValueError('boom')")
    assert result.exit_code == 0, result.output

    envelope = json.loads(result.stdout)
    out = envelope["blocks"][0]["outputs"][0]
    assert out["traceback"] == ["ValueError: boom\n"]
    assert out["traceback_raw"] == raw_tb


def test_exec_json_transport_failure_mid_run_emits_error_envelope(
    mock_session, mock_runtime_class, mock_common_state
):
    """A genuine transport failure (connection drop, TimeoutError) during
    the execute_code call must still leave stdout carrying a JSON error
    envelope, not a bare traceback -- the process still exits non-zero
    since this is a genuine CLI-level failure, not a job outcome."""
    mock_common_state.json_output = True
    mock_common_state.resolve_session.return_value = "s1"
    mock_runtime = mock_runtime_class.return_value
    mock_runtime.execute_code.side_effect = [
        None,  # the /content chdir preflight call
        TimeoutError("no output for 30s"),
    ]

    result = runner.invoke(app, ["exec", "-s", "s1"], input="print('hi')")
    assert result.exit_code != 0

    envelope = json.loads(result.stdout)
    assert envelope["status"] == "error"
    assert envelope["reason"] == "execution_failed"
    assert envelope["exit_code"] == 1


def test_exec_json_empty_code_is_ok_with_no_blocks(mock_session, mock_common_state):
    mock_common_state.json_output = True
    mock_common_state.resolve_session.return_value = "s1"

    result = runner.invoke(app, ["exec", "-s", "s1"], input="   \n  ")
    assert result.exit_code == 0, result.output

    envelope = json.loads(result.stdout)
    assert envelope == {
        "schema_version": envelope["schema_version"],
        "cli_version": envelope["cli_version"],
        "command": "exec",
        "status": "ok",
        "exit_code": 0,
        "blocks": [],
    }


def test_exec_json_result_path_writes_sidecar_not_stdout(
    mock_session, mock_runtime_class, mock_common_state, tmp_path
):
    """The hidden `--json-result-path` flag (used only by `exec-async`'s
    spawned child) must write the envelope to that file, not stdout -- and
    must build the envelope even when `state.json_output` is False (the
    child never receives `--json` itself)."""
    mock_common_state.json_output = False
    mock_common_state.resolve_session.return_value = "s1"
    mock_runtime = mock_runtime_class.return_value
    mock_runtime.execute_code.return_value = [{"output_type": "stream", "text": "hi\n"}]

    result_path = tmp_path / "result.json"
    result = runner.invoke(
        app,
        ["exec", "-s", "s1", "--json-result-path", str(result_path)],
        input="print('hi')",
    )
    assert result.exit_code == 0, result.output
    assert result.stdout == ""

    envelope = json.loads(result_path.read_text())
    assert envelope["status"] == "ok"
    assert envelope["exit_code"] == 0


def test_exec_json_result_path_does_not_suppress_output_hook(
    mock_session, mock_runtime_class, mock_common_state, tmp_path
):
    """The sidecar mechanism must not change live rendering -- `--tail`
    peeking at a still-running exec-async job depends on this."""
    mock_common_state.json_output = False
    mock_common_state.resolve_session.return_value = "s1"
    mock_runtime = mock_runtime_class.return_value
    mock_runtime.execute_code.return_value = []

    result_path = tmp_path / "result.json"
    result = runner.invoke(
        app,
        ["exec", "-s", "s1", "--json-result-path", str(result_path)],
        input="print('hi')",
    )
    assert result.exit_code == 0, result.output
    mock_runtime.execute_code.assert_any_call(
        "print('hi')", output_hook=ANY, timeout=30.0
    )
    assert mock_runtime.execute_code.call_args.kwargs["output_hook"] is not None
