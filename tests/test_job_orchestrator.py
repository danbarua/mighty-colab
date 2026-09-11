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

"""Behavioural tests for the apply state machine and the envelope predicates.

These assert what a *caller* observes -- the verdict tuple, whether the VM
got released, what `retry_class` tells the next agent turn to do -- and not
which private method ran. The distinction matters here because the whole
value of the envelope is that an unattended agent can act on it without
reading the implementation.
"""

from contextlib import redirect_stdout
from enum import Enum
from io import StringIO
from types import SimpleNamespace
from unittest.mock import MagicMock, mock_open, patch

import pytest

from colab_cli.auto_update import get_app_version
from colab_cli.job.models import (
    Accelerator,
    ArtifactItem,
    Budgets,
    Cleanup,
    CodeSpec,
    Control,
    ControlChannel,
    DataItem,
    JobEnvelope,
    JobSpec,
    Offload,
    Phase,
    Plan,
    RetryClass,
    Supervisor,
    Workload,
)
from colab_cli.job.orchestrator import Orchestrator, PhaseError
from colab_cli.job.store import JobStore


class FakeStatus(str, Enum):
    OK = "ok"
    NOT_FOUND = "not_found"
    DEGRADED = "degraded"
    SESSION_LOST = "session_lost"


def _spec(**kw) -> JobSpec:
    base = dict(
        name="unit",
        code=CodeSpec(kind="file", entry="train.py"),
        accelerator=Accelerator(prefer=["T4"], accept_cpu=False),
        budgets=Budgets(wall_clock=60),
    )
    base.update(kw)
    return JobSpec(**base)


def _plan(spec: JobSpec) -> Plan:
    return Plan(job_id="unit-job", spec_hash="deadbeef", created_at="now", spec=spec)


def _orch(tmp_path, spec=None, client=None, runtime=None, session_store=None):
    spec = spec or _spec()
    return Orchestrator(
        plan=_plan(spec),
        store=JobStore(tmp_path / "jobs"),
        client=client or MagicMock(),
        runtime_factory=lambda url, token: runtime or MagicMock(),
        transport_factory=lambda s: MagicMock(),
        session_store=session_store or MagicMock(),
    )


class _LaunchRuntime:
    """Execute the launch cell while capturing what the runner receives."""

    def __init__(self, control_url=None):
        self.argv = None
        self.code = None
        self.env = None
        self.control_url = control_url
        self.chmod = None
        self.unlinked = None

        self.namespace = None

    def execute_code(self, code, timeout):
        self.code = code

        def capture(argv, **kwargs):
            self.argv = argv
            self.env = dict(kwargs["env"])
            return SimpleNamespace(pid=4312)

        def control_exists(path):
            return bool(self.control_url and path.endswith("result.put-url"))

        stdout = StringIO()
        with (
            patch("os.makedirs"),
            patch("os.path.exists", side_effect=control_exists),
            patch("os.chmod", side_effect=lambda path, mode: setattr(self, "chmod", (path, mode))),
            patch("os.unlink", side_effect=lambda path: setattr(self, "unlinked", path)),
            patch("builtins.open", mock_open(read_data=self.control_url or "")),
            patch("subprocess.Popen", side_effect=capture),
            redirect_stdout(stdout),
        ):
            self.namespace = {}
            exec(code, self.namespace)
        return [{"text": stdout.getvalue()}]


def test_launch_reads_control_result_url_from_a_staged_file(tmp_path):
    put_url = "https://storage.example/result.json?secret=signature"
    spec = _spec(
        code=CodeSpec(kind="file", entry="train.py", args=["--deadline", "user"]),
        control=Control(
            result=ControlChannel(
                put_url=put_url,
                get_url="https://storage.example/result.json?secret=reader",
            )
        ),
    )
    runtime = _LaunchRuntime(control_url=put_url)
    orch = _orch(tmp_path, spec=spec, runtime=runtime)
    orch.session_state = SimpleNamespace(url="https://vm", token="token")

    assert orch.launch("/content/jobs/unit-job/mighty_runtime") == 4312
    separator = runtime.argv.index("--")
    assert "--result-put-url" not in runtime.argv
    assert put_url not in runtime.code
    assert put_url not in runtime.argv
    assert runtime.env["MIGHTY_CONTROL_RESULT_PUT_URL"] == put_url
    assert put_url not in repr(runtime.namespace)
    assert runtime.chmod[1] == 0o600
    assert runtime.unlinked.endswith("result.put-url")
    assert runtime.argv[separator + 1 :] == ["--deadline", "user"]


def test_launch_persists_the_kernel_target_for_session_commands(tmp_path):
    runtime = _LaunchRuntime()
    runtime.kernel_id = "kernel-used-to-launch"
    runtime.session_id = "session-used-to-launch"
    session_store = MagicMock()
    orch = _orch(tmp_path, runtime=runtime, session_store=session_store)
    orch.session_state = SimpleNamespace(
        url="https://vm",
        token="token",
        kernel_id=None,
        session_id=None,
    )

    orch.launch("/content/jobs/unit-job/mighty_runtime")

    assert orch.session_state.kernel_id == "kernel-used-to-launch"
    assert orch.session_state.session_id == "session-used-to-launch"
    session_store.add.assert_called_once_with(orch.session_state)


def test_job_envelope_identifies_the_cli_that_created_it(tmp_path):
    orch = _orch(tmp_path)

    assert orch.env.cli_version == get_app_version()

# --------------------------------------------------------------------------
# Envelope predicates
# --------------------------------------------------------------------------


def test_done_requires_all_four_fields_terminal():
    """A finished process is not a finished job.

    The artifacts may still be uploading and the VM may still be billing;
    an agent that treats process exit as `done` stops polling and leaks
    both.
    """
    env = JobEnvelope(job_id="j", workload=Workload.SUCCEEDED)
    assert not env.done
    env.offload = Offload.OK
    assert not env.done
    env.cleanup = Cleanup.RELEASED
    assert not env.done
    env.supervisor = Supervisor.FINISHED
    assert env.done


def test_ok_is_stricter_than_done_for_a_failed_workload():
    env = JobEnvelope(
        job_id="j",
        workload=Workload.FAILED,
        offload=Offload.OK,
        cleanup=Cleanup.RELEASED,
        supervisor=Supervisor.FINISHED,
    )
    assert env.done
    assert not env.ok


def test_left_up_is_ok_but_still_reports_the_endpoint():
    """`left_up` was asked for, so it is not a failure -- but it is still
    billing, which is why the endpoint must survive into the envelope."""
    env = JobEnvelope(
        job_id="j",
        workload=Workload.SUCCEEDED,
        offload=Offload.OK,
        cleanup=Cleanup.LEFT_UP,
        supervisor=Supervisor.FINISHED,
        endpoint="m-s-abc",
    )
    assert env.ok and env.done
    assert env.endpoint == "m-s-abc"


def test_succeeded_workload_with_failed_cleanup_is_not_ok():
    """The leaked-VM case. `done` is true because nothing is still moving,
    but `ok` must be false or the caller never learns it is paying."""
    env = JobEnvelope(
        job_id="j",
        workload=Workload.SUCCEEDED,
        offload=Offload.OK,
        cleanup=Cleanup.FAILED,
        supervisor=Supervisor.FINISHED,
        endpoint="m-s-abc",
    )
    assert env.done
    assert not env.ok


def test_no_declared_artifacts_can_still_reach_ok():
    env = JobEnvelope(
        job_id="j",
        workload=Workload.SUCCEEDED,
        offload=Offload.NOT_REQUIRED,
        cleanup=Cleanup.RELEASED,
        supervisor=Supervisor.FINISHED,
    )
    assert env.ok


def test_interrupted_supervisor_is_terminal_so_done_can_be_reached():
    """Without this the tuple has no state for "nobody is driving", and a
    job whose supervisor died would poll as unfinished forever."""
    env = JobEnvelope(
        job_id="j",
        workload=Workload.UNKNOWN,
        offload=Offload.SKIPPED,
        cleanup=Cleanup.LEFT_UP,
        supervisor=Supervisor.INTERRUPTED,
    )
    assert env.done
    assert not env.ok


# --------------------------------------------------------------------------
# Provision
# --------------------------------------------------------------------------


def test_provision_refuses_a_cpu_box_when_a_gpu_was_requested(tmp_path):
    """The silent-substitution failure. Upstream maps unknown accelerators
    onto A100 and capacity pressure can hand back a CPU box; accepting it
    is how you publish chance-level results from a run that never had a
    GPU."""
    client = MagicMock()
    client.assign.return_value = SimpleNamespace(
        accelerator=SimpleNamespace(name="NONE"),
        endpoint="m-s-cpu",
        runtime_proxy_info=SimpleNamespace(token="t", url="https://u"),
        variant=SimpleNamespace(name="DEFAULT"),
        machine_shape="STANDARD",
    )
    orch = _orch(tmp_path, client=client)

    with pytest.raises(PhaseError) as exc:
        orch.provision()

    assert exc.value.retry_class is RetryClass.RETRY_DIFFERENT
    # and it must not leave the CPU box assigned
    client.unassign.assert_called_once_with("m-s-cpu")


def test_provision_accepts_cpu_when_the_spec_asked_for_it(tmp_path):
    client = MagicMock()
    client.assign.return_value = SimpleNamespace(
        accelerator=SimpleNamespace(name="NONE"),
        endpoint="m-s-cpu",
        runtime_proxy_info=SimpleNamespace(token="t", url="https://u"),
        variant=SimpleNamespace(name="DEFAULT"),
        machine_shape="STANDARD",
    )
    spec = _spec(accelerator=Accelerator(prefer=[], accept_cpu=True))
    orch = _orch(tmp_path, spec=spec, client=client)

    orch.provision()

    assert orch.env.actual_accelerator == "NONE"
    assert orch.env.endpoint == "m-s-cpu"
    client.unassign.assert_not_called()


def test_provision_walks_the_preference_list_in_order(tmp_path):
    """Capacity varies by hour; the point of an ordered list is that the
    second choice is tried automatically rather than failing the job."""
    client = MagicMock()
    client.assign.side_effect = [
        RuntimeError("A100 unavailable"),
        SimpleNamespace(
            accelerator=SimpleNamespace(name="T4"),
            endpoint="m-s-t4",
            runtime_proxy_info=SimpleNamespace(token="t", url="https://u"),
            variant=SimpleNamespace(name="GPU"),
            machine_shape="STANDARD",
        ),
    ]
    spec = _spec(accelerator=Accelerator(prefer=["A100", "T4"], accept_cpu=False))
    orch = _orch(tmp_path, spec=spec, client=client)

    orch.provision()

    assert orch.env.actual_accelerator == "T4"
    assert orch.env.requested_accelerator == "T4"
    assert client.assign.call_count == 2


# --------------------------------------------------------------------------
# Verify
# --------------------------------------------------------------------------


def _runtime_returning(text: str) -> MagicMock:
    rt = MagicMock()
    rt.execute_code.return_value = [{"output_type": "stream", "text": text}]
    return rt


def test_verify_fails_when_a_declared_dependency_is_not_importable(tmp_path):
    rt = _runtime_returning(
        'VERIFY={"deps": {"torch": "MISSING: PackageNotFoundError"}, '
        '"device": "Tesla T4", "free": 100000000000}'
    )
    spec = _spec(deps=["torch==2.4.1"])
    orch = _orch(tmp_path, spec=spec, runtime=rt)
    orch.session_state = SimpleNamespace(url="https://u", token="t")
    orch.env.actual_accelerator = "T4"

    with pytest.raises(PhaseError) as exc:
        orch.verify()

    assert exc.value.retry_class is RetryClass.FIX_CODE
    assert "torch" in exc.value.reason


def test_verify_fails_when_a_gpu_was_granted_but_is_invisible(tmp_path):
    """Assigned-but-not-visible is a real Colab state, and it is worse than
    an outright failure because the job would otherwise run on CPU at 1/50th
    the speed and still report success."""
    rt = _runtime_returning('VERIFY={"deps": {}, "device": null, "free": 100000000000}')
    orch = _orch(tmp_path, runtime=rt)
    orch.session_state = SimpleNamespace(url="https://u", token="t")
    orch.env.actual_accelerator = "T4"

    with pytest.raises(PhaseError) as exc:
        orch.verify()

    assert exc.value.retry_class is RetryClass.RETRY_DIFFERENT


def test_verify_refuses_when_declared_inputs_cannot_fit_on_disk(tmp_path):
    rt = _runtime_returning('VERIFY={"deps": {}, "device": null, "free": 1000}')
    spec = _spec(
        accelerator=Accelerator(prefer=[], accept_cpu=True),
        data=[DataItem(url="https://x/y.npy", dest="d.npy", size_bytes=10_000)],
    )
    orch = _orch(tmp_path, spec=spec, runtime=rt)
    orch.session_state = SimpleNamespace(url="https://u", token="t")
    orch.env.actual_accelerator = "NONE"

    with pytest.raises(PhaseError) as exc:
        orch.verify()

    assert "free" in exc.value.reason


def test_verify_passes_on_cpu_when_no_gpu_was_requested(tmp_path):
    rt = _runtime_returning('VERIFY={"deps": {}, "device": null, "free": 100000000000}')
    orch = _orch(tmp_path, runtime=rt)
    orch.session_state = SimpleNamespace(url="https://u", token="t")
    orch.env.actual_accelerator = "NONE"

    orch.verify()  # must not raise
def test_verify_counts_declared_artifact_space_before_launch(tmp_path):
    spec = _spec(
        accelerator=Accelerator(prefer=[], accept_cpu=True),
        artifacts=[
            ArtifactItem(
                path="/content/out/model.pt",
                url="https://x/model.pt",
                size_bytes=10_000,
                required=False,
            )
        ],
    )
    orch = _orch(tmp_path, spec=spec)

    with pytest.raises(PhaseError) as exc:
        orch._check_disk(1000)

    assert "inputs and artifacts" in exc.value.reason


# --------------------------------------------------------------------------
# Result absorption
# --------------------------------------------------------------------------


def test_missing_required_artifact_fails_offload_even_on_a_clean_exit(tmp_path):
    """Exit 0 with nothing written is the "it ran, but where are the
    results" failure. Reporting `ok` there is a lie the caller acts on."""
    spec = _spec(
        artifacts=[ArtifactItem(path="/content/out/model.pt", url="https://x/m.pt")]
    )
    orch = _orch(tmp_path, spec=spec)
    orch._absorb_result(
        {
            "workload": "succeeded",
            "exit_code": 0,
            "artifacts": [
                {
                    "path": "/content/out/model.pt",
                    "url_id": "https://x/m.pt#abc123",
                    "status": "missing",
                }
            ],
        }
    )
    assert orch.env.workload is Workload.SUCCEEDED
    assert orch.env.offload is Offload.FAILED
    assert orch.env.retry_class is RetryClass.FIX_CODE
    assert not orch.env.ok


def test_optional_artifact_missing_does_not_fail_offload(tmp_path):
    spec = _spec(
        artifacts=[
            ArtifactItem(path="/content/out/extra.png", url="https://x/e.png",
                         required=False)
        ]
    )
    orch = _orch(tmp_path, spec=spec)
    orch._absorb_result(
        {
            "workload": "succeeded",
            "exit_code": 0,
            "artifacts": [
                {
                    "path": "/content/out/extra.png",
                    "url_id": "https://x/e.png#abc123",
                    "status": "missing",
                }
            ],
        }
    )
    assert orch.env.offload is Offload.OK


def test_surviving_descendants_are_surfaced_as_a_hint(tmp_path):
    """A setsid grandchild outliving a `succeeded` verdict is invisible to
    a process-group scan and still holds the GPU. Proven on a live VM."""
    orch = _orch(tmp_path)
    orch._absorb_result(
        {"workload": "succeeded", "exit_code": 0, "surviving_descendants": [4242]}
    )
    assert orch.env.surviving_descendants == [4242]
    assert any("descendant" in h for h in orch.env.hints)


# --------------------------------------------------------------------------
# Poll classification
# --------------------------------------------------------------------------


def test_degraded_transport_never_becomes_session_lost(tmp_path):
    """The expensive misclassification: calling a healthy VM dead makes a
    retrying agent provision a second one alongside the first."""
    orch = _orch(tmp_path)
    transport = MagicMock()
    transport.read_json.return_value = (None, FakeStatus.DEGRADED)

    orch.poll(transport, deadline=0, interval=0)

    assert orch.env.workload is not Workload.UNKNOWN
    assert orch.env.supervisor is Supervisor.INTERRUPTED


def test_session_lost_yields_unknown_not_failed(tmp_path):
    """`unknown` is the one verdict an agent cannot act on, so it needs the
    tightest evidence -- but a vanished assignment genuinely is unknown:
    the job may well have finished before the VM went."""
    orch = _orch(tmp_path)
    transport = MagicMock()
    transport.read_json.return_value = (None, FakeStatus.SESSION_LOST)

    orch.poll(transport, deadline=9e18, interval=0)

    assert orch.env.workload is Workload.UNKNOWN
    assert orch.env.retry_class is RetryClass.RETRY_SAME
    assert orch.env.supervisor is Supervisor.FINISHED


def test_poll_returns_the_verdict_when_result_json_appears(tmp_path):
    orch = _orch(tmp_path)
    transport = MagicMock()
    transport.read_json.return_value = (
        {"workload": "failed", "exit_code": 1},
        FakeStatus.OK,
    )

    orch.poll(transport, deadline=9e18, interval=0)

    assert orch.env.workload is Workload.FAILED
    assert orch.env.exit_code == 1
    assert orch.env.supervisor is Supervisor.FINISHED


# --------------------------------------------------------------------------
# Cleanup
# --------------------------------------------------------------------------


def test_cleanup_failure_does_not_overwrite_the_workload_verdict(tmp_path):
    """Orthogonality, tested. A successful run whose teardown failed must
    keep saying `succeeded` -- the results are real -- while the leak is
    reported in its own field."""
    client = MagicMock()
    client.unassign.side_effect = RuntimeError("boom")
    orch = _orch(tmp_path, client=client)
    orch.env.workload = Workload.SUCCEEDED
    orch.env.endpoint = "m-s-abc"

    orch.cleanup()

    assert orch.env.workload is Workload.SUCCEEDED
    assert orch.env.cleanup is Cleanup.FAILED
    assert orch.env.endpoint == "m-s-abc"
    assert any("billing" in h for h in orch.env.hints)


def test_cleanup_with_no_endpoint_is_already_absent_not_failed(tmp_path):
    """An early failure never got a VM. Reporting that as a teardown
    failure would send an agent chasing a leak that does not exist."""
    orch = _orch(tmp_path)
    orch.cleanup()
    assert orch.env.cleanup is Cleanup.ALREADY_ABSENT


def test_leave_up_records_the_endpoint_and_a_destroy_hint(tmp_path):
    client = MagicMock()
    orch = _orch(tmp_path, client=client)
    orch.env.endpoint = "m-s-abc"

    orch.cleanup(force_leave_up=True)

    assert orch.env.cleanup is Cleanup.LEFT_UP
    client.unassign.assert_not_called()
    assert any("job destroy" in h for h in orch.env.hints)

def test_cleanup_closes_the_kernel_client_when_the_vm_is_left_up(tmp_path):
    runtime = MagicMock()
    orch = _orch(tmp_path, runtime=runtime)
    orch.session_state = SimpleNamespace(url="https://vm", token="token")
    orch._runtime_handle()
    orch.env.endpoint = "m-s-abc"

    orch.cleanup(force_leave_up=True)

    runtime.stop.assert_called_once_with()


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------


def test_envelope_is_persisted_on_every_phase_transition(tmp_path):
    """A `job status` run from a different process must be able to answer,
    which it cannot if the envelope only ever lived in the applying
    process's memory."""
    orch = _orch(tmp_path)
    orch._set_phase(Phase.PROVISION)

    reloaded = JobStore(tmp_path / "jobs").read_envelope("unit-job")
    assert reloaded is not None
    assert reloaded.phase is Phase.PROVISION


def test_launch_clears_an_inherited_control_url(tmp_path):
    runtime = _LaunchRuntime()
    orch = _orch(tmp_path, runtime=runtime)
    orch.session_state = SimpleNamespace(url="https://vm", token="token")

    with patch.dict(
        "os.environ",
        {"MIGHTY_CONTROL_RESULT_PUT_URL": "https://stale.example?secret=old"},
    ):
        orch.launch("/content/jobs/unit-job/mighty_runtime")

    assert runtime.env.get("MIGHTY_CONTROL_RESULT_PUT_URL") is None


def test_absorb_result_honors_remote_offload_failure_and_phase(tmp_path):
    spec = _spec(
        artifacts=[ArtifactItem(path="/content/out/model.pt", url="https://x/model")]
    )
    orch = _orch(tmp_path, spec=spec)

    orch._absorb_result(
        {
            "workload": "succeeded",
            "exit_code": 0,
            "phase": "offload",
            "offload": "failed",
            "artifacts": [],
        }
    )

    assert orch.env.workload is Workload.SUCCEEDED
    assert orch.env.phase is Phase.OFFLOAD
    assert orch.env.offload is Offload.FAILED
