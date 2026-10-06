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

"""A job envelope written by a newer CLI can carry fields this CLI does not
define. This CLI reads such an envelope, keeps the unknown fields when it
writes the envelope back, and logs a WARN naming them. The job spec still
rejects unknown fields."""

import json
import logging

import pytest
from pydantic import ValidationError

from colab_cli.job.models import (
    Accelerator,
    ArtifactItem,
    Budgets,
    CodeSpec,
    JobSpec,
    Offload,
    Workload,
)
from colab_cli.job.store import JobStore

NEWER_ENVELOPE = {
    "job_id": "from-a-newer-cli",
    "workload": "succeeded",
    "offload": "ok",
    "cleanup": "released",
    "supervisor": "finished",
    "future_field": {"note": "added by a newer CLI"},
    "artifacts": [
        {
            "path": "/content/out/model.pt",
            "url_id": "https://x/m.pt#abc",
            "status": "ok",
            "future_artifact_field": 7,
        }
    ],
    "compute_units_at_release": {
        "at": "2026-10-06T03:11:54+00:00",
        "balance": 107.8,
        "rate_hourly": 0.0,
        "assignments": 0,
        "future_reading_field": "x",
    },
}


def _write_raw(store: JobStore, envelope: dict) -> None:
    job_dir = store.job_dir(envelope["job_id"])
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "envelope.json").write_text(json.dumps(envelope))


def test_an_envelope_with_unknown_fields_is_read_with_its_known_fields(tmp_path):
    store = JobStore(tmp_path / "jobs")
    _write_raw(store, NEWER_ENVELOPE)

    env = store.read_envelope("from-a-newer-cli")

    assert env.workload is Workload.SUCCEEDED
    assert env.offload is Offload.OK
    assert env.artifacts[0].status == "ok"
    assert env.compute_units_at_release.balance == 107.8


def test_writing_the_envelope_back_keeps_the_unknown_fields(tmp_path):
    store = JobStore(tmp_path / "jobs")
    _write_raw(store, NEWER_ENVELOPE)

    env = store.read_envelope("from-a-newer-cli")
    env.hints.append("finished by an older CLI")
    store.write_envelope(env)

    written = json.loads((store.job_dir("from-a-newer-cli") / "envelope.json").read_text())
    assert written["future_field"] == {"note": "added by a newer CLI"}
    assert written["artifacts"][0]["future_artifact_field"] == 7
    assert written["compute_units_at_release"]["future_reading_field"] == "x"
    assert written["hints"] == ["finished by an older CLI"]


def test_reading_an_envelope_with_unknown_fields_logs_them_once_per_process(tmp_path, caplog):
    store = JobStore(tmp_path / "jobs")
    _write_raw(store, NEWER_ENVELOPE)

    with caplog.at_level(logging.WARNING, logger="colab_cli.job.store"):
        store.read_envelope("from-a-newer-cli")
        store.read_envelope("from-a-newer-cli")

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0]
    assert str(store.job_dir("from-a-newer-cli") / "envelope.json") in message
    assert "future_field" in message
    assert "artifacts[0].future_artifact_field" in message
    assert "compute_units_at_release.future_reading_field" in message
    assert "kept unchanged" in message


def test_listing_problem_is_none_for_an_envelope_with_unknown_fields(tmp_path):
    store = JobStore(tmp_path / "jobs")
    _write_raw(store, NEWER_ENVELOPE)

    env, problem = store.read_envelope_or_problem("from-a-newer-cli")

    assert problem is None
    assert env.job_id == "from-a-newer-cli"


def test_an_unknown_enum_value_still_makes_the_envelope_unreadable(tmp_path):
    """Only new fields are tolerated. A value this CLI cannot interpret,
    such as a new workload state, still fails the read."""
    store = JobStore(tmp_path / "jobs")
    _write_raw(store, {"job_id": "new-state", "workload": "preempted"})

    env, problem = store.read_envelope_or_problem("new-state")

    assert env is None
    assert problem.startswith("envelope unreadable")


def test_a_runner_record_with_an_unknown_key_is_absorbed_and_kept(tmp_path, caplog):
    from unittest.mock import MagicMock

    from colab_cli.job.models import Plan
    from colab_cli.job.orchestrator import Orchestrator

    spec = JobSpec(
        name="unit",
        code=CodeSpec(kind="file", entry="train.py"),
        accelerator=Accelerator(prefer=[], accept_cpu=True),
        budgets=Budgets(wall_clock=60),
        artifacts=[ArtifactItem(path="/content/out/model.pt", url="https://x/m.pt")],
    )
    orch = Orchestrator(
        plan=Plan(job_id="unit-job", spec_hash="deadbeef", created_at="now", spec=spec),
        store=JobStore(tmp_path / "jobs"),
        client=MagicMock(),
        runtime_factory=lambda url, token: MagicMock(),
        transport_factory=lambda _s: MagicMock(),
        session_store=MagicMock(),
    )

    with caplog.at_level(logging.WARNING):
        orch._absorb_result(
            {
                "workload": "succeeded",
                "exit_code": 0,
                "artifacts": [
                    {
                        "path": "/content/out/model.pt",
                        "url_id": "https://x/m.pt#abc",
                        "status": "ok",
                        "future_artifact_field": 7,
                    }
                ],
            }
        )

    assert orch.env.workload is Workload.SUCCEEDED
    assert orch.env.offload is Offload.OK
    assert orch.env.artifacts[0].model_extra == {"future_artifact_field": 7}
    assert any(
        "result.json" in r.getMessage() and "artifacts[0].future_artifact_field" in r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING
    )


def test_the_job_spec_still_rejects_unknown_fields():
    with pytest.raises(ValidationError, match="future_spec_field"):
        JobSpec.model_validate(
            {
                "name": "unit",
                "code": {"kind": "file", "entry": "train.py"},
                "future_spec_field": True,
            }
        )
    with pytest.raises(ValidationError, match="prefered"):
        JobSpec.model_validate(
            {
                "name": "unit",
                "code": {"kind": "file", "entry": "train.py"},
                "accelerator": {"prefered": ["T4"]},
            }
        )
