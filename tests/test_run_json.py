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
from colab_cli.client import PostAssignmentResponse

runner = CliRunner()


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


def test_run_json_script_not_found(mock_common_state, tmp_path):
    mock_common_state.json_output = True
    missing = tmp_path / "nope.py"

    result = runner.invoke(app, ["run", str(missing)])
    assert result.exit_code == 2

    envelope = json.loads(result.stdout)
    assert envelope["status"] == "error"
    assert envelope["reason"] == "script_not_found"
    assert envelope["exit_code"] == 2
