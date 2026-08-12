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

"""`new --json`: success envelope, and the three failure reasons
(accelerator_rejected, auth_scope_missing, new_failed)."""

import json

from unittest.mock import MagicMock

import pytest
import typer

# Importing colab_cli.cli installs the `--json`-aware `typer.echo` wrapper as
# a side effect of module load. These tests call `new()` directly (bypassing
# `colab_cli.cli.app`/`CliRunner`), so the import must be explicit here
# rather than relying on another test module having pulled it in first.
import colab_cli.cli  # noqa: F401
from colab_cli.client import ColabRequestError, PostAssignmentResponse
from colab_cli.commands.session import new


def _make_response_error(status_code, body="", message="error"):
    response = MagicMock()
    response.status_code = status_code
    response.reason = message
    return ColabRequestError(
        message, request=MagicMock(), response=response, response_body=body
    )


@pytest.fixture
def mock_spawn_keep_alive(mocker):
    return mocker.patch("colab_cli.commands.session.spawn_keep_alive", return_value=9999)


def test_new_json_success_envelope_shape(
    mock_common_state, mock_spawn_keep_alive, capsys, mocker
):
    mocker.patch("colab_cli.auto_update.get_app_version", return_value="9.9.9")
    mock_common_state.json_output = True
    mock_res = MagicMock()
    mock_res.__class__ = PostAssignmentResponse
    mock_res.runtime_proxy_info.token = "tok"
    mock_res.runtime_proxy_info.url = "http://runtime"
    mock_res.endpoint = "ep-1"
    mock_common_state.client.assign.return_value = mock_res

    new(session="s1", gpu="T4")

    envelope = json.loads(capsys.readouterr().out)
    assert envelope["schema_version"] == "1"
    assert envelope["cli_version"] == "9.9.9"
    assert envelope["command"] == "new"
    assert envelope["status"] == "ok"
    assert envelope["exit_code"] == 0
    assert envelope["session"] == "s1"
    assert envelope["endpoint"] == "ep-1"
    assert envelope["variant"] == "GPU"
    assert envelope["accelerator"] == "T4"


def test_new_json_accelerator_rejected(mock_common_state, capsys):
    mock_common_state.json_output = True
    mock_common_state.client.assign.side_effect = _make_response_error(400)

    with pytest.raises(typer.Exit) as excinfo:
        new(session="s1", gpu="H100")
    assert excinfo.value.exit_code == 1

    envelope = json.loads(capsys.readouterr().out)
    assert envelope["status"] == "error"
    assert envelope["reason"] == "accelerator_rejected"
    assert envelope["http_status"] == 400


def test_new_json_accelerator_rejected_not_triggered_without_accelerator(
    mock_common_state, capsys
):
    """A 400 with no --gpu/--tpu requested isn't an accelerator rejection --
    must fall through to the generic new_failed wrap, not the specific one."""
    mock_common_state.json_output = True
    mock_common_state.client.assign.side_effect = _make_response_error(400)

    with pytest.raises(ColabRequestError):
        new(session="s1")

    envelope = json.loads(capsys.readouterr().out)
    assert envelope["reason"] == "new_failed"
    assert envelope["http_status"] == 400


def test_new_json_auth_scope_missing(mock_common_state, capsys):
    mock_common_state.json_output = True
    mock_res = MagicMock()
    mock_res.__class__ = PostAssignmentResponse
    mock_res.runtime_proxy_info.token = "tok"
    mock_res.runtime_proxy_info.url = "http://runtime"
    mock_res.endpoint = "ep-1"
    mock_common_state.client.assign.return_value = mock_res
    mock_common_state.client.keep_alive_assignment.side_effect = _make_response_error(
        403, body="...SCOPE_NOT_PERMITTED..."
    )

    with pytest.raises(typer.Exit) as excinfo:
        new(session="s1")
    assert excinfo.value.exit_code == 1

    envelope = json.loads(capsys.readouterr().out)
    assert envelope["status"] == "error"
    assert envelope["reason"] == "auth_scope_missing"
    assert envelope["http_status"] == 403
    mock_common_state.client.unassign.assert_called_once_with("ep-1")


def test_new_json_generic_failure_emits_new_failed(mock_common_state, capsys):
    mock_common_state.json_output = True
    mock_common_state.client.assign.side_effect = RuntimeError("connection reset")

    with pytest.raises(RuntimeError):
        new(session="s1")

    envelope = json.loads(capsys.readouterr().out)
    assert envelope["status"] == "error"
    assert envelope["reason"] == "new_failed"
    assert envelope["http_status"] is None


def test_new_logs_assign_error_history_event(mock_common_state):
    """assign failure previously left zero trace in session history --
    unlike keep-alive's own error path, which always logs one. `new` must
    log an `assign_error` event regardless of which error branch the
    caller sees."""
    error = _make_response_error(
        400, body="<html><b>400.</b> That's an error.</html>"
    )
    error.response.headers = {"Content-Type": "text/html; charset=UTF-8"}
    mock_common_state.client.assign.side_effect = error

    with pytest.raises(typer.Exit):
        new(session="s1", gpu="H100")

    log_calls = mock_common_state.history.log_event.call_args_list
    assign_errors = [c for c in log_calls if c.args[1] == "assign_error"]
    assert len(assign_errors) == 1
    payload = assign_errors[0].args[2]
    assert payload["status_code"] == 400
    assert payload["error_type"] == "ColabRequestError"
    assert payload["variant"] == "GPU"
    assert payload["accelerator"] == "H100"
    # HTML body must never be logged, matching response_body_if_json's
    # content-type gate.
    assert payload["response_body"] is None


def test_new_without_json_unaffected(mock_common_state, mock_spawn_keep_alive, capsys):
    """Regression guard: default (non-`--json`) path emits no JSON."""
    mock_common_state.json_output = False
    mock_res = MagicMock()
    mock_res.__class__ = PostAssignmentResponse
    mock_res.runtime_proxy_info.token = "tok"
    mock_res.runtime_proxy_info.url = "http://runtime"
    mock_res.endpoint = "ep-1"
    mock_common_state.client.assign.return_value = mock_res

    new(session="s1")

    out = capsys.readouterr().out
    assert "{" not in out
