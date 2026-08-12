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

"""`run --json`: single-block envelope, the SystemExit(0)->ok rule, and
process exit code staying 0 even when the job itself raised."""

import json

from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from colab_cli.cli import app
from colab_cli.client import ColabRequestError, PostAssignmentResponse

runner = CliRunner()


def _make_response_error(status_code, body="", message="error"):
    response = MagicMock()
    response.status_code = status_code
    response.reason = message
    return ColabRequestError(
        message, request=MagicMock(), response=response, response_body=body
    )


@pytest.fixture
def mock_client(mock_common_state):
    return mock_common_state.client


@pytest.fixture
def mock_store(mock_common_state):
    return mock_common_state.store


@pytest.fixture
def mock_runtime_class(mocker):
    return mocker.patch("colab_cli.commands.run.ColabRuntime")


@pytest.fixture
def mock_spawn_keep_alive(mocker):
    return mocker.patch("colab_cli.commands.run.spawn_keep_alive", return_value=12345)


@pytest.fixture
def assign_response():
    res = MagicMock()
    res.__class__ = PostAssignmentResponse
    res.runtime_proxy_info.token = "tok"
    res.runtime_proxy_info.url = "http://runtime"
    res.endpoint = "ep-123"
    return res


@pytest.fixture
def script_path(tmp_path):
    p = tmp_path / "script.py"
    p.write_text("print('hello from script')\n")
    return p


@pytest.fixture(autouse=True)
def _persisted_store(mock_store):
    persisted = {}
    mock_store.add.side_effect = lambda s: persisted.__setitem__("s", s)
    mock_store.get.side_effect = lambda name: persisted.get("s")
    return persisted


def _systemexit_output(evalue: str):
    return {
        "output_type": "error",
        "ename": "SystemExit",
        "evalue": evalue,
        "traceback": [f"\x1b[0;31mSystemExit\x1b[0m: {evalue}\n"],
    }


def test_run_json_success_envelope_shape(
    mock_client,
    mock_store,
    mock_runtime_class,
    mock_spawn_keep_alive,
    mock_common_state,
    assign_response,
    script_path,
    mocker,
):
    mocker.patch("colab_cli.auto_update.get_app_version", return_value="9.9.9")
    mock_common_state.json_output = True
    mock_client.assign.return_value = assign_response
    mock_runtime = mock_runtime_class.return_value
    mock_runtime.execute_code.return_value = [{"output_type": "stream", "text": "hi\n"}]

    result = runner.invoke(app, ["run", str(script_path)])
    assert result.exit_code == 0, result.output

    envelope = json.loads(result.stdout)
    assert envelope["schema_version"] == "1"
    assert envelope["cli_version"] == "9.9.9"
    assert envelope["status"] == "ok"
    assert envelope["exit_code"] == 0
    assert "reason" not in envelope
    assert envelope["outputs"] == [{"output_type": "stream", "text": "hi\n"}]


def test_run_json_traceback_ansi_stripped_by_default_no_raw_field(
    mock_client,
    mock_store,
    mock_runtime_class,
    mock_spawn_keep_alive,
    mock_common_state,
    assign_response,
    script_path,
):
    """`run` shares `json_safe_outputs` with `exec` but had zero prior
    coverage of this field -- closing that gap alongside the opt-in flag."""
    mock_common_state.json_output = True
    mock_client.assign.return_value = assign_response
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

    result = runner.invoke(app, ["run", str(script_path)])
    assert result.exit_code == 0, result.output

    envelope = json.loads(result.stdout)
    assert envelope["status"] == "job_raised"
    out = envelope["outputs"][0]
    assert out["traceback"] == ["ValueError: boom\n"]
    assert "traceback_raw" not in out


def test_run_json_no_strip_ansi_keeps_raw_traceback_in_same_field(
    mock_client,
    mock_store,
    mock_runtime_class,
    mock_spawn_keep_alive,
    mock_common_state,
    assign_response,
    script_path,
):
    mock_common_state.json_output = True
    mock_common_state.no_strip_ansi = True
    mock_client.assign.return_value = assign_response
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

    result = runner.invoke(app, ["run", str(script_path)])
    assert result.exit_code == 0, result.output

    envelope = json.loads(result.stdout)
    assert envelope["status"] == "job_raised"
    out = envelope["outputs"][0]
    assert out["traceback"] == raw_tb
    assert "traceback_raw" not in out


def test_run_json_output_hook_suppressed_stdout_carries_json_only(
    mock_client,
    mock_store,
    mock_runtime_class,
    mock_spawn_keep_alive,
    mock_common_state,
    assign_response,
    script_path,
):
    """Regression guard: `display_output` writes stream outputs straight to
    sys.stdout, bypassing the typer.echo --json redirect entirely. If the
    hook isn't suppressed, the script's own print() output lands on stdout
    BEFORE the JSON envelope, breaking "stdout carries JSON only" for any
    caller doing `json.loads($(mighty-colab --json run ...))`."""
    mock_common_state.json_output = True
    mock_client.assign.return_value = assign_response
    mock_runtime = mock_runtime_class.return_value

    def mock_execute_code(code, output_hook=None, **kwargs):
        outputs = [{"output_type": "stream", "text": "hi\n"}]
        if output_hook:
            for o in outputs:
                output_hook(o)
        return outputs

    mock_runtime.execute_code.side_effect = mock_execute_code

    result = runner.invoke(app, ["run", str(script_path)])
    assert result.exit_code == 0, result.output

    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == 1, f"stdout must carry the JSON envelope only, got: {result.stdout!r}"
    envelope = json.loads(lines[0])
    assert envelope["status"] == "ok"


def test_run_without_json_output_hook_unchanged(
    mock_client,
    mock_store,
    mock_runtime_class,
    mock_spawn_keep_alive,
    mock_common_state,
    assign_response,
    script_path,
):
    """Regression guard: the default (non-`--json`) path must still pass
    the human-display hook -- confirms the branch is additive."""
    mock_common_state.json_output = False
    mock_client.assign.return_value = assign_response
    mock_runtime = mock_runtime_class.return_value
    mock_runtime.execute_code.return_value = []

    result = runner.invoke(app, ["run", str(script_path)])
    assert result.exit_code == 0, result.output
    assert mock_runtime.execute_code.call_args.kwargs["output_hook"] is not None


def test_run_json_systemexit_zero_is_ok_not_job_raised(
    mock_client,
    mock_store,
    mock_runtime_class,
    mock_spawn_keep_alive,
    mock_common_state,
    assign_response,
    script_path,
):
    mock_common_state.json_output = True
    mock_client.assign.return_value = assign_response
    mock_runtime = mock_runtime_class.return_value
    mock_runtime.execute_code.return_value = [_systemexit_output("0")]

    result = runner.invoke(app, ["run", str(script_path)])
    assert result.exit_code == 0, result.output

    envelope = json.loads(result.stdout)
    assert envelope["status"] == "ok"
    assert envelope["exit_code"] == 0
    assert "reason" not in envelope


def test_run_json_job_raised_keeps_process_exit_zero(
    mock_client,
    mock_store,
    mock_runtime_class,
    mock_spawn_keep_alive,
    mock_common_state,
    assign_response,
    script_path,
):
    mock_common_state.json_output = True
    mock_client.assign.return_value = assign_response
    mock_runtime = mock_runtime_class.return_value
    mock_runtime.execute_code.return_value = [
        {
            "output_type": "error",
            "ename": "ValueError",
            "evalue": "boom",
            "traceback": ["ValueError: boom\n"],
        }
    ]

    result = runner.invoke(app, ["run", str(script_path)])
    assert result.exit_code == 0, result.output

    envelope = json.loads(result.stdout)
    assert envelope["status"] == "job_raised"
    assert envelope["exit_code"] == 1
    assert envelope["reason"] == "job_raised"


def test_run_without_json_still_exits_nonzero_on_job_raised(
    mock_client,
    mock_store,
    mock_runtime_class,
    mock_spawn_keep_alive,
    mock_common_state,
    assign_response,
    script_path,
):
    """Regression guard: default (non-`--json`) behavior is unchanged --
    process exit code still reflects the job's own failure."""
    mock_common_state.json_output = False
    mock_client.assign.return_value = assign_response
    mock_runtime = mock_runtime_class.return_value
    mock_runtime.execute_code.return_value = [
        {
            "output_type": "error",
            "ename": "ValueError",
            "evalue": "boom",
            "traceback": ["ValueError: boom\n"],
        }
    ]

    result = runner.invoke(app, ["run", str(script_path)])
    assert result.exit_code == 1


def test_run_json_preflight_transport_failure_emits_error_envelope(
    mock_client,
    mock_store,
    mock_runtime_class,
    mock_spawn_keep_alive,
    mock_common_state,
    assign_response,
    script_path,
):
    """A genuine transport failure during the `/content` preflight call
    (observed live: a just-created kernel's websocket dropping before the
    reply arrives) must still leave stdout carrying a JSON error envelope,
    not a bare traceback -- this preflight step is separate from the main
    script-execution call and needs its own --json coverage."""
    mock_common_state.json_output = True
    mock_client.assign.return_value = assign_response
    mock_runtime = mock_runtime_class.return_value
    mock_runtime.execute_code.side_effect = RuntimeError("Connection was lost.")

    result = runner.invoke(app, ["run", str(script_path)])
    assert result.exit_code != 0

    envelope = json.loads(result.stdout)
    assert envelope["status"] == "error"
    assert envelope["reason"] == "preflight_failed"
    assert envelope["exit_code"] == 1


def test_run_json_main_execution_transport_failure_emits_error_envelope(
    mock_client,
    mock_store,
    mock_runtime_class,
    mock_spawn_keep_alive,
    mock_common_state,
    assign_response,
    script_path,
):
    """Distinct from the preflight-call failure above: a transport failure
    on the SECOND execute_code call (the actual script payload, after the
    preflight succeeded) must emit its own error envelope
    (reason=run_failed) before propagating."""
    mock_common_state.json_output = True
    mock_client.assign.return_value = assign_response
    mock_runtime = mock_runtime_class.return_value
    mock_runtime.execute_code.side_effect = [None, RuntimeError("websocket closed")]

    result = runner.invoke(app, ["run", str(script_path)])
    assert result.exit_code != 0

    envelope = json.loads(result.stdout)
    assert envelope["status"] == "error"
    assert envelope["reason"] == "run_failed"
    assert envelope["exit_code"] == 1


def test_run_json_preflight_session_lost_emits_error_envelope(
    mock_client,
    mock_store,
    mock_runtime_class,
    mock_spawn_keep_alive,
    mock_common_state,
    assign_response,
    script_path,
):
    mock_common_state.json_output = True
    mock_client.assign.return_value = assign_response
    mock_runtime = mock_runtime_class.return_value
    mock_runtime.execute_code.side_effect = Exception("404 Not Found")

    result = runner.invoke(app, ["run", str(script_path)])
    assert result.exit_code != 0

    envelope = json.loads(result.stdout)
    assert envelope["status"] == "error"
    assert envelope["reason"] == "session_lost"
    assert envelope["exit_code"] == 1


def test_run_json_preflight_session_lost_includes_http_status(
    mock_client,
    mock_store,
    mock_runtime_class,
    mock_spawn_keep_alive,
    mock_common_state,
    assign_response,
    script_path,
):
    mock_common_state.json_output = True
    mock_client.assign.return_value = assign_response
    mock_runtime = mock_runtime_class.return_value

    class _FakeHttpError(Exception):
        def __init__(self, message, status_code):
            super().__init__(message)
            self.status_code = status_code

    mock_runtime.execute_code.side_effect = _FakeHttpError("Not Found", 401)

    result = runner.invoke(app, ["run", str(script_path)])
    assert result.exit_code != 0

    envelope = json.loads(result.stdout)
    assert envelope["reason"] == "session_lost"
    assert envelope["http_status"] == 401


def test_run_json_script_not_found(mock_common_state, tmp_path):
    mock_common_state.json_output = True
    missing = tmp_path / "nope.py"

    result = runner.invoke(app, ["run", str(missing)])
    assert result.exit_code == 2

    envelope = json.loads(result.stdout)
    assert envelope["status"] == "error"
    assert envelope["reason"] == "script_not_found"
    assert envelope["exit_code"] == 2


def test_run_json_accelerator_rejected(mock_client, mock_common_state, script_path):
    """Mirrors `new --json`'s accelerator_rejected handling -- `run_command`
    only had the plain-text message, not the --json gating, until now."""
    mock_common_state.json_output = True
    mock_client.assign.side_effect = _make_response_error(400)

    result = runner.invoke(app, ["run", str(script_path), "--gpu", "H100"])
    assert result.exit_code == 1

    envelope = json.loads(result.stdout)
    assert envelope["command"] == "run"
    assert envelope["status"] == "error"
    assert envelope["reason"] == "accelerator_rejected"
    assert envelope["http_status"] == 400


def test_run_json_assign_generic_failure_emits_assign_failed(
    mock_client, mock_common_state, script_path
):
    """A non-400 (or accelerator-less) assign failure falls through to a
    generic catch-all, mirroring `new --json`'s `new_failed`."""
    mock_common_state.json_output = True
    mock_client.assign.side_effect = RuntimeError("connection reset")

    result = runner.invoke(app, ["run", str(script_path)])
    assert result.exit_code == 1

    envelope = json.loads(result.stdout)
    assert envelope["status"] == "error"
    assert envelope["reason"] == "assign_failed"


def test_run_json_auth_scope_missing(
    mock_client, mock_common_state, assign_response, script_path
):
    """Mirrors `new --json`'s auth_scope_missing handling for the same
    keep-alive preflight scope check, which `run_command` duplicates."""
    mock_common_state.json_output = True
    mock_client.assign.return_value = assign_response
    mock_client.keep_alive_assignment.side_effect = _make_response_error(
        403, body="...SCOPE_NOT_PERMITTED..."
    )

    result = runner.invoke(app, ["run", str(script_path)])
    assert result.exit_code == 1

    envelope = json.loads(result.stdout)
    assert envelope["status"] == "error"
    assert envelope["reason"] == "auth_scope_missing"
    assert envelope["http_status"] == 403
    mock_client.unassign.assert_called_once_with("ep-123")


def test_run_records_last_keep_alive_ping_on_preflight_success(
    mock_client,
    mock_runtime_class,
    mock_spawn_keep_alive,
    mock_common_state,
    assign_response,
    script_path,
    _persisted_store,
):
    """Mirrors `new`'s pre-flight ping recording -- `run` allocates its own
    session the same way and shares the same keep-alive preflight call."""
    mock_common_state.json_output = False
    mock_client.assign.return_value = assign_response
    mock_runtime = mock_runtime_class.return_value
    mock_runtime.execute_code.return_value = [{"output_type": "stream", "text": "hi\n"}]

    result = runner.invoke(app, ["run", str(script_path)])
    assert result.exit_code == 0, result.output

    saved = _persisted_store["s"]
    assert saved.last_keep_alive_ping is not None
    import datetime as dt

    dt.datetime.fromisoformat(saved.last_keep_alive_ping)


def test_run_json_malformed_env(mock_common_state, script_path):
    """`_parse_env_vars` is shared with exec/exec-async and predates
    --json -- validated up front, before any VM is allocated."""
    mock_common_state.json_output = True

    result = runner.invoke(
        app, ["run", str(script_path), "--env", "HF_TOKEN"]
    )
    assert result.exit_code == 2

    envelope = json.loads(result.stdout)
    assert envelope["command"] == "run"
    assert envelope["status"] == "error"
    assert envelope["reason"] == "invalid_env"
