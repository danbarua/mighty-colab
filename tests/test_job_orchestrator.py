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
import time
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
    ArtifactResult,
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


def test_launch_passes_artifact_sync_interval_when_configured(tmp_path):
    spec = _spec(budgets=Budgets(wall_clock=600, artifact_sync_interval_seconds=60))
    runtime = _LaunchRuntime()
    orch = _orch(tmp_path, spec=spec, runtime=runtime)
    orch.session_state = SimpleNamespace(url="https://vm", token="token")

    orch.launch("/content/jobs/unit-job/mighty_runtime")

    flag_index = runtime.argv.index("--artifact-sync-interval")
    assert runtime.argv[flag_index + 1] == "60"


def test_launch_omits_artifact_sync_interval_by_default(tmp_path):
    runtime = _LaunchRuntime()
    orch = _orch(tmp_path, runtime=runtime)
    orch.session_state = SimpleNamespace(url="https://vm", token="token")

    orch.launch("/content/jobs/unit-job/mighty_runtime")

    assert "--artifact-sync-interval" not in runtime.argv



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

    # No response from assign: try the same spec again.
    assert exc.value.retry_class is RetryClass.RETRY_SAME
    assert orch.env.endpoint is None
    [attempt] = orch.env.provision_attempts
    assert attempt.error == "ReadTimeout: stalled assign"


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


def test_missing_required_artifact_names_which_one(tmp_path):
    """"a required artifact was not produced" alone forces a second round
    trip to find out which one -- the declared path is not a secret."""
    spec = _spec(
        artifacts=[
            ArtifactItem(path="/content/out/model.pt", url="https://x/m.pt"),
            ArtifactItem(path="/content/out/metrics.json", url="https://x/j"),
        ]
    )
    orch = _orch(tmp_path, spec=spec)
    orch._absorb_result(
        {
            "workload": "succeeded",
            "exit_code": 0,
            "artifacts": [
                {
                    "path": "/content/out/metrics.json",
                    "url_id": "https://x/j#abc",
                    "status": "ok",
                }
            ],
        }
    )
    assert orch.env.offload is Offload.FAILED
    assert "/content/out/model.pt" in orch.env.reason
    assert "/content/out/metrics.json" not in orch.env.reason


def test_failed_artifact_offload_names_which_one(tmp_path):
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
                    "url_id": "https://x/m.pt#abc",
                    "status": "failed",
                }
            ],
        }
    )
    assert orch.env.offload is Offload.FAILED
    assert "/content/out/model.pt" in orch.env.reason


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


def test_poll_pulls_runner_log_locally_every_healthy_tick(tmp_path):
    """As long as the VM is alive, runner.log must be pulled locally on
    every healthy poll tick -- same guarantee exec-async already gives
    (check the log at any moment, not just at the end), now true for
    `job apply`/`job apply --async` too. No new signed URL: reuses the
    same Contents connection already open for result.json/watchdog.json.
    """
    orch = _orch(tmp_path)
    transport = MagicMock()
    transport.read_json.return_value = (None, FakeStatus.NOT_FOUND)
    transport.read_text.return_value = ("step 100: loss=0.5\n", FakeStatus.OK)

    orch.poll(transport, deadline=time.time() + 0.05, interval=0)

    local_log = JobStore(tmp_path / "jobs").job_dir("unit-job") / "runner.log"
    assert local_log.read_text() == "step 100: loss=0.5\n"


def test_poll_does_not_write_runner_log_when_the_pull_fails(tmp_path):
    orch = _orch(tmp_path)
    transport = MagicMock()
    transport.read_json.return_value = (None, FakeStatus.NOT_FOUND)
    transport.read_text.return_value = (None, FakeStatus.NOT_FOUND)

    orch.poll(transport, deadline=time.time() + 0.05, interval=0)

    local_log = JobStore(tmp_path / "jobs").job_dir("unit-job") / "runner.log"
    assert not local_log.exists()




def test_poll_survives_a_broken_read_text_without_losing_the_verdict(tmp_path):
    """The log pull is best-effort auxiliary telemetry, not the verdict
    path -- a transport double (or a real transient failure) that raises
    out of read_text must never prevent the actual result from being
    absorbed."""
    orch = _orch(tmp_path)
    transport = MagicMock()
    calls = {"n": 0}

    def read_json(path):
        calls["n"] += 1
        if calls["n"] >= 2:
            return {"workload": "succeeded", "exit_code": 0}, FakeStatus.OK
        return None, FakeStatus.NOT_FOUND

    transport.read_json.side_effect = read_json
    transport.read_text.side_effect = RuntimeError("boom")

    orch.poll(transport, deadline=9e18, interval=0)

    assert orch.env.workload is Workload.SUCCEEDED
    assert orch.env.exit_code == 0



def test_cleanup_pulls_runner_log_once_more_before_releasing_the_vm(tmp_path):
    """The run's last output can land after the final poll tick that still
    saw "not done yet" -- cleanup() must pull runner.log one more time,
    through the same Contents transport poll() used, before the VM
    disappears for good."""
    client = MagicMock()
    transport = MagicMock()
    orch = _orch(tmp_path, client=client, transport_factory=lambda _s: transport)
    orch.env.endpoint = "m-s-abc"

    # poll()'s last healthy tick only sees the log as of that moment.
    transport.read_json.return_value = (None, FakeStatus.NOT_FOUND)
    transport.read_text.return_value = ("round 0: heartbeat\n", FakeStatus.OK)
    orch.poll(transport, deadline=time.time() + 0.05, interval=0)

    local_log = JobStore(tmp_path / "jobs").job_dir("unit-job") / "runner.log"
    assert local_log.read_text() == "round 0: heartbeat\n"

    # The script wrote its last line between that tick and process exit.
    transport.read_text.return_value = (
        "round 0: heartbeat\nfinished\n",
        FakeStatus.OK,
    )
    orch.cleanup()

    assert local_log.read_text() == "round 0: heartbeat\nfinished\n"
    client.unassign.assert_called_once_with("m-s-abc")

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

    orch.cleanup(leave_up=True)

    assert orch.env.cleanup is Cleanup.LEFT_UP
    client.unassign.assert_not_called()
    assert any("job destroy" in h for h in orch.env.hints)

def test_cleanup_closes_the_kernel_client_when_the_vm_is_left_up(tmp_path):
    runtime = MagicMock()
    orch = _orch(tmp_path, runtime=runtime)
    orch.session_state = SimpleNamespace(url="https://vm", token="token")
    orch._runtime_handle()
    orch.env.endpoint = "m-s-abc"

    orch.cleanup(leave_up=True)

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

    orch.cleanup(leave_up=True)

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


def test_stage_failure_surfaces_which_file_and_why(tmp_path):
    """The reason names the input, its URL identity and the response; the
    record keeps the redacted body for the rest."""
    orch = _orch(tmp_path)

    orch._absorb_result(
        {
            "workload": "failed",
            "exit_code": 1,
            "phase": "stage",
            "inputs": [
                {"dest": "inputs/a.npz", "url_id": "https://x/a.npz#111",
                 "status": "ok", "bytes": 10, "sha256": "ab" * 32},
                {"dest": "inputs/2shapes_train.npz", "url_id": "https://x/b.npz#222",
                 "status": "failed",
                 "error": {"exception": "HTTPError", "reason": "HTTP Error 403: Forbidden",
                           "http_status": 403, "body": "<Error>SignatureDoesNotMatch</Error>",
                           "category": "http"}},
            ],
        }
    )

    assert orch.env.workload is Workload.FAILED
    assert orch.env.reason == (
        "staging failed at inputs/2shapes_train.npz (https://x/b.npz#222): "
        "HTTP Error 403: Forbidden. The consumer never started."
    )
    assert orch.env.retry_class is RetryClass.REFRESH_URLS
    assert orch.env.failed_phase is Phase.STAGE
    assert [i.status for i in orch.env.inputs] == ["ok", "failed"]
    assert orch.env.inputs[1].error.body == "<Error>SignatureDoesNotMatch</Error>"


@pytest.mark.parametrize(
    "error, retry",
    [
        ({"http_status": 404, "category": "http"}, RetryClass.FIX_CODE),
        ({"http_status": 401, "category": "http"}, RetryClass.REFRESH_URLS),
        ({"http_status": 410, "category": "http"}, RetryClass.FIX_CODE),
        ({"http_status": 429, "category": "http"}, RetryClass.RETRY_SAME),
        ({"http_status": 503, "category": "http"}, RetryClass.RETRY_SAME),
        ({"category": "network"}, RetryClass.RETRY_SAME),
        ({"category": "size"}, RetryClass.FIX_CODE),
        ({"category": "checksum"}, RetryClass.FIX_CODE),
        ({"category": "local"}, RetryClass.FIX_CODE),
        ({"category": "blocked"}, RetryClass.FIX_HUMAN),
        ({"category": "setup"}, RetryClass.DO_NOT_RETRY),
        ({"category": "error"}, RetryClass.RETRY_SAME),
    ],
)
def test_stage_failure_retry_class_follows_the_transfer_table(tmp_path, error, retry):
    orch = _orch(tmp_path)

    orch._absorb_result(
        {
            "workload": "failed",
            "exit_code": 1,
            "phase": "stage",
            "inputs": [
                {"dest": "in.bin", "url_id": "https://x/in#1", "status": "failed",
                 "error": {"exception": "E", "reason": "r", **error}},
            ],
        }
    )

    assert orch.env.retry_class is retry


def test_stage_failure_before_any_input_is_a_supervisor_fault(tmp_path):
    """A credential channel or manifest the runner cannot use is a bug in
    mighty-colab; the runner's message says which."""
    orch = _orch(tmp_path)

    orch._absorb_result(
        {
            "workload": "failed",
            "exit_code": 1,
            "phase": "stage",
            "inputs": [],
            "exception": {
                "type": "ManifestError",
                "message": "ManifestError: required transfer credential channel is missing",
                "traceback": "",
            },
        }
    )

    assert orch.env.workload is Workload.FAILED
    assert orch.env.reason == (
        "staging failed before any input was fetched: ManifestError: required "
        "transfer credential channel is missing. The consumer never started."
    )
    assert orch.env.retry_class is RetryClass.DO_NOT_RETRY


# --------------------------------------------------------------------------
# Evidence survives release
# --------------------------------------------------------------------------


def test_absorbed_artifact_failure_keeps_its_cause(tmp_path):
    spec = _spec(
        artifacts=[ArtifactItem(path="/content/out/adapter.tar", url="https://x/a.tar")]
    )
    orch = _orch(tmp_path, spec=spec)
    error = {
        "exception": "HTTPStatusError",
        "reason": "HTTP 413 Payload Too Large (upload cut short: BrokenPipeError)",
        "http_status": 413,
        "body": "<html>413</html>",
        "category": "http",
    }
    orch._absorb_result(
        {
            "workload": "succeeded",
            "exit_code": 0,
            "artifacts": [
                {
                    "path": "/content/out/adapter.tar",
                    "url_id": "https://x/a.tar#abc123",
                    "status": "failed",
                    "bytes": 173199360,
                    "error": error,
                }
            ],
        }
    )
    orch._persist()

    assert orch.env.offload is Offload.FAILED
    assert orch.env.retry_class is RetryClass.FIX_CODE
    stored = JobStore(tmp_path / "jobs").read_envelope("unit-job")
    assert stored.artifacts[0].error.model_dump() == error


class _VMFiles:
    """Contents transport double: serves `files` by remote path and records
    every read in `events`."""

    def __init__(self, files, events, statuses=None):
        self.files = files
        self.events = events
        self.statuses = statuses or {}

    def read_text(self, path):
        self.events.append(("read", path))
        name = path.rsplit("/", 1)[-1]
        if name in self.statuses:
            return None, self.statuses[name]
        if path in self.files:
            return self.files[path], FakeStatus.OK
        return None, FakeStatus.NOT_FOUND


def test_copy_vm_records_copies_what_exists_and_names_it(tmp_path):
    from colab_cli.job.orchestrator import copy_vm_records

    remote = "/content/jobs/unit-job"
    events = []
    transport = _VMFiles(
        {
            f"{remote}/runner.log": "step 10/10\n",
            f"{remote}/result.json": '{"workload": "failed"}',
            f"{remote}/install.log": "Collecting torch\n",
        },
        events,
    )
    store = JobStore(tmp_path / "jobs")

    hint = copy_vm_records(transport, store, "unit-job")

    local = store.job_dir("unit-job")
    assert (local / "runner.log").read_text() == "step 10/10\n"
    assert (local / "result.json").read_text() == '{"workload": "failed"}'
    assert (local / "install.log").read_text() == "Collecting torch\n"
    assert not (local / "watchdog.json").exists()
    assert str(local) in hint
    for name in ("runner.log", "result.json", "install.log"):
        assert name in hint
    assert "watchdog.json" not in hint
    assert all(".secrets" not in path for _kind, path in events)


def test_copy_vm_records_stops_when_the_session_is_lost(tmp_path):
    from colab_cli.job.orchestrator import copy_vm_records

    events = []
    transport = _VMFiles({}, events, statuses={"runner.log": FakeStatus.SESSION_LOST})

    hint = copy_vm_records(transport, JobStore(tmp_path / "jobs"), "unit-job")

    assert len(events) == 1
    assert "session_lost" in hint


def test_copy_vm_records_never_raises(tmp_path):
    from colab_cli.job.orchestrator import copy_vm_records

    transport = MagicMock()
    transport.read_text.side_effect = RuntimeError("contents down")

    hint = copy_vm_records(transport, JobStore(tmp_path / "jobs"), "unit-job")

    assert "RuntimeError" in hint


def test_cleanup_copies_vm_records_before_releasing_the_vm(tmp_path):
    events = []
    remote = "/content/jobs/unit-job"
    transport = _VMFiles(
        {
            f"{remote}/runner.log": "Traceback ...\n",
            f"{remote}/install.log": "pip output\n",
            f"{remote}/watchdog.json": "{}",
        },
        events,
    )
    client = MagicMock()
    client.unassign.side_effect = lambda endpoint: events.append(("unassign", endpoint))
    orch = _orch(tmp_path, client=client, transport_factory=lambda _s: transport)
    orch.env.endpoint = "m-s-abc"
    orch.env.workload = Workload.FAILED

    orch.cleanup()

    assert events[-1] == ("unassign", "m-s-abc")
    assert ("read", f"{remote}/install.log") in events
    local = JobStore(tmp_path / "jobs").job_dir("unit-job")
    assert (local / "install.log").read_text() == "pip output\n"
    assert any("install.log" in hint and str(local) in hint for hint in orch.env.hints)


def test_cleanup_does_not_copy_records_when_the_vm_is_left_up(tmp_path):
    events = []
    transport = _VMFiles({}, events)
    orch = _orch(tmp_path, transport_factory=lambda _s: transport)
    orch.env.endpoint = "m-s-abc"

    orch.cleanup(leave_up=True)

    assert events == []


def test_copy_vm_records_keeps_the_redacted_error_message(tmp_path):
    from colab_cli.job.orchestrator import copy_vm_records

    transport = MagicMock()
    transport.read_text.side_effect = requests.ConnectionError(
        "HTTPSConnectionPool(host='colab.example', port=443): Max retries exceeded "
        "with url: /api/contents/content/jobs/unit-job/runner.log"
        "?colab-runtime-proxy-token=SECRET"
    )

    hint = copy_vm_records(transport, JobStore(tmp_path / "jobs"), "unit-job")

    assert "ConnectionError" in hint
    assert "Max retries exceeded" in hint
    assert "SECRET" not in hint


def test_cleanup_keeps_why_the_transport_could_not_be_built(tmp_path):
    def no_transport(_session):
        raise RuntimeError("no proxy for https://colab.example/x?token=SECRET")

    orch = _orch(tmp_path, transport_factory=no_transport)
    orch.env.endpoint = "m-s-abc"

    orch.cleanup()

    hint = next(h for h in orch.env.hints if "not copied" in h)
    assert "RuntimeError: no proxy for https://colab.example/x?<redacted>" in hint
    assert "SECRET" not in hint


def test_offload_reason_names_each_failed_artifacts_cause(tmp_path):
    spec = _spec(
        artifacts=[
            ArtifactItem(path="/content/out/adapter.tar", url="https://x/a.tar"),
            ArtifactItem(path="/content/out/meta.json", url="https://x/m.json"),
        ]
    )
    orch = _orch(tmp_path, spec=spec)
    orch._absorb_result(
        {
            "workload": "succeeded",
            "exit_code": 0,
            "artifacts": [
                {
                    "path": "/content/out/adapter.tar",
                    "url_id": "https://x/a.tar#1",
                    "status": "failed",
                    "error": {
                        "exception": "HTTPStatusError",
                        "reason": "HTTP 413 Payload Too Large",
                        "http_status": 413,
                        "body": "too large",
                    },
                },
                {
                    "path": "/content/out/meta.json",
                    "url_id": "https://x/m.json#2",
                    "status": "failed",
                    "error": {
                        "exception": "ConnectionResetError",
                        "reason": "[Errno 104] Connection reset by peer",
                    },
                },
            ],
        }
    )

    assert "/content/out/adapter.tar (HTTP 413 Payload Too Large)" in orch.env.reason
    assert (
        "/content/out/meta.json (ConnectionResetError: [Errno 104] Connection reset by peer)"
        in orch.env.reason
    )


# --------------------------------------------------------------------------
# Outcomes: no VM is left billing without a handle or a reason
# --------------------------------------------------------------------------


def _colab_error(status, body=None, content_type="application/json"):
    from colab_cli.client import ColabRequestError

    response = MagicMock()
    response.status_code = status
    response.headers = {"Content-Type": content_type}
    return ColabRequestError(
        f"Failed to issue request POST https://colab.example/tun/m/unassign/m-s-abc"
        f"?authuser=0&token=SECRET: HTTP {status}",
        MagicMock(),
        response,
        response_body=body,
    )


def test_release_assignment_outcomes():
    from colab_cli.job.orchestrator import release_assignment

    client = MagicMock()
    assert release_assignment(client, "m-s-abc") == (Cleanup.RELEASED, None)

    client.unassign.side_effect = _colab_error(404)
    client.list_assignments.return_value = []
    assert release_assignment(client, "m-s-abc") == (Cleanup.ALREADY_ABSENT, None)

    client.unassign.side_effect = _colab_error(500, '{"error": "backend unavailable"}')
    cleanup, detail = release_assignment(client, "m-s-abc")
    assert cleanup is Cleanup.FAILED
    assert "HTTP 500" in detail
    assert "ColabRequestError" in detail
    assert "backend unavailable" in detail
    assert "SECRET" not in detail


def test_cleanup_records_a_404_unassign_as_already_absent(tmp_path):
    client = MagicMock()
    client.unassign.side_effect = _colab_error(404)
    orch = _orch(tmp_path, client=client)
    orch.env.endpoint = "m-s-abc"

    orch.cleanup()

    assert orch.env.cleanup is Cleanup.ALREADY_ABSENT


def test_cleanup_failure_keeps_the_http_status_and_body(tmp_path):
    client = MagicMock()
    client.unassign.side_effect = _colab_error(500, '{"error": "backend unavailable"}')
    orch = _orch(tmp_path, client=client)
    orch.env.endpoint = "m-s-abc"

    orch.cleanup()

    assert orch.env.cleanup is Cleanup.FAILED
    hint = next(h for h in orch.env.hints if "teardown failed" in h)
    assert "HTTP 500" in hint
    assert "backend unavailable" in hint
    assert "m-s-abc" in hint


def test_keep_alive_scope_error_keeps_the_endpoint_when_unassign_fails(
    tmp_path, keep_alive_spawn
):
    """Clearing the endpoint after a failed unassign leaves a VM billing
    with no handle: cleanup then reports it already absent."""
    from colab_cli.client import ColabRequestError

    client = MagicMock()
    client.assign.return_value = _cpu_assignment("m-s-job")
    response = MagicMock()
    response.status_code = 403
    client.keep_alive_assignment.side_effect = ColabRequestError(
        "Forbidden",
        MagicMock(),
        response,
        response_body='[7,"Request had insufficient authentication scopes."]',
    )
    client.unassign.side_effect = _colab_error(500, '{"error": "backend unavailable"}')
    orch = _orch(
        tmp_path,
        spec=_spec(accelerator=Accelerator(prefer=[], accept_cpu=True)),
        client=client,
    )

    with pytest.raises(PhaseError):
        orch.provision()

    assert orch.env.endpoint == "m-s-job"
    assert any("HTTP 500" in h and "m-s-job" in h for h in orch.env.hints)


def test_provision_stops_when_a_refused_cpu_vm_cannot_be_released(tmp_path):
    """Moving on to the next accelerator would overwrite the endpoint of a
    CPU VM that is still assigned."""
    client = MagicMock()
    client.assign.return_value = _cpu_assignment("m-s-cpu")
    client.unassign.side_effect = _colab_error(500, '{"error": "backend unavailable"}')
    orch = _orch(
        tmp_path,
        spec=_spec(accelerator=Accelerator(prefer=["T4", "L4"], accept_cpu=False)),
        client=client,
    )

    with pytest.raises(PhaseError) as exc:
        orch.provision()

    assert client.assign.call_count == 1
    assert orch.env.endpoint == "m-s-cpu"
    assert "HTTP 500" in exc.value.reason


def _stage_failure_result():
    return {
        "workload": "failed",
        "exit_code": 1,
        "phase": "stage",
        "artifacts": [],
        "offload": "pending",
        "inputs": [
            {"dest": "inputs/x.npy", "url_id": "https://x/x.npy#abc", "status": "failed",
             "error": {"exception": "HTTPError", "reason": "HTTP Error 403: Forbidden",
                       "http_status": 403, "category": "http"}},
        ],
        "exception": {
            "type": "StageItemError",
            "message": "inputs/x.npy: HTTP Error 403: Forbidden",
            "traceback": "",
        },
        "runner_error": "inputs/x.npy: HTTP Error 403: Forbidden",
    }


def test_stage_failure_skips_offload_and_releases_the_vm(tmp_path):
    """The consumer never ran, so nothing was offloaded. Recording that as
    an offload failure left the VM up under the default leave_up policy."""
    client = MagicMock()
    spec = _spec(
        artifacts=[ArtifactItem(path="/content/out/model.pt", url="https://x/m.pt")]
    )
    orch = _orch(tmp_path, spec=spec, client=client)
    orch.env.endpoint = "m-s-abc"

    orch._absorb_result(_stage_failure_result())
    orch.cleanup()

    assert orch.env.offload is Offload.SKIPPED
    assert "staging failed at inputs/x.npy (https://x/x.npy#abc): HTTP Error 403" in orch.env.reason
    assert "required artifact" not in orch.env.reason
    client.unassign.assert_called_once_with("m-s-abc")
    assert orch.env.cleanup is Cleanup.RELEASED


def test_stage_failure_without_artifacts_is_not_required(tmp_path):
    orch = _orch(tmp_path)
    orch._absorb_result(_stage_failure_result())
    assert orch.env.offload is Offload.NOT_REQUIRED


def _poll_transport(files):
    """read_json double: `files` maps a remote file name to a list of
    responses, consumed in order; the last one repeats."""

    def read_json(path):
        name = path.rsplit("/", 1)[-1]
        queue = files.get(name)
        if not queue:
            return None, FakeStatus.NOT_FOUND
        value = queue.pop(0) if len(queue) > 1 else queue[0]
        return (value, FakeStatus.OK) if value is not None else (None, FakeStatus.NOT_FOUND)

    transport = MagicMock()
    transport.read_json.side_effect = read_json
    transport.read_text.return_value = (None, FakeStatus.NOT_FOUND)
    return transport


def test_poll_classifies_a_dead_runner_without_waiting_for_the_deadline(tmp_path):
    orch = _orch(tmp_path)
    transport = _poll_transport(
        {
            "launch.json": [{"pid": 7, "starttime": "1", "boot_id": "b"}],
            "watchdog.json": [{"runner_alive": False, "elapsed": 95, "remaining": 505}],
        }
    )

    orch.poll(transport, deadline=time.time() + 2, interval=0)

    assert orch.env.workload is Workload.UNKNOWN
    assert orch.env.supervisor is Supervisor.FINISHED
    assert orch.env.retry_class is RetryClass.RETRY_SAME
    assert "runner is dead" in orch.env.reason
    assert "95s" in orch.env.reason
    assert orch.env.offload is Offload.NOT_REQUIRED
    assert orch.env.finished_at is not None


def test_poll_does_not_end_the_job_when_liveness_is_unknown(tmp_path):
    """The watchdog reports runner_alive null when launch.json gives no
    usable identity; that is not a dead runner."""
    orch = _orch(tmp_path)
    transport = _poll_transport(
        {
            "watchdog.json": [
                {"runner_alive": None, "elapsed": 95, "remaining": 505,
                 "runner_identity_error": "launch.json unreadable: JSONDecodeError: x",
                 "gpu": None, "gpu_error": "nvidia-smi exited 9: Unknown Error"}
            ],
        }
    )

    orch.poll(transport, deadline=time.time() + 1, interval=0)

    assert orch.env.workload is not Workload.UNKNOWN
    assert orch.env.supervisor is Supervisor.INTERRUPTED
    assert orch.env.hints == [
        "watchdog: t=95s remaining=505s gpu=none (nvidia-smi exited 9: Unknown Error) "
        "disk_free=None runner_alive=None (launch.json unreadable: JSONDecodeError: x)"
    ]


def test_poll_rereads_the_result_before_declaring_the_runner_dead(tmp_path):
    """The runner writes result.json, then exits; the watchdog can report
    it dead between the poll's two reads."""
    orch = _orch(tmp_path)
    transport = _poll_transport(
        {
            "result.json": [None, {"workload": "succeeded", "exit_code": 0}],
            "launch.json": [{"pid": 7, "starttime": "1", "boot_id": "b"}],
            "watchdog.json": [{"runner_alive": False, "elapsed": 95}],
        }
    )

    orch.poll(transport, deadline=time.time() + 2, interval=0)

    assert orch.env.workload is Workload.SUCCEEDED


def test_poll_classifies_a_runner_that_never_started(tmp_path, monkeypatch):
    import datetime as dt

    from colab_cli.job import orchestrator as orchestrator_module

    monkeypatch.setattr(orchestrator_module, "LAUNCH_ABSENCE_CONFIRM_SECONDS", 0.0)
    orch = _orch(tmp_path)
    orch.env.started_at = (
        dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=600)
    ).isoformat()
    transport = _poll_transport({})

    orch.poll(transport, deadline=time.time() + 2, interval=0)

    assert orch.env.workload is Workload.UNKNOWN
    assert orch.env.supervisor is Supervisor.FINISHED
    assert "never started" in orch.env.reason
    assert "runner.log" in orch.env.reason


def test_poll_waits_for_a_runner_that_was_just_launched(tmp_path):
    import datetime as dt

    orch = _orch(tmp_path)
    orch.env.started_at = dt.datetime.now(dt.timezone.utc).isoformat()
    transport = _poll_transport({})

    orch.poll(transport, deadline=time.time() + 0.2, interval=0)

    assert orch.env.supervisor is Supervisor.INTERRUPTED



def test_poll_finishes_offload_when_the_assignment_is_gone(tmp_path):
    spec = _spec(
        artifacts=[ArtifactItem(path="/content/out/model.pt", url="https://x/m.pt")]
    )
    orch = _orch(tmp_path, spec=spec)
    transport = MagicMock()
    transport.read_json.return_value = (None, FakeStatus.SESSION_LOST)

    orch.poll(transport, deadline=time.time() + 2, interval=0)

    assert orch.env.workload is Workload.UNKNOWN
    assert orch.env.offload is Offload.SKIPPED



def test_a_404_unassign_counts_as_absent_only_when_the_listing_agrees():
    """`jobs prune` deletes already_absent records. A 404 that does not
    mean the VM is gone must not delete the only handle to it."""
    from colab_cli.job.orchestrator import release_assignment

    client = MagicMock()
    client.unassign.side_effect = _colab_error(404)

    client.list_assignments.return_value = [SimpleNamespace(endpoint="m-s-abc")]
    cleanup, detail = release_assignment(client, "m-s-abc")
    assert cleanup is Cleanup.FAILED
    assert "still listed" in detail
    assert "HTTP 404" in detail

    client.list_assignments.side_effect = RuntimeError("listing unavailable")
    cleanup, detail = release_assignment(client, "m-s-abc")
    assert cleanup is Cleanup.FAILED
    assert "listing unavailable" in detail
    assert "HTTP 404" in detail


def test_poll_never_declares_never_started_once_the_runner_was_seen(tmp_path, monkeypatch):
    """Near the proxy-token boundary a read of an existing file can return
    NOT_FOUND. Once the runner's records have been read, later absence is
    a transport question, not a verdict."""
    import datetime as dt

    from colab_cli.job import orchestrator as orchestrator_module

    monkeypatch.setattr(orchestrator_module, "LAUNCH_ABSENCE_CONFIRM_SECONDS", 0.0)
    orch = _orch(tmp_path)
    orch.env.started_at = (
        dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=3600)
    ).isoformat()
    transport = _poll_transport(
        {"watchdog.json": [{"runner_alive": True, "elapsed": 3590}, None]}
    )

    orch.poll(transport, deadline=time.time() + 1, interval=0)

    assert orch.env.workload is not Workload.UNKNOWN
    assert orch.env.supervisor is Supervisor.INTERRUPTED


def test_poll_needs_launch_json_absent_across_a_token_refresh_window(tmp_path):
    """One poll's NOT_FOUND can be an expired token the transport has not
    refreshed yet; absence must persist past its 404 refresh interval."""
    import datetime as dt

    orch = _orch(tmp_path)
    orch.env.started_at = (
        dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=600)
    ).isoformat()
    transport = _poll_transport({})

    orch.poll(transport, deadline=time.time() + 1, interval=0)

    assert orch.env.supervisor is Supervisor.INTERRUPTED


# --------------------------------------------------------------------------
# Kernel calls fail per phase, with the cause and the right retry class
# --------------------------------------------------------------------------


def test_outputs_text_keeps_the_error_name_and_value_and_strips_ansi():
    from colab_cli.job.orchestrator import _outputs_text

    text = _outputs_text(
        [
            {"output_type": "stream", "text": "\x1b[1mcollecting\x1b[0m\n"},
            {
                "output_type": "error",
                "ename": "ModuleNotFoundError",
                "evalue": "No module named 'torch'",
                "traceback": ["\x1b[0;31mTraceback (most recent call last)\x1b[0m"],
            },
            {"output_type": "error", "ename": "RuntimeError", "evalue": "boom", "traceback": []},
        ]
    )

    assert "\x1b" not in text
    assert "collecting" in text
    assert "ModuleNotFoundError: No module named 'torch'" in text
    assert "Traceback (most recent call last)" in text
    assert "RuntimeError: boom" in text


def _install_orch(tmp_path, side_effect):
    rt = MagicMock()
    rt.execute_code.side_effect = side_effect
    orch = _orch(tmp_path, spec=_spec(deps=["torch==2.4.1"]), runtime=rt)
    orch.session_state = SimpleNamespace(url="https://u", token="t")
    return orch


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("Connection was lost."),
        RuntimeError("You must first start a kernel before requesting a client."),
        TimeoutError("Timeout waiting for reply"),
        ConnectionResetError(104, "Connection reset by peer"),
    ],
)
def test_a_lost_kernel_connection_during_install_is_retry_same(tmp_path, error):
    """Run 3: the websocket dropped 41 s into install and the job was
    reported as an internal supervisor failure with do_not_retry."""
    orch = _install_orch(tmp_path, error)

    with pytest.raises(PhaseError) as exc:
        orch.install()

    assert exc.value.phase is Phase.INSTALL
    assert exc.value.retry_class is RetryClass.RETRY_SAME
    assert type(error).__name__ in exc.value.reason
    assert str(error) in exc.value.reason
    assert any("install.log" in h for h in exc.value.hints)


def test_an_unexpected_kernel_call_error_keeps_its_type_and_message(tmp_path):
    orch = _orch(tmp_path, runtime=MagicMock(**{"execute_code.side_effect": KeyError("ename")}))
    orch.session_state = SimpleNamespace(url="https://u", token="t")
    orch.env.actual_accelerator = "NONE"

    with pytest.raises(PhaseError) as exc:
        orch.verify()

    assert exc.value.phase is Phase.VERIFY
    assert exc.value.retry_class is RetryClass.DO_NOT_RETRY
    assert "KeyError: 'ename'" in exc.value.reason


def test_a_lost_launch_reply_leaves_the_job_to_poll(tmp_path):
    """The runner is detached: once the launch code ran, the kernel
    connection no longer matters. A lost reply must not release a VM whose
    runner may be running; poll decides from the runner's own files."""
    rt = MagicMock()
    rt.execute_code.side_effect = RuntimeError("Connection was lost.")
    orch = _orch(tmp_path, runtime=rt)
    orch.session_state = SimpleNamespace(url="https://u", token="t")

    pid = orch.launch("/content/jobs/unit-job/src")

    assert pid is None
    assert orch.env.workload is Workload.RUNNING
    assert orch.env.started_at is not None
    assert any("launch" in h and "Connection was lost" in h for h in orch.env.hints)


def test_restart_failure_names_its_error_instead_of_claiming_a_timeout(tmp_path):
    rt = MagicMock()
    response = MagicMock()
    response.status_code = 403
    error = requests.HTTPError("403 Client Error: Forbidden", response=response)
    rt.restart.side_effect = error
    orch = _orch(tmp_path, spec=_spec(deps=["torch==2.4.1"]), runtime=rt)
    orch.session_state = SimpleNamespace(url="https://u", token="t")

    with pytest.raises(PhaseError) as exc:
        orch.restart()

    assert "did not complete within" not in exc.value.reason
    assert "HTTPError: 403 Client Error: Forbidden" in exc.value.reason


def test_poll_keeps_a_result_it_cannot_absorb(tmp_path):
    orch = _orch(tmp_path)
    transport = _poll_transport(
        {"result.json": [{"schema_version": "99", "workload": "succeeded", "exit_code": 0}]}
    )

    orch.poll(transport, deadline=time.time() + 2, interval=0)

    assert orch.env.workload is Workload.UNKNOWN
    assert orch.env.supervisor is Supervisor.FINISHED
    assert "could not be absorbed" in orch.env.reason
    assert "unsupported result schema" in orch.env.reason
    assert "workload='succeeded'" in orch.env.reason


def test_a_keep_alive_preflight_network_error_is_recorded_and_tolerated(
    tmp_path, keep_alive_spawn
):
    client = MagicMock()
    client.assign.return_value = _cpu_assignment("m-s-job")
    client.keep_alive_assignment.side_effect = requests.ConnectionError(
        "HTTPSConnectionPool(host='colab.research.google.com'): Max retries exceeded"
    )
    orch = _orch(
        tmp_path,
        spec=_spec(accelerator=Accelerator(prefer=[], accept_cpu=True)),
        client=client,
    )

    orch.provision()

    keep_alive_spawn.assert_called_once()
    assert any(
        "keep-alive pre-flight failed" in h and "ConnectionError" in h
        for h in orch.env.hints
    )


def test_a_keep_alive_daemon_that_cannot_start_fails_provisioning(
    tmp_path, keep_alive_spawn
):
    """Without the daemon Colab reclaims the idle VM mid-run, with no record
    of why. Failing at provision says so while the cause is known."""
    client = MagicMock()
    client.assign.return_value = _cpu_assignment("m-s-job")
    keep_alive_spawn.side_effect = OSError(24, "Too many open files")
    orch = _orch(
        tmp_path,
        spec=_spec(accelerator=Accelerator(prefer=[], accept_cpu=True)),
        client=client,
    )

    with pytest.raises(PhaseError) as exc:
        orch.provision()

    assert exc.value.phase is Phase.PROVISION
    assert exc.value.retry_class is RetryClass.FIX_HUMAN
    assert "Too many open files" in exc.value.reason
    assert orch.env.endpoint == "m-s-job"


_INTERRUPTED = [
    {
        "output_type": "error",
        "ename": "KeyboardInterrupt",
        "evalue": "",
        "traceback": ["/usr/lib/python3.13/subprocess.py in _wait(self, timeout)", "KeyboardInterrupt: "],
    }
]


def test_an_interrupted_install_cell_is_retry_same(tmp_path):
    """Shutting down or restarting a busy kernel interrupts the running
    cell first; the call then returns a KeyboardInterrupt error output
    instead of raising. Seen live when the install kernel was shut down."""
    orch = _install_orch(tmp_path, None)
    orch._runtime_handle().execute_code.side_effect = None
    orch._runtime_handle().execute_code.return_value = _INTERRUPTED

    with pytest.raises(PhaseError) as exc:
        orch.install()

    assert exc.value.retry_class is RetryClass.RETRY_SAME
    assert exc.value.reason.startswith("kernel interrupted during install")
    assert "subprocess.py" not in exc.value.reason
    assert any("install.log" in h for h in exc.value.hints)


def test_an_interrupted_launch_cell_leaves_the_job_to_poll(tmp_path):
    rt = MagicMock()
    rt.execute_code.return_value = _INTERRUPTED
    orch = _orch(tmp_path, runtime=rt)
    orch.session_state = SimpleNamespace(url="https://u", token="t")

    assert orch.launch("/content/jobs/unit-job/src") is None
    assert orch.env.workload is Workload.RUNNING


# --------------------------------------------------------------------------
# failed_phase: which phase's failure decided the outcome
# --------------------------------------------------------------------------


def test_record_failure_keeps_the_first_phase():
    from colab_cli.job.models import JobEnvelope

    env = JobEnvelope(job_id="x")
    env.record_failure(Phase.RUN)
    env.record_failure(Phase.CLEANUP)
    assert env.failed_phase is Phase.RUN


@pytest.mark.parametrize(
    "result,artifacts,expected",
    [
        (_stage_failure_result(), True, Phase.STAGE),
        ({"workload": "failed", "exit_code": 1}, False, Phase.RUN),
        ({"workload": "unknown", "runner_error": "escapee detection unavailable"}, False, Phase.RUN),
        ({"workload": "cancelled", "signal": 15}, False, None),
        ({"workload": "succeeded", "exit_code": 0}, False, None),
        (
            {
                "workload": "succeeded",
                "exit_code": 0,
                "artifacts": [{"path": "/content/out/model.pt", "url_id": "https://x/m.pt#1", "status": "failed"}],
            },
            True,
            Phase.OFFLOAD,
        ),
        (
            {
                "workload": "failed",
                "exit_code": 1,
                "artifacts": [{"path": "/content/out/model.pt", "url_id": "https://x/m.pt#1", "status": "failed"}],
            },
            True,
            Phase.RUN,
        ),
    ],
)
def test_absorbed_result_records_the_failed_phase(tmp_path, result, artifacts, expected):
    spec = _spec(
        artifacts=[ArtifactItem(path="/content/out/model.pt", url="https://x/m.pt", required=False)]
    ) if artifacts else _spec()
    orch = _orch(tmp_path, spec=spec)

    orch._absorb_result(result)

    assert orch.env.failed_phase is expected


def test_poll_without_a_result_records_run_as_the_failed_phase(tmp_path):
    orch = _orch(tmp_path)
    transport = _poll_transport(
        {
            "launch.json": [{"pid": 7, "starttime": "1", "boot_id": "b"}],
            "watchdog.json": [{"runner_alive": False, "elapsed": 95}],
        }
    )

    orch.poll(transport, deadline=time.time() + 2, interval=0)

    assert orch.env.failed_phase is Phase.RUN


def test_a_failed_release_after_success_records_cleanup(tmp_path):
    client = MagicMock()
    client.unassign.side_effect = _colab_error(500, '{"error": "backend unavailable"}')
    orch = _orch(tmp_path, client=client)
    orch.env.endpoint = "m-s-abc"
    orch.env.workload = Workload.SUCCEEDED

    orch.cleanup()

    assert orch.env.phase is Phase.CLEANUP
    assert orch.env.failed_phase is Phase.CLEANUP


def test_a_failed_release_after_a_failed_run_keeps_run(tmp_path):
    client = MagicMock()
    client.unassign.side_effect = _colab_error(500, '{"error": "backend unavailable"}')
    orch = _orch(tmp_path, client=client)
    orch.env.endpoint = "m-s-abc"
    orch._absorb_result({"workload": "failed", "exit_code": 1})

    orch.cleanup()

    assert orch.env.failed_phase is Phase.RUN


# --------------------------------------------------------------------------
# apply --timeout: cancel the runner and release, never leave the VM billing
# --------------------------------------------------------------------------


def _cancel_transport(results):
    """result.json answers from `results` in order (last repeats); the
    runner identity and watchdog say it is alive. Records cancel writes."""
    transport = _poll_transport(
        {
            "result.json": list(results),
            "launch.json": [{"pid": 7, "starttime": "1", "boot_id": "b"}],
            "watchdog.json": [{"runner_alive": True, "elapsed": 1000}],
        }
    )
    transport.written = []
    transport.write_json.side_effect = lambda path, value: transport.written.append((path, value)) or FakeStatus.OK
    return transport


def test_a_passed_deadline_cancels_the_runner_and_keeps_its_result(tmp_path, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _s: None)
    orch = _orch(tmp_path)
    orch.env.supervisor = Supervisor.INTERRUPTED
    transport = _cancel_transport(
        [
            None,
            {
                "workload": "cancelled",
                "signal": 15,
                "signal_name": "SIGTERM",
                "cancel_intent": {"intent": "cancelled", "by": "job apply --timeout"},
            },
        ]
    )

    orch.cancel_after_deadline(
        transport, "apply's --timeout of 900s passed before a verdict", "job apply --timeout"
    )

    path, intent = transport.written[0]
    assert path.endswith("/cancel.json") and intent["by"] == "job apply --timeout"
    assert orch.env.workload is Workload.CANCELLED
    assert orch.env.supervisor is Supervisor.FINISHED
    assert orch.env.failed_phase is Phase.RUN
    # The result shows the cancel arrived, so "cancel requested" is not
    # repeated beside it.
    assert orch.env.reason == (
        "apply's --timeout of 900s passed before a verdict; cancelled by job "
        "apply --timeout; the workload was stopped by SIGTERM (15)"
    )
    assert orch.env.retry_class is RetryClass.RETRY_SAME


def test_a_passed_deadline_with_a_result_that_finished_first_keeps_the_cancel_note(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("time.sleep", lambda _s: None)
    orch = _orch(tmp_path)
    orch.env.supervisor = Supervisor.INTERRUPTED
    transport = _cancel_transport([None, {"workload": "succeeded", "exit_code": 0}])

    orch.cancel_after_deadline(
        transport, "apply's --timeout of 900s passed before a verdict", "job apply --timeout"
    )

    assert orch.env.reason == (
        "apply's --timeout of 900s passed before a verdict; cancel requested"
    )
    assert orch.env.retry_class is RetryClass.RETRY_SAME


def test_a_passed_deadline_without_a_result_still_ends_the_job(tmp_path, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _s: None)
    orch = _orch(tmp_path)
    orch.env.supervisor = Supervisor.INTERRUPTED
    transport = _cancel_transport([None])

    orch.cancel_after_deadline(
        transport, "apply's --timeout of 900s passed before a verdict",
        "job apply --timeout", wait=10,
    )

    assert orch.env.workload is Workload.UNKNOWN
    assert orch.env.supervisor is Supervisor.FINISHED
    assert orch.env.offload.terminal
    assert "--timeout of 900s" in orch.env.reason
    assert "no result.json within 10s" in orch.env.reason


def test_an_absorbed_result_replaces_earlier_local_reasons(tmp_path):
    """A reason written while the verdict was unknown (an interruption, a
    degraded transport) is stale once the runner's result arrives."""
    orch = _orch(tmp_path)
    orch.env.reason = "interrupted locally after the runner was launched"
    orch.env.retry_class = RetryClass.RETRY_SAME

    orch._absorb_result({"workload": "succeeded", "exit_code": 0})

    assert orch.env.reason is None
    assert orch.env.retry_class is None


def test_detach_closes_the_local_kernel_client(tmp_path):
    """Its websocket threads are not daemons: left open, they keep the
    interrupted apply process from exiting."""
    rt = MagicMock()
    orch = _orch(tmp_path, runtime=rt)
    orch.session_state = SimpleNamespace(url="https://u", token="t")
    orch._runtime_handle()

    orch.detach()

    rt.stop.assert_called_once()


def test_verify_rechecks_url_expiry_once_install_has_run(tmp_path):
    """Apply's preflight allows for the longest install; after install, a
    URL must still cover staging, the run and offload."""
    import datetime as dt

    expires = int((dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5)).timestamp())
    spec = _spec(
        data=[DataItem(url=f"https://storage.example/in?Expires={expires}&Signature=S",
                       dest="in.bin", size_bytes=1)]
    )
    orch = _orch(tmp_path, spec=spec)

    with pytest.raises(PhaseError) as caught:
        orch._check_url_expiry()

    assert caught.value.phase is Phase.VERIFY
    assert caught.value.retry_class is RetryClass.REFRESH_URLS
    assert "signed URLs no longer last until the end of the run: data[0].url" in caught.value.reason
    assert "for installing deps" not in caught.value.reason


def test_verify_accepts_urls_that_outlast_the_run(tmp_path):
    import datetime as dt

    expires = int((dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=2)).timestamp())
    spec = _spec(
        data=[DataItem(url=f"https://storage.example/in?Expires={expires}&Signature=S",
                       dest="in.bin", size_bytes=1)]
    )
    _orch(tmp_path, spec=spec)._check_url_expiry()



# -- provision attempts ------------------------------------------------------


def _assign_error(status, reason, body="", content_type="text/html"):
    """The real shape of a failed assign (seen 2026-10-04 for a CPU VM:
    `Failed to issue request POST .../tun/m/assign?nbh=...: Service
    Unavailable`)."""
    from colab_cli.client import ColabRequestError

    response = MagicMock()
    response.status_code = status
    response.headers = {"Content-Type": content_type}
    return ColabRequestError(
        "Failed to issue request POST https://colab.research.google.com/tun/m/assign"
        f"?nbh=e92e31f1_8d0d&variant=DEFAULT&accelerator=NONE&authuser=0: {reason}",
        request=MagicMock(),
        response=response,
        response_body=body,
    )


@pytest.mark.parametrize(
    "status, retry",
    [
        (400, RetryClass.FIX_HUMAN),
        (401, RetryClass.FIX_HUMAN),
        (403, RetryClass.FIX_HUMAN),
        (408, RetryClass.RETRY_SAME),
        (429, RetryClass.RETRY_SAME),
        (503, RetryClass.RETRY_DIFFERENT),
        (409, RetryClass.RETRY_DIFFERENT),
    ],
)
def test_a_failed_assign_is_classified_by_status(tmp_path, status, retry):
    client = MagicMock()
    client.assign.side_effect = _assign_error(status, "Reason")
    orch = _orch(tmp_path, spec=_spec(accelerator=Accelerator(prefer=[], accept_cpu=True)),
                 client=client)

    with pytest.raises(PhaseError) as caught:
        orch.provision()

    assert caught.value.retry_class is retry
    [attempt] = orch.env.provision_attempts
    assert attempt.http_status == status
    assert attempt.retry_class is retry


def test_a_failed_assign_keeps_no_signed_or_identifying_query(tmp_path):
    client = MagicMock()
    client.assign.side_effect = _assign_error(503, "Service Unavailable", body="<html>503</html>")
    orch = _orch(tmp_path, spec=_spec(accelerator=Accelerator(prefer=[], accept_cpu=True)),
                 client=client)

    with pytest.raises(PhaseError) as caught:
        orch.provision()

    [attempt] = orch.env.provision_attempts
    assert attempt.error == (
        "ColabRequestError: Failed to issue request POST "
        "https://colab.research.google.com/tun/m/assign?<redacted> Service Unavailable"
    )
    assert attempt.body == "[text/html body of 16 characters not kept]"
    assert "nbh=" not in caught.value.reason
    assert "NONE: ColabRequestError" in caught.value.reason


def test_a_json_assign_body_is_kept(tmp_path):
    client = MagicMock()
    client.assign.side_effect = _assign_error(
        400, "Bad Request", body='{"error": {"message": "quota"}}', content_type="application/json"
    )
    orch = _orch(tmp_path, spec=_spec(accelerator=Accelerator(prefer=["A100"])), client=client)

    with pytest.raises(PhaseError):
        orch.provision()

    assert orch.env.provision_attempts[0].body == '{"error": {"message": "quota"}}'


def test_mixed_candidates_take_the_strongest_class(tmp_path):
    client = MagicMock()
    client.assign.side_effect = [_assign_error(503, "Service Unavailable"),
                                 _assign_error(400, "Bad Request")]
    orch = _orch(tmp_path, spec=_spec(accelerator=Accelerator(prefer=["T4", "L4"])), client=client)

    with pytest.raises(PhaseError) as caught:
        orch.provision()

    assert caught.value.retry_class is RetryClass.FIX_HUMAN
    assert [a.accelerator for a in orch.env.provision_attempts] == ["T4", "L4"]


def test_a_parse_failure_in_the_client_is_do_not_retry(tmp_path):
    client = MagicMock()
    client.assign.side_effect = ValueError("unexpected assign response")
    orch = _orch(tmp_path, spec=_spec(accelerator=Accelerator(prefer=["T4"])), client=client)

    with pytest.raises(PhaseError) as caught:
        orch.provision()

    assert caught.value.retry_class is RetryClass.DO_NOT_RETRY


def test_earlier_failures_are_kept_when_a_later_candidate_is_granted(tmp_path, keep_alive_spawn):
    client = MagicMock()
    client.assign.side_effect = [_assign_error(503, "Service Unavailable"),
                                 _cpu_assignment("m-s-cpu")]
    orch = _orch(tmp_path, spec=_spec(accelerator=Accelerator(prefer=["T4"], accept_cpu=True)),
                 client=client)

    orch.provision()

    assert [(a.accelerator, a.outcome) for a in orch.env.provision_attempts] == [
        ("T4", "failed"), ("NONE", "granted"),
    ]


def test_a_first_try_grant_records_no_attempts(tmp_path, keep_alive_spawn):
    client = MagicMock()
    client.assign.return_value = _cpu_assignment("m-s-cpu")
    orch = _orch(tmp_path, spec=_spec(accelerator=Accelerator(prefer=[], accept_cpu=True)),
                 client=client)

    orch.provision()

    assert orch.env.provision_attempts == []
    assert "provision_attempts" not in orch.env.model_dump(mode="json")


def test_the_assignment_limit_keeps_its_body(tmp_path):
    from colab_cli.client import TooManyAssignmentsError

    cause = _assign_error(412, "Precondition Failed", body='{"limit": 2}',
                          content_type="application/json")
    client = MagicMock()
    client.assign.side_effect = TooManyAssignmentsError(
        str(cause), response=cause.response, response_body=cause.response_body
    )
    orch = _orch(tmp_path, spec=_spec(accelerator=Accelerator(prefer=["T4"])), client=client)

    with pytest.raises(PhaseError) as caught:
        orch.provision()

    assert caught.value.retry_class is RetryClass.FIX_HUMAN
    assert 'response body: {"limit": 2}' in caught.value.reason
    assert "nbh=" not in caught.value.reason


def test_a_long_json_assign_body_says_how_much_was_cut(tmp_path):
    client = MagicMock()
    client.assign.side_effect = _assign_error(
        400, "Bad Request", body='{"m": "' + "x" * 500 + '"}', content_type="application/json"
    )
    orch = _orch(tmp_path, spec=_spec(accelerator=Accelerator(prefer=["A100"])), client=client)

    with pytest.raises(PhaseError):
        orch.provision()

    assert orch.env.provision_attempts[0].body.endswith(" [... 209 characters omitted]")


# -- leave_up keeps the VM only for a failed upload --------------------------


def _artifact_orch(tmp_path, client, on_offload_fail="leave_up"):
    spec = _spec(
        artifacts=[ArtifactItem(path="/content/out/model.pt", url="https://x/m.pt")],
        on_offload_fail=on_offload_fail,
    )
    orch = _orch(tmp_path, spec=spec, client=client)
    orch.env.endpoint = "m-s-abc"
    return orch


def test_a_crash_before_writing_a_required_artifact_releases_the_vm(tmp_path):
    """Nothing was produced, so nothing on the VM needs rescuing; its
    records are copied off before release."""
    client = MagicMock()
    orch = _artifact_orch(tmp_path, client)
    orch._absorb_result(
        {
            "workload": "failed",
            "exit_code": 1,
            "exception": {"type": "ValueError", "message": "bad", "traceback": ""},
            "artifacts": [{"path": "/content/out/model.pt", "url_id": "https://x/m.pt#1",
                           "status": "missing"}],
        }
    )

    assert orch.env.offload is Offload.FAILED
    assert orch.env.retry_class is RetryClass.FIX_CODE
    from colab_cli.job.orchestrator import keep_vm

    assert keep_vm(orch.env, orch.spec, secret_removed=True) is False
    orch.cleanup(leave_up=False)

    client.unassign.assert_called_once_with("m-s-abc")
    assert orch.env.cleanup is Cleanup.RELEASED


def test_a_failed_upload_keeps_the_vm_under_leave_up(tmp_path):
    orch = _artifact_orch(tmp_path, MagicMock())
    orch._absorb_result(
        {
            "workload": "succeeded",
            "exit_code": 0,
            "artifacts": [{"path": "/content/out/model.pt", "url_id": "https://x/m.pt#1",
                           "status": "failed",
                           "error": {"exception": "HTTPStatusError", "reason": "HTTP 413",
                                     "http_status": 413, "category": "http"}}],
        }
    )

    from colab_cli.job.orchestrator import keep_vm

    assert keep_vm(orch.env, orch.spec, secret_removed=True) is True
    # An unconfirmed credential deletion releases the VM whatever the policy.
    assert keep_vm(orch.env, orch.spec, secret_removed=False) is False


def test_on_offload_fail_destroy_never_keeps_the_vm(tmp_path):
    orch = _artifact_orch(tmp_path, MagicMock(), on_offload_fail="destroy")
    orch.env.artifacts = [ArtifactResult(path="/content/out/model.pt", url_id="u", status="failed")]

    from colab_cli.job.orchestrator import keep_vm

    assert keep_vm(orch.env, orch.spec, secret_removed=True) is False


def test_cleanup_releases_when_told_to_even_after_a_failed_upload(tmp_path):
    """The caller decides: apply overrides leave_up when the credential
    file's deletion was not confirmed, and cleanup must not keep the VM
    on its own."""
    client = MagicMock()
    orch = _artifact_orch(tmp_path, client)
    orch.env.offload = Offload.FAILED
    orch.env.artifacts = [ArtifactResult(path="/content/out/model.pt", url_id="u", status="failed")]

    orch.cleanup(leave_up=False)

    client.unassign.assert_called_once_with("m-s-abc")
    assert orch.env.cleanup is Cleanup.RELEASED


# -- a watchdog that stops writing ---------------------------------------------


def test_a_watchdog_that_stops_writing_is_reported_as_stalled(tmp_path, monkeypatch):
    clock = iter(range(0, 10**6, 100))
    monkeypatch.setattr("colab_cli.job.orchestrator.time.monotonic", lambda: float(next(clock)))
    orch = _orch(tmp_path)
    orch.env.hints = ["verified device=None source=1 input=0 output=0 free=9"]
    transport = _poll_transport(
        {"watchdog.json": [{"runner_alive": True, "ts": 5.0, "elapsed": 30, "remaining": 30}]}
    )

    orch.poll(transport, deadline=time.time() + 0.3, interval=0)

    stalled = [h for h in orch.env.hints if h.startswith("watchdog stalled: ")]
    assert len(stalled) == 1
    assert "watchdog.json has not changed for" in stalled[0]
    assert "(ts=5.0)" in stalled[0]
    # Earlier hints survive the poll; the telemetry line is replaced in place.
    assert orch.env.hints[0].startswith("verified device=")
    assert sum(h.startswith("watchdog: ") for h in orch.env.hints) == 1


def test_a_changing_watchdog_is_not_stalled():
    from colab_cli.job.orchestrator import WatchdogStaleness

    ticks = iter([0.0, 400.0, 800.0])
    tracker = WatchdogStaleness(clock=lambda: next(ticks))
    assert tracker.observe({"ts": 1.0}) is None
    assert tracker.observe({"ts": 31.0}) is None
    assert tracker.observe({"ts": 61.0}) is None



def test_the_leave_up_flag_keeps_the_vm_and_an_unknown_plan_counts_only_it(tmp_path):
    from colab_cli.job.orchestrator import keep_vm

    orch = _artifact_orch(tmp_path, MagicMock())
    orch.env.artifacts = [ArtifactResult(path="/content/out/model.pt", url_id="u", status="failed")]

    assert keep_vm(orch.env, None, secret_removed=True) is False
    orch.env.leave_up = True
    assert keep_vm(orch.env, None, secret_removed=True) is True
    assert keep_vm(orch.env, orch.spec, secret_removed=False) is False
    assert orch.env.model_dump(mode="json")["leave_up"] is True
    orch.env.leave_up = False
    assert "leave_up" not in orch.env.model_dump(mode="json")


def test_an_unconfirmed_secret_cleanup_says_why(tmp_path):
    from colab_cli.job.transport import ReadStatus

    orch = _orch(tmp_path)
    orch._secret_channel_prepared = True
    orch.session_state = SimpleNamespace(url="https://u", token="t", name="s", endpoint="e")
    orch._execute_code = MagicMock(side_effect=RuntimeError("Connection was lost."))
    transport = MagicMock()
    transport.remove.return_value = ReadStatus.DEGRADED
    orch._job_transport = transport

    assert orch.cleanup_secret_channel() is False
    assert orch.secret_cleanup_problem == (
        "the kernel call failed (RuntimeError: Connection was lost.); "
        "Contents removal returned DEGRADED"
    )


def test_a_lost_vm_with_a_control_result_is_absorbed_not_unknown(tmp_path, monkeypatch):
    spec = _spec(
        control=Control(result=ControlChannel(put_url="https://x/r?sig=a", get_url="https://x/r?sig=b"))
    )
    orch = _orch(tmp_path, spec=spec)
    monkeypatch.setattr(
        "colab_cli.job.orchestrator.fetch_control_result",
        lambda url: {"workload": "succeeded", "exit_code": 0},
    )
    transport = _poll_transport({})

    orch._finish_without_result("the assignment is gone from the server", transport)

    assert orch.env.workload is Workload.SUCCEEDED
    assert "result read from control.result after: the assignment is gone from the server" in orch.env.hints


def test_an_unreadable_control_result_says_why_without_the_signature(tmp_path, monkeypatch):
    import urllib.error

    get = "https://x/r?X-Goog-Signature=SENTINEL"
    spec = _spec(control=Control(result=ControlChannel(put_url="https://x/r?sig=a", get_url=get)))
    orch = _orch(tmp_path, spec=spec)

    def forbidden(url):
        raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)

    monkeypatch.setattr("colab_cli.job.orchestrator.fetch_control_result", forbidden)

    orch._finish_without_result("the runner is dead", _poll_transport({}))

    assert orch.env.workload is Workload.UNKNOWN
    [hint] = [h for h in orch.env.hints if h.startswith("control.result could not be read")]
    assert "HTTP Error 403: Forbidden" in hint
    assert "SENTINEL" not in hint


def test_an_unknown_remote_phase_is_a_hint(tmp_path):
    orch = _orch(tmp_path)
    orch._absorb_result({"workload": "succeeded", "exit_code": 0, "phase": "teleport"})

    assert any("the runner reported phase 'teleport'" in h for h in orch.env.hints)
