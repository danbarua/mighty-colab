"""Reason and retry class from a runner result (`colab_cli.job.verdict`),
applied through `Orchestrator.absorb_result`."""

from __future__ import annotations

import json

import pytest

from colab_cli.job.models import (
    Accelerator,
    ArtifactItem,
    Budgets,
    CodeSpec,
    JobEnvelope,
    JobSpec,
    Offload,
    Phase,
    RetryClass,
    TransferError,
    Workload,
)
from colab_cli.job.orchestrator import Orchestrator
from colab_cli.job.verdict import strongest, transfer_retry_class


def _spec(**kw) -> JobSpec:
    base = dict(
        name="unit",
        code=CodeSpec(kind="file", entry="train.py"),
        accelerator=Accelerator(prefer=[], accept_cpu=True),
        budgets=Budgets(wall_clock=600),
    )
    base.update(kw)
    return JobSpec(**base)


def _absorb(result: dict, spec: JobSpec | None = None) -> JobEnvelope:
    env = JobEnvelope(job_id="unit-job")
    Orchestrator.absorb_result(env, spec or _spec(), result)
    return env


def _artifact_spec(*paths, required=True):
    return _spec(
        artifacts=[ArtifactItem(path=p, url=f"https://x/{p}", required=required) for p in paths]
    )


def _failed_put(path, status=None, category="http"):
    return {
        "path": path,
        "url_id": f"https://x/{path}#1",
        "status": "failed",
        "error": {"exception": "HTTPStatusError", "reason": f"HTTP {status}",
                  "http_status": status, "category": category},
    }


# -- transfers ---------------------------------------------------------------


@pytest.mark.parametrize(
    "status, method, retry",
    [
        (401, "GET", RetryClass.REFRESH_URLS),
        (403, "PUT", RetryClass.REFRESH_URLS),
        (404, "GET", RetryClass.FIX_CODE),
        (404, "PUT", RetryClass.REFRESH_URLS),
        (413, "PUT", RetryClass.FIX_CODE),
        (400, "GET", RetryClass.FIX_CODE),
        (412, "PUT", RetryClass.FIX_CODE),
        (408, "GET", RetryClass.RETRY_SAME),
        (429, "PUT", RetryClass.RETRY_SAME),
        (500, "GET", RetryClass.RETRY_SAME),
        (503, "PUT", RetryClass.RETRY_SAME),
    ],
)
def test_http_status_maps_to_the_chosen_retry_class(status, method, retry):
    error = TransferError(exception="E", reason="r", http_status=status, category="http")
    assert transfer_retry_class(error, method) is retry


def test_several_failed_puts_take_the_strongest_class():
    assert strongest(
        [RetryClass.RETRY_SAME, RetryClass.REFRESH_URLS, RetryClass.FIX_CODE]
    ) is RetryClass.FIX_CODE
    assert strongest([RetryClass.RETRY_SAME, RetryClass.REFRESH_URLS]) is RetryClass.REFRESH_URLS
    assert strongest([]) is None


def test_failed_uploads_name_every_artifact_and_take_the_strongest_class():
    env = _absorb(
        {
            "workload": "succeeded",
            "exit_code": 0,
            "artifacts": [_failed_put("a.bin", 403), _failed_put("b.bin", 413)],
        },
        _artifact_spec("a.bin", "b.bin"),
    )
    assert env.offload is Offload.FAILED
    assert env.retry_class is RetryClass.FIX_CODE
    assert env.reason == "artifact offload failed: a.bin (HTTP 403); b.bin (HTTP 413)"
    assert env.failed_phase is Phase.OFFLOAD


def test_a_missing_and_a_failed_artifact_are_both_named():
    env = _absorb(
        {"workload": "succeeded", "exit_code": 0, "artifacts": [_failed_put("a.bin", 503)]},
        _artifact_spec("a.bin", "model.pt"),
    )
    assert env.reason == (
        "required artifact(s) not produced: model.pt; "
        "artifact offload failed: a.bin (HTTP 503)"
    )
    assert env.retry_class is RetryClass.FIX_CODE


def test_an_unreadable_offload_manifest_is_a_supervisor_fault():
    env = _absorb(
        {
            "workload": "succeeded",
            "exit_code": 0,
            "offload": "failed",
            "artifacts": [],
            "offload_error": "offload manifest unreadable: ManifestError: manifest x is unreadable",
        },
        _artifact_spec("a.bin", required=False),
    )
    assert env.offload is Offload.FAILED
    assert env.retry_class is RetryClass.DO_NOT_RETRY
    assert env.reason.startswith("artifact offload could not start: offload manifest unreadable")


# -- the workload ------------------------------------------------------------


def test_a_wall_clock_kill_names_the_budget_and_is_fix_code():
    env = _absorb(
        {
            "workload": "cancelled",
            "signal": 15,
            "signal_name": "SIGTERM",
            "cancel_intent": {"intent": "cancelled", "by": "wall_clock", "at": 1.0},
        }
    )
    assert env.workload is Workload.CANCELLED
    assert env.reason == (
        "wall_clock budget of 600s reached; the workload was stopped by SIGTERM (15)"
    )
    assert env.retry_class is RetryClass.FIX_CODE
    assert env.failed_phase is Phase.RUN


def test_a_wall_clock_kill_that_needed_sigkill_says_so():
    env = _absorb(
        {
            "workload": "cancelled",
            "signal": 9,
            "signal_name": "SIGKILL",
            "cancel_intent": {"intent": "cancelled", "by": "wall_clock"},
        }
    )
    assert env.reason == (
        "wall_clock budget of 600s reached; the workload did not exit within "
        "5s of SIGTERM and was killed by SIGKILL"
    )


def test_a_wall_clock_kill_with_a_missing_artifact_keeps_both_facts():
    env = _absorb(
        {
            "workload": "cancelled",
            "signal": 15,
            "signal_name": "SIGTERM",
            "cancel_intent": {"intent": "cancelled", "by": "wall_clock"},
            "artifacts": [],
        },
        _artifact_spec("model.pt"),
    )
    assert env.reason == (
        "wall_clock budget of 600s reached; the workload was stopped by SIGTERM (15); "
        "required artifact(s) not produced: model.pt"
    )
    assert env.retry_class is RetryClass.FIX_CODE
    assert env.failed_phase is Phase.RUN
    assert env.offload is Offload.FAILED


def test_a_destroy_cancel_names_the_requester_and_carries_no_class():
    env = _absorb(
        {
            "workload": "cancelled",
            "exit_code": 0,
            "cancel_intent": {"intent": "cancelled", "by": "job destroy"},
        }
    )
    assert env.reason == "cancelled by job destroy; the workload exited 0 after SIGTERM"
    assert env.retry_class is None
    assert env.failed_phase is None


def test_an_unreadable_cancel_record_is_still_a_cancel_and_says_why():
    env = _absorb(
        {
            "workload": "cancelled",
            "signal": 15,
            "signal_name": "SIGTERM",
            "cancel_intent": {"intent": "cancelled", "by": None,
                              "error": "JSONDecodeError: Expecting value"},
        }
    )
    assert env.reason.startswith(
        "cancelled (cancel.json unreadable: JSONDecodeError: Expecting value)"
    )


def test_an_unrequested_sigkill_with_oom_evidence_names_the_oom_killer():
    line = "[ 812.1] Out of memory: Killed process 4242 (python3) total-vm:13000000kB"
    env = _absorb(
        {
            "workload": "failed",
            "signal": 9,
            "signal_name": "SIGKILL",
            "oom_kills": 1,
            "oom_log": [line],
        }
    )
    assert env.reason == (
        "the workload was killed by SIGKILL (9) with no cancel request; "
        f"the kernel's out-of-memory killer ran 1 time(s) during the run: {line}"
    )
    assert env.retry_class is RetryClass.FIX_CODE
    assert env.failed_phase is Phase.RUN


def test_an_unrequested_sigkill_without_oom_evidence_says_none_was_seen():
    env = _absorb({"workload": "failed", "signal": 9, "signal_name": "SIGKILL", "oom_kills": 0})
    assert env.reason == (
        "the workload was killed by SIGKILL (9) with no cancel request; "
        "no kernel OOM kill during the run"
    )


def test_a_signal_the_vm_did_not_name_falls_back_to_its_number():
    env = _absorb({"workload": "failed", "signal": 7})
    assert env.reason == "the workload was killed by signal 7 with no cancel request"


def test_an_exception_is_the_reason_for_a_failed_workload():
    env = _absorb(
        {
            "workload": "failed",
            "exit_code": 1,
            "exception": {"type": "torch.OutOfMemoryError", "message": "CUDA out of memory",
                          "traceback": "..."},
        }
    )
    assert env.reason == "the workload exited 1: torch.OutOfMemoryError: CUDA out of memory"
    assert env.retry_class is RetryClass.FIX_CODE


def test_a_long_exception_message_is_cut_in_the_reason():
    env = _absorb(
        {"workload": "failed", "exit_code": 1,
         "exception": {"type": "ValueError", "message": "x" * 5000, "traceback": ""}}
    )
    assert env.reason.endswith(
        "x [... 4500 characters omitted; the full message is in `exception`]"
    )
    assert env.exception["message"] == "x" * 5000


def test_a_transfer_error_without_a_category_is_classified_and_logged(caplog):
    error = TransferError(exception="OSError", reason="boom")
    with caplog.at_level("WARNING", logger="colab_cli.job.verdict"):
        assert transfer_retry_class(error, "GET") is RetryClass.RETRY_SAME
    assert "transfer error has no category (exception=OSError reason=boom)" in caplog.text


def test_a_result_without_finished_at_uses_local_time_and_says_so(caplog):
    with caplog.at_level("WARNING", logger="colab_cli.job.orchestrator"):
        env = _absorb({"workload": "succeeded", "exit_code": 0})
    assert env.finished_at is not None
    assert "result.json finished_at None is not an epoch time" in caplog.text


def test_an_exit_without_an_exception_points_at_runner_log():
    env = _absorb({"workload": "failed", "exit_code": 7})
    assert env.reason == (
        "the workload exited 7 without a recorded exception; its output is in runner.log"
    )


def test_a_dataloader_oom_is_noted_beside_the_exception():
    env = _absorb(
        {
            "workload": "failed",
            "exit_code": 1,
            "exception": {"type": "RuntimeError", "message": "DataLoader worker is killed",
                          "traceback": ""},
            "oom_kills": 2,
            "oom_log": [],
        }
    )
    assert env.reason == (
        "the workload exited 1: RuntimeError: DataLoader worker is killed; "
        "the kernel's out-of-memory killer ran 2 time(s) during the run"
    )


def test_surviving_tagged_processes_are_the_reason():
    env = _absorb(
        {
            "workload": "failed",
            "exit_code": 0,
            "survivors_by_job_tag": [4242, 4243],
            "surviving_descendants": [4242, 4243],
            "runner_error": "tagged descendants survived containment",
        }
    )
    assert env.reason == (
        "the workload exited 0 but 2 tagged process(es) survived containment: 4242, 4243"
    )
    assert env.retry_class is RetryClass.FIX_CODE


def test_a_runner_without_a_verdict_is_a_supervisor_fault():
    env = _absorb(
        {"workload": "unknown", "runner_error": "OSError: [Errno 28] No space left on device"}
    )
    assert env.reason == (
        "the runner could not reach a verdict: OSError: [Errno 28] No space left on device"
    )
    assert env.retry_class is RetryClass.DO_NOT_RETRY
    assert env.failed_phase is Phase.RUN


def test_a_workload_failure_decides_the_class_over_an_upload_failure():
    env = _absorb(
        {
            "workload": "failed",
            "exit_code": 1,
            "exception": {"type": "ValueError", "message": "bad", "traceback": ""},
            "artifacts": [_failed_put("a.bin", 403)],
        },
        _artifact_spec("a.bin"),
    )
    assert env.retry_class is RetryClass.FIX_CODE
    assert env.reason == (
        "the workload exited 1: ValueError: bad; artifact offload failed: a.bin (HTTP 403)"
    )
    assert env.failed_phase is Phase.RUN


def test_runner_warnings_become_hints_once():
    env = JobEnvelope(job_id="unit-job")
    result = {"workload": "succeeded", "exit_code": 0,
              "runner_warnings": ["periodic sync of a.bin failed 3 time(s); last: HTTP 403"]}
    Orchestrator.absorb_result(env, _spec(), result)
    Orchestrator.absorb_result(env, _spec(), result)
    assert env.hints == ["runner: periodic sync of a.bin failed 3 time(s); last: HTTP 403"]


def test_finished_at_is_the_vms_time():
    env = _absorb({"workload": "succeeded", "exit_code": 0, "finished_at": 0})
    assert env.finished_at == "1970-01-01T00:00:00+00:00"


def test_a_signed_query_in_the_result_never_reaches_the_envelope_through_inputs():
    """The runner redacts before writing; this pins that the envelope keeps
    exactly what the runner wrote, and the runner test pins the redaction."""
    env = _absorb(
        {
            "workload": "failed",
            "exit_code": 1,
            "phase": "stage",
            "inputs": [{"dest": "a", "url_id": "https://x/a#1", "status": "failed",
                        "error": {"exception": "HTTPError", "reason": "HTTP Error 403: Forbidden",
                                  "http_status": 403, "body": "https://x/a#1 <redacted>",
                                  "category": "http"}}],
        }
    )
    assert "<redacted>" in json.dumps(env.model_dump(mode="json"))
