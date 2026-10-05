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

"""`usage`: the account's compute-unit balance, from `GET /tun/m/ccu-info`."""

import json
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from colab_cli.cli import app
from colab_cli.client import CcuInfo, Client, ColabRequestError, Prod

runner = CliRunner()

# A live response body (2026-10-05), as Colab sends it.
LIVE_BODY = (
    ")]}'\n"
    '{"currentBalance":108.58074996725507,"consumptionRateHourly":0.16,'
    '"assignmentsCount":2,"eligibleGpus":["G4","A100","L4","T4"],'
    '"ineligibleGpus":["H100"],"eligibleTpus":["V6E1","V5E1"]}'
)


def _ccu(**overrides) -> CcuInfo:
    body = json.loads(LIVE_BODY.split("\n", 1)[1])
    body.update(overrides)
    return CcuInfo.model_validate(body)


def test_get_ccu_info_reads_the_live_response_shape():
    session = MagicMock()
    response = MagicMock(ok=True, text=LIVE_BODY)
    session.request.return_value = response

    ccu = Client(Prod(), session).get_ccu_info()

    assert ccu.current_balance == pytest.approx(108.58074996725507)
    assert ccu.consumption_rate_hourly == 0.16
    assert ccu.assignments_count == 2
    assert ccu.eligible_gpus == ["G4", "A100", "L4", "T4"]
    assert ccu.ineligible_gpus == ["H100"]
    assert ccu.eligible_tpus == ["V6E1", "V5E1"]
    method, url = session.request.call_args.args
    assert method == "GET"
    assert url.endswith("/tun/m/ccu-info")


def test_accelerator_lists_missing_from_the_response_stay_unknown():
    ccu = CcuInfo.model_validate(
        {"currentBalance": 1.0, "consumptionRateHourly": 0.0, "assignmentsCount": 0}
    )

    assert ccu.eligible_gpus is None
    assert ccu.ineligible_gpus is None
    assert ccu.eligible_tpus is None


def test_usage_prints_balance_rate_assignments_and_accelerators(mock_common_state):
    mock_common_state.client.get_ccu_info.return_value = _ccu()

    result = runner.invoke(app, ["usage"])

    assert result.exit_code == 0, result.output
    assert "Current balance: 108.58 compute units" in result.output
    assert "Usage rate: 0.16/hr" in result.output
    assert "Active assignments: 2" in result.output
    assert "Eligible GPUs: G4, A100, L4, T4" in result.output
    assert "Ineligible GPUs: H100" in result.output
    assert "Eligible TPUs: V6E1, V5E1" in result.output


def test_usage_json_envelope(mock_common_state):
    mock_common_state.client.get_ccu_info.return_value = _ccu()
    mock_common_state.json_output = True

    result = runner.invoke(app, ["--json", "usage"])

    assert result.exit_code == 0, result.output
    envelope = json.loads(result.stdout.strip().splitlines()[-1])
    assert envelope["command"] == "usage"
    assert envelope["status"] == "ok"
    assert envelope["current_balance"] == pytest.approx(108.58074996725507)
    assert envelope["consumption_rate_hourly"] == 0.16
    assert envelope["assignments_count"] == 2
    assert envelope["eligible_gpus"] == ["G4", "A100", "L4", "T4"]
    assert envelope["ineligible_gpus"] == ["H100"]
    assert envelope["eligible_tpus"] == ["V6E1", "V5E1"]


def test_usage_json_leaves_out_accelerator_lists_colab_did_not_send(mock_common_state):
    mock_common_state.client.get_ccu_info.return_value = CcuInfo.model_validate(
        {"currentBalance": 1.0, "consumptionRateHourly": 0.0, "assignmentsCount": 0}
    )
    mock_common_state.json_output = True

    result = runner.invoke(app, ["--json", "usage"])

    envelope = json.loads(result.stdout.strip().splitlines()[-1])
    assert "eligible_gpus" not in envelope
    assert "eligible_tpus" not in envelope


def test_usage_failure_reports_the_http_status_and_body(mock_common_state):
    response = MagicMock(status_code=403, reason="Forbidden")
    response.headers = {"Content-Type": "application/json"}
    response.text = '{"error": {"code": 403, "message": "scope missing"}}'
    mock_common_state.client.get_ccu_info.side_effect = ColabRequestError(
        "Failed to issue request GET https://colab.research.google.com/tun/m/ccu-info: Forbidden",
        request=MagicMock(),
        response=response,
        response_body=response.text,
    )
    mock_common_state.json_output = True

    result = runner.invoke(app, ["--json", "usage"])

    assert result.exit_code == 1
    envelope = json.loads(result.stdout.strip().splitlines()[-1])
    assert envelope["status"] == "error"
    assert envelope["reason"] == "usage_unavailable"
    assert envelope["http_status"] == 403
    assert "Forbidden" in envelope["message"]
    assert "scope missing" in envelope["message"]


def test_usage_is_json_capable_and_an_mcp_tool():
    import typer

    from colab_cli.cli import JSON_CAPABLE_COMMANDS
    from colab_cli.mcp_server import build_tools

    tools, _ = build_tools(typer.main.get_command(app))

    assert "usage" in JSON_CAPABLE_COMMANDS
    assert "usage" in {t.name for t in tools}
