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

import json
import os
import tempfile
from contextlib import redirect_stdout
from enum import Enum
from io import StringIO
from types import SimpleNamespace
from unittest.mock import MagicMock, mock_open, patch

import pytest
import requests

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
def test_job_records_redact_signed_urls_and_hydrate_only_for_apply(tmp_path):
    sentinel = "ISSUE18_LOCAL_SENTINEL"
    signed = f"https://storage.example/input.bin?signature={sentinel}"
    spec = _spec(data=[DataItem(url=signed, dest="input.bin", size_bytes=1)])
    plan = _plan(spec)
    store = JobStore(tmp_path / "jobs")

    spec_path = store.write_spec(plan.job_id, spec)
    plan_path = store.write_plan(plan)
    secret_path = store.plan_secrets_path(plan_path)

    assert sentinel not in spec_path.read_text()
    assert sentinel not in plan_path.read_text()
    assert "mighty-colab-secret-sha256=" in plan_path.read_text()
    assert sentinel in secret_path.read_text()
    assert secret_path.stat().st_mode & 0o777 == 0o600
    assert sentinel not in store.read_plan(plan.job_id).spec.data[0].url
    assert store.read_plan_for_apply(plan.job_id).spec.data[0].url == signed


def test_apply_secret_sidecar_fails_closed_when_permissions_are_unsafe(tmp_path):
    signed = "https://storage.example/input.bin?signature=ISSUE18_MODE_SENTINEL"
    plan = _plan(_spec(data=[DataItem(url=signed, dest="input.bin", size_bytes=1)]))
    store = JobStore(tmp_path / "jobs")
    plan_path = store.write_plan(plan)
    store.plan_secrets_path(plan_path).chmod(0o644)

    with pytest.raises(ValueError, match="permissions"):
        store.read_plan_for_apply(plan.job_id)

def _orch(
    tmp_path, spec=None, client=None, runtime=None, session_store=None, **kw
):
    spec = spec or _spec()
    kw.setdefault("transport_factory", lambda _s: MagicMock())
    return Orchestrator(
        plan=_plan(spec),
        store=JobStore(tmp_path / "jobs"),
        client=client or MagicMock(),
        runtime_factory=lambda url, token: runtime or MagicMock(),
        session_store=session_store or MagicMock(),
        **kw,
    )



@pytest.fixture(autouse=True)
def keep_alive_spawn(monkeypatch):
    spawn = MagicMock(return_value=4242)
    monkeypatch.setattr("colab_cli.commands.session.spawn_keep_alive", spawn)
    return spawn


def _cpu_assignment(endpoint="m-s-cpu"):
    return SimpleNamespace(
        accelerator=SimpleNamespace(name="NONE"),
        endpoint=endpoint,
        runtime_proxy_info=SimpleNamespace(token="t", url="https://u"),
        variant=SimpleNamespace(name="DEFAULT"),
        machine_shape="STANDARD",
    )


class _LaunchRuntime:
    """Execute the launch cell while capturing what the runner receives."""

    def __init__(self, control_url=None):
        self.argv = None
        self.code = None
        self.env = None
        self.pass_fds = None
        self.control_url = control_url
        self.secret_file = tempfile.TemporaryFile()
        self.secret_file.write((control_url or "").encode())
        self.secret_file.flush()
        self.chmod = None
        self.unlinked = None
        self.namespace = None

    def execute_code(self, code, timeout):
        self.code = code

        def capture(argv, **kwargs):
            self.argv = argv
            self.env = dict(kwargs["env"])
            self.pass_fds = kwargs["pass_fds"]
            return SimpleNamespace(pid=4312)

        def control_exists(path):
            return bool(self.control_url and path.endswith("transfer.json"))

        real_fchmod = os.fchmod

        def capture_fchmod(fd, mode):
            self.chmod = (fd, mode)
            real_fchmod(fd, mode)

        stdout = StringIO()
        with (
            patch("os.makedirs"),
            patch("os.path.exists", side_effect=control_exists),
            patch("os.open", side_effect=lambda *_args: os.dup(self.secret_file.fileno())),
            patch("os.fchmod", side_effect=capture_fchmod),
            patch("os.unlink", side_effect=lambda path: setattr(self, "unlinked", path)),
            patch("builtins.open", mock_open()),
            patch("subprocess.Popen", side_effect=capture),
            redirect_stdout(stdout),
        ):
            self.namespace = {}
            exec(code, self.namespace)
        return [{"text": stdout.getvalue()}]


def test_launch_passes_transfer_secrets_only_by_inherited_fd(tmp_path):
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
    secret_index = runtime.argv.index("--secrets-fd") + 1
    assert int(runtime.argv[secret_index]) == runtime.pass_fds[0]
    assert runtime.argv[1:4] == ["-I", "-S", "-c"]
    cli_version_index = runtime.argv.index("--cli-version") + 1
    assert runtime.argv[cli_version_index] == orch.env.cli_version
    assert put_url not in runtime.code
    assert put_url not in runtime.argv
    assert all(put_url not in value for value in runtime.env.values())
    assert put_url not in repr(runtime.namespace)
    assert runtime.chmod[1] == 0o600
    assert runtime.unlinked.endswith("transfer.json")
    assert runtime.argv[separator + 1 :] == ["--deadline", "user"]


class _MissingTransferRuntime:
    def __init__(self):
        self.consumer_started = False

    def execute_code(self, code, timeout):
        del timeout
        stdout = StringIO()
        try:
            with (
                patch("os.path.exists", return_value=False),
                patch(
                    "subprocess.Popen",
                    side_effect=lambda *_a, **_kw: setattr(
                        self, "consumer_started", True
                    ),
                ),
                redirect_stdout(stdout),
            ):
                exec(code, {})
        except Exception as exc:
            return [{"ename": type(exc).__name__, "evalue": str(exc)}]
        return [{"text": stdout.getvalue()}]


def test_control_only_missing_transfer_map_fails_before_consumer(tmp_path):
    sentinel = "ISSUE18_MISSING_CONTROL_SENTINEL"
    spec = _spec(
        control=Control(
            result=ControlChannel(
                put_url=f"https://storage.example/result?auth={sentinel}",
                get_url="https://storage.example/result",
            )
        )
    )
    runtime = _MissingTransferRuntime()
    orch = _orch(tmp_path, spec=spec, runtime=runtime)
    orch.session_state = SimpleNamespace(url="https://vm", token="token")
    orch._secret_channel_prepared = True

    with pytest.raises(PhaseError, match="credential channel sealing failed") as error:
        orch.seal_secret_channel()

    assert error.value.phase is Phase.STAGE
    assert sentinel not in str(error.value)
    assert runtime.consumer_started is False


def test_unconsumed_secret_cleanup_fails_when_neither_path_proves_absence(
    tmp_path, monkeypatch
):
    from colab_cli.job.transport import ReadStatus

    runtime = MagicMock()
    runtime.execute_code.side_effect = RuntimeError("kernel unreachable")
    transport = MagicMock()
    transport.remove.return_value = ReadStatus.DEGRADED
    orch = _orch(tmp_path, runtime=runtime, transport_factory=lambda _s: transport)
    orch.session_state = SimpleNamespace(url="https://vm", token="token")
    orch._secret_channel_prepared = True

    assert orch.cleanup_secret_channel() is False
    transport.remove.assert_called_once()


def test_restart_passes_an_explicit_timeout(tmp_path):
    from colab_cli.job.orchestrator import RESTART_TIMEOUT

    runtime = MagicMock()
    orch = _orch(tmp_path, spec=_spec(deps=["numpy"]), runtime=runtime)
    orch.session_state = SimpleNamespace(url="https://vm", token="token")

    orch.restart()

    runtime.restart.assert_called_once_with(timeout=RESTART_TIMEOUT)


def test_restart_timeout_is_retry_same_not_session_lost(tmp_path):
    runtime = MagicMock()
    runtime.restart.side_effect = TimeoutError("stalled")
    orch = _orch(tmp_path, spec=_spec(deps=["numpy"]), runtime=runtime)
    orch.session_state = SimpleNamespace(url="https://vm", token="token")

    with pytest.raises(PhaseError) as error:
        orch.restart()

    assert error.value.phase is Phase.RESTART
    assert error.value.retry_class is RetryClass.RETRY_SAME
    assert "kernel restart did not complete" in error.value.reason





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


def test_provision_refuses_a_cpu_box_when_a_gpu_was_requested(
    tmp_path, keep_alive_spawn
):
    """The silent-substitution failure. Upstream maps unknown accelerators
    onto A100 and capacity pressure can hand back a CPU box; accepting it
    is how you publish chance-level results from a run that never had a
    GPU."""
    client = MagicMock()
    client.assign.return_value = _cpu_assignment()
    orch = _orch(tmp_path, client=client)

    with pytest.raises(PhaseError) as exc:
        orch.provision()

    assert exc.value.retry_class is RetryClass.RETRY_DIFFERENT
    client.unassign.assert_called_once_with("m-s-cpu")
    keep_alive_spawn.assert_not_called()
    orch.session_store.add.assert_not_called()


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


def test_provision_timeout_is_retryable_without_claiming_session_loss(tmp_path):
    client = MagicMock()
    client.assign.side_effect = requests.exceptions.ReadTimeout("stalled assign")
    spec = _spec(accelerator=Accelerator(prefer=["T4"], accept_cpu=False))
    orch = _orch(tmp_path, spec=spec, client=client)

    with pytest.raises(PhaseError) as exc:
        orch.provision()

    assert exc.value.retry_class is RetryClass.RETRY_DIFFERENT
    assert orch.env.endpoint is None


def test_provision_starts_keep_alive_after_persisting_the_session(
    tmp_path, keep_alive_spawn
):
    from colab_cli.auth import AuthProvider

    order = []
    client = MagicMock()
    client.assign.return_value = _cpu_assignment("m-s-job")
    session_store = MagicMock()
    session_store.add.side_effect = lambda s: order.append(
        ("persist", s.keep_alive_pid, s.last_keep_alive_ping is not None)
    )
    keep_alive_spawn.side_effect = lambda *a, **k: (
        order.append(("spawn", a, k)) or 4242
    )

    orch = _orch(
        tmp_path,
        spec=_spec(accelerator=Accelerator(prefer=[], accept_cpu=True)),
        client=client,
        session_store=session_store,
        auth_provider=AuthProvider.ADC,
        config_path="/tmp/sessions.json",
    )
    orch.provision()

    assert [step[0] for step in order] == ["persist", "spawn", "persist"]
    assert order[0][1] is None
    assert order[0][2] is True
    assert order[1][1] == ("m-s-job", "job-unit-job")
    assert order[1][2]["auth_provider"] is AuthProvider.ADC
    assert order[1][2]["config_path"] == "/tmp/sessions.json"
    assert order[2][1] == 4242
    assert orch.session_state.keep_alive_pid == 4242
    assert orch.session_state.last_keep_alive_ping is not None
    client.keep_alive_assignment.assert_called_once_with("m-s-job")


def test_provision_persists_the_endpoint_before_keep_alive(tmp_path, keep_alive_spawn):
    order = []
    client = MagicMock()
    client.assign.return_value = _cpu_assignment("m-s-job")
    orch = _orch(
        tmp_path,
        spec=_spec(accelerator=Accelerator(prefer=[], accept_cpu=True)),
        client=client,
    )
    original = orch._persist

    def persist():
        pid = getattr(orch.session_state, "keep_alive_pid", None)
        order.append(("envelope", orch.env.endpoint, pid))
        original()

    orch._persist = persist
    keep_alive_spawn.side_effect = lambda *a, **k: order.append("spawn") or 4242

    orch.provision()

    assert ("envelope", "m-s-job", None) in order
    assert order.index(("envelope", "m-s-job", None)) < order.index("spawn")
    assert orch.env.endpoint == "m-s-job"



def test_provision_scope_error_releases_the_vm_without_a_daemon(
    tmp_path, keep_alive_spawn
):
    from colab_cli.client import ColabRequestError

    client = MagicMock()
    client.assign.return_value = _cpu_assignment("m-s-job")
    response = MagicMock()
    response.status_code = 403
    client.keep_alive_assignment.side_effect = ColabRequestError(
        "Forbidden",
        MagicMock(),
        response,
        response_body='[7,"Request had insufficient authentication scopes.",'
        '[["type.googleapis.com/google.rpc.DebugInfo",[null,'
        '"gaia_mint_exchange::SCOPE_NOT_PERMITTED"]]]]',
    )
    orch = _orch(
        tmp_path,
        spec=_spec(accelerator=Accelerator(prefer=[], accept_cpu=True)),
        client=client,
    )

    with pytest.raises(PhaseError) as exc:
        orch.provision()

    assert exc.value.retry_class is RetryClass.FIX_HUMAN
    client.unassign.assert_called_once_with("m-s-job")
    keep_alive_spawn.assert_not_called()
    orch.session_store.add.assert_not_called()
    assert orch.env.endpoint is None


def test_provision_tolerates_a_non_scope_preflight_error(
    tmp_path, keep_alive_spawn
):
    from colab_cli.client import ColabRequestError

    client = MagicMock()
    client.assign.return_value = _cpu_assignment("m-s-job")
    response = MagicMock()
    response.status_code = 503
    client.keep_alive_assignment.side_effect = ColabRequestError(
        "Service Unavailable",
        MagicMock(),
        response,
        response_body="upstream timeout",
    )
    orch = _orch(
        tmp_path,
        spec=_spec(accelerator=Accelerator(prefer=[], accept_cpu=True)),
        client=client,
    )

    orch.provision()

    client.unassign.assert_not_called()
    keep_alive_spawn.assert_called_once()
    assert orch.session_state.keep_alive_pid == 4242
    assert orch.session_state.last_keep_alive_ping is None
    assert orch.session_state.keep_alive_consecutive_failures == 1


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
        orch._check_disk(1000, source_bytes=0, input_bytes=0, output_bytes=10_000)

    assert "output=10000" in exc.value.reason



# --------------------------------------------------------------------------
# Result absorption
# --------------------------------------------------------------------------

def test_remote_result_updates_terminal_provenance(tmp_path):
    orch = _orch(tmp_path)
    orch.env.schema_version = "1"

    orch._absorb_result(
        {
            "schema_version": "2",
            "workload": "succeeded",
            "exit_code": 0,
            "cli_version": "1.2.3",
            "runtime_payload_version": "sha256:remote-payload",
        }
    )

    assert orch.env.cli_version == "1.2.3"
    assert orch.env.runtime_payload_version == "sha256:remote-payload"
    assert orch.env.schema_version == "2"

@pytest.mark.parametrize(
    "result, message",
    [
        ({"schema_version": "3"}, "unsupported result schema"),
        (
            {"schema_version": "2", "cli_version": "1.2.3"},
            "invalid runtime_payload_version",
        ),
    ],
)
def test_remote_result_rejects_unidentifiable_producer(tmp_path, result, message):
    orch = _orch(tmp_path)

    with pytest.raises(ValueError, match=message):
        orch._absorb_result(result)

def test_malformed_remote_result_does_not_mutate_envelope(tmp_path):
    orch = _orch(tmp_path)
    original = orch.env.model_copy(deep=True)

    with pytest.raises(ValueError):
        orch._absorb_result(
            {
                "schema_version": "2",
                "cli_version": "1.2.3",
                "runtime_payload_version": "sha256:remote-payload",
                "phase": "stage",
                "workload": "succeeded",
                "exit_code": "bad",
            }
        )

    assert orch.env == original


def test_schema_one_envelope_without_runtime_version_remains_readable(tmp_path):
    store = JobStore(tmp_path / "jobs")
    envelope_dir = store.job_dir("legacy-job")
    envelope_dir.mkdir(parents=True)
    (envelope_dir / "envelope.json").write_text(
        json.dumps(
            {
                "schema_version": "1",
                "cli_version": "legacy-cli",
                "job_id": "legacy-job",
            }
        )
    )

    envelope = store.read_envelope("legacy-job")

    assert envelope is not None
    assert envelope.schema_version == "1"
    assert envelope.cli_version == "legacy-cli"
    assert envelope.runtime_payload_version == ""
    assert _plan(_spec()).schema_version == "1"



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
def test_cleanup_stops_keep_alive_before_release(tmp_path, monkeypatch):
    killed = []
    monkeypatch.setattr(
        "colab_cli.common.kill_process", lambda pid: killed.append(pid)
    )
    client = MagicMock()
    session_store = MagicMock()
    orch = _orch(tmp_path, client=client, session_store=session_store)
    orch.env.endpoint = "m-s-abc"
    orch.session_state = SimpleNamespace(
        name="job-unit-job", keep_alive_pid=4242
    )

    orch.cleanup()

    assert killed == [4242]
    client.unassign.assert_called_once_with("m-s-abc")
    session_store.remove.assert_called_once_with("job-unit-job")
    assert orch.env.cleanup is Cleanup.RELEASED


def test_leave_up_preserves_keep_alive(tmp_path, monkeypatch):
    killed = []
    monkeypatch.setattr(
        "colab_cli.common.kill_process", lambda pid: killed.append(pid)
    )
    client = MagicMock()
    session_store = MagicMock()
    orch = _orch(tmp_path, client=client, session_store=session_store)
    orch.env.endpoint = "m-s-abc"
    orch.session_state = SimpleNamespace(
        name="job-unit-job", keep_alive_pid=4242
    )

    orch.cleanup(force_leave_up=True)

    assert killed == []
    client.unassign.assert_not_called()
    session_store.remove.assert_not_called()
    assert orch.env.cleanup is Cleanup.LEFT_UP


def test_cleanup_stops_keep_alive_when_the_assignment_is_already_absent(
    tmp_path, monkeypatch
):
    killed = []
    monkeypatch.setattr(
        "colab_cli.common.kill_process", lambda pid: killed.append(pid)
    )
    session_store = MagicMock()
    orch = _orch(tmp_path, session_store=session_store)
    orch.session_state = SimpleNamespace(
        name="job-unit-job", keep_alive_pid=4242
    )

    orch.cleanup()

    assert killed == [4242]
    session_store.remove.assert_called_once_with("job-unit-job")
    assert orch.env.cleanup is Cleanup.ALREADY_ABSENT


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
