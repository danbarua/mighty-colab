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

"""The `apply` state machine: provision -> install -> restart -> verify ->
stage -> run -> offload -> cleanup.

Two rules shape almost every decision in here.

**Teardown is not a phase you reach, it is how you leave.** Every exit path
runs cleanup, including the ones that failed early, because the failure mode
that actually costs money is an A100 left assigned by a crash at
`verify`. Cleanup records its own outcome in its own field and never
overwrites the workload's verdict.

**Stage, run and offload live behind one kernel RPC.** They are not three
kernel calls; they are three arguments to a single detached runner. A
websocket drop mid-download or mid-upload would otherwise lose the run,
which is the exact wound this command exists to close.
"""

import datetime
import json
import time
import uuid
from typing import Callable, List, Optional, Tuple


from colab_cli.auto_update import get_app_version
from colab_cli.job import RESULT_SCHEMA_VERSION, SCHEMA_VERSION
from colab_cli.job.models import (
    ArtifactResult,
    Cleanup,
    JobEnvelope,
    JobSpec,
    Offload,
    Phase,
    Plan,
    RetryClass,
    Supervisor,
    Workload,
)
from colab_cli.job.store import JobStore
from colab_cli.job.runtime_payload import RUNTIME_PAYLOAD_VERSION

# Remote layout. Everything the job owns lives under one directory so
# `destroy` has exactly one thing to remove and `plan` has exactly one
# prefix to validate `dest` paths against.
REMOTE_ROOT = "/content/jobs"

# The launch RPC must not inherit the 10s default: `execute_code`'s timeout
# is a wall-clock budget, and a cold import of the runtime package on a
# freshly restarted kernel can exceed it. It is still finite -- the call
# only spawns a process and returns a pid, so anything approaching this
# ceiling means the kernel itself is wedged.
LAUNCH_TIMEOUT = 120.0
INSTALL_TIMEOUT = 1800.0
VERIFY_TIMEOUT = 180.0
RESTART_TIMEOUT = 60.0


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class PhaseError(Exception):
    """A phase failed in a way that stops the apply.

    Carries the classification the *next agent turn* needs, not just a
    message: a human-readable string is not actionable by a caller deciding
    whether to retry the same VM, ask for a different one, or stop.
    """

    def __init__(
        self,
        phase: Phase,
        reason: str,
        retry_class: RetryClass,
        hints: Optional[List[str]] = None,
    ):
        super().__init__(reason)
        self.phase = phase
        self.reason = reason
        self.retry_class = retry_class
        self.hints = hints or []


class Orchestrator:
    """Drives one attempt of one job.

    Deliberately takes its collaborators as arguments rather than reaching
    for the `state` singleton: every phase here is worth testing without a
    VM, and a hidden global makes that a mocking exercise instead of a
    constructor argument.
    """

    def __init__(
        self,
        plan: Plan,
        store: JobStore,
        client,
        runtime_factory: Callable[..., object],
        transport_factory: Callable[..., object],
        session_store,
        emit: Optional[Callable[[str], None]] = None,
        auth_provider=None,
        config_path: Optional[str] = None,
    ):
        self.plan = plan
        self.spec: JobSpec = plan.spec
        self.job_id = plan.job_id
        self.store = store
        self.client = client
        self.runtime_factory = runtime_factory
        self.transport_factory = transport_factory
        self.session_store = session_store
        self.emit = emit or (lambda _m: None)
        self.auth_provider = auth_provider
        self.config_path = config_path

        self.env = JobEnvelope(
            cli_version=get_app_version(),
            runtime_payload_version=RUNTIME_PAYLOAD_VERSION,
            job_id=self.job_id,
            phase=Phase.PLAN,
        )
        self.session_state = None
        self._runtime = None
        result_channel = getattr(getattr(self.spec, "control", None), "result", None)
        self._secrets_required = bool(
            self.spec.data
            or self.spec.artifacts
            or getattr(result_channel, "put_url", None)
        )
        self._job_transport = None

        self._secret_channel_prepared = False

    # -- envelope bookkeeping -------------------------------------------

    def _persist(self) -> None:
        self.store.write_envelope(self.env)

    def _set_phase(self, phase: Phase) -> None:
        self.env.phase = phase
        self.store.append_event(self.job_id, {"ts": _now(), "phase": phase.value})
        self._persist()
        self.emit(f"[job] phase={phase.value}")

    @property
    def remote_dir(self) -> str:
        return f"{REMOTE_ROOT}/{self.job_id}"

    def job_transport(self):
        """One refreshable Contents transport for stage, poll, cancel, and recovery."""
        if self._job_transport is None:
            self._job_transport = self.transport_factory(self.session_state)
        return self._job_transport


    # -- provision -------------------------------------------------------

    def provision(self) -> None:
        """Walk `accelerator.prefer` in order; never substitute silently.

        Upstream `new` maps an unrecognised accelerator onto A100, so the
        difference between "I asked for T4 and got one" and "I asked for
        nonsense and got a bill" is invisible at the call site. The plan
        rejects unknown names; this records what was actually granted so a
        mismatch is visible in the envelope rather than in the results.
        """
        from colab_cli.client import TooManyAssignmentsError
        from colab_cli.commands.session import resolve_runtime_options
        from colab_cli.state import SessionState

        self._set_phase(Phase.PROVISION)
        session_name = f"job-{self.job_id}"
        attempts: List[Tuple[str, str]] = []

        candidates = list(self.spec.accelerator.prefer)
        if self.spec.accelerator.accept_cpu:
            candidates.append("NONE")

        last_exc: Optional[Exception] = None
        for want in candidates:
            self.env.requested_accelerator = want
            try:
                is_tpu = want.upper().startswith("V5") or want.upper().startswith("V6")
                variant, accelerator, shape = resolve_runtime_options(
                    gpu=None if (is_tpu or want == "NONE") else want,
                    tpu=want if is_tpu else None,
                    high_mem=False,
                )
                res = self.client.assign(
                    uuid.uuid4(), variant=variant, accelerator=accelerator, shape=shape
                )
            except TooManyAssignmentsError as e:
                # Not a capacity problem and not fixable by asking for a
                # different accelerator -- the account is already at its
                # limit, and retrying just burns the retry budget.
                raise PhaseError(
                    Phase.PROVISION,
                    f"account is at its concurrent-assignment limit: {e}",
                    RetryClass.FIX_HUMAN,
                    ["run `mighty-colab sessions` and stop what you are not using"],
                ) from e
            except Exception as e:  # noqa: BLE001 - classified below
                last_exc = e
                attempts.append((want, str(e)[:200]))
                self.emit(f"[job] {want} unavailable, trying next preference")
                continue

            granted = getattr(res.accelerator, "name", str(res.accelerator))
            self.env.actual_accelerator = granted
            self.env.endpoint = res.endpoint

            # A GPU request satisfied by a CPU box is the silent failure that
            # publishes chance-level science. Refuse it unless asked.
            if granted in ("NONE", "UNRECOGNIZED") and want != "NONE":
                self.client.unassign(res.endpoint)
                self.env.endpoint = None
                attempts.append((want, f"granted {granted}"))
                continue

            proxy = res.runtime_proxy_info
            self.session_state = SessionState(
                name=session_name,
                token=proxy.token,
                url=proxy.url,
                endpoint=res.endpoint,
                variant=res.variant.name,
                accelerator=granted,
                machine_shape=getattr(res, "machine_shape", "STANDARD"),
            )
            # Endpoint must be durable before keep-alive or any later
            # fallible step: a crash here is recoverable from envelope.json.
            self.env.session = session_name
            self._persist()
            self._start_keep_alive()
            self.emit(f"[job] provisioned {res.endpoint} accel={granted}")
            return

        detail = "; ".join(f"{a}: {r}" for a, r in attempts) or str(last_exc)
        raise PhaseError(
            Phase.PROVISION,
            f"no acceptable accelerator from {candidates}: {detail}",
            RetryClass.RETRY_DIFFERENT,
            [
                "Colab capacity varies by hour; a different accelerator in "
                "`accelerator.prefer` is usually available sooner than the "
                "same one later.",
                "Set `accelerator.accept_cpu: true` only if the workload is "
                "genuinely useful without a GPU.",
            ],
        )

    def _start_keep_alive(self) -> None:
        """Own the TFE daemon for this assignment.

        Persist the session first so the detached child cannot observe an
        empty store and exit with `session_not_found`.
        """
        from colab_cli.client import ColabRequestError
        from colab_cli.commands.session import _is_scope_error, spawn_keep_alive
        from colab_cli.utils import get_status_code

        session = self.session_state
        try:
            self.client.keep_alive_assignment(session.endpoint)
        except ColabRequestError as exc:
            if get_status_code(exc) == 403 and _is_scope_error(exc):
                try:
                    self.client.unassign(session.endpoint)
                except Exception:  # noqa: BLE001
                    pass
                self.env.endpoint = None
                self.env.session = None
                self.session_state = None
                raise PhaseError(
                    Phase.PROVISION,
                    "keep-alive pre-flight failed: credentials are missing "
                    "an OAuth scope required by Colab",
                    RetryClass.FIX_HUMAN,
                    [
                        "re-authenticate with the colaboratory and "
                        "userinfo.email scopes"
                    ],
                ) from exc
        else:
            session.last_keep_alive_ping = _now()

        self.session_store.add(session)
        session.keep_alive_pid = spawn_keep_alive(
            session.endpoint,
            session.name,
            auth_provider=self.auth_provider,
            config_path=self.config_path,
        )
        self.session_store.add(session)

    def _stop_keep_alive(self) -> None:
        stop_session_keep_alive(self.session_state)


    # -- install / restart / verify --------------------------------------

    def _runtime_handle(self):
        if self._runtime is None:
            self._runtime = self.runtime_factory(
                self.session_state.url, self.session_state.token
            )
        return self._runtime

    def _sync_runtime_identity(self) -> None:
        if self.session_state is None or self._runtime is None:
            return
        changed = False
        for field in ("kernel_id", "session_id"):
            value = getattr(self._runtime, field, None)
            if value and getattr(self.session_state, field, None) != value:
                setattr(self.session_state, field, value)
                changed = True
        if changed:
            self.session_store.add(self.session_state)

    def _execute_code(self, code: str, *, timeout: float):
        runtime = self._runtime_handle()
        try:
            return runtime.execute_code(code, timeout=timeout)
        finally:
            self._sync_runtime_identity()

    def _close_runtime(self) -> None:
        runtime, self._runtime = self._runtime, None
        if runtime is None:
            return
        try:
            runtime.stop()
        except Exception as e:  # noqa: BLE001 - local cleanup must not hide the verdict
            self.env.hints.append(
                f"local runtime client close failed ({type(e).__name__})"
            )

    def install(self) -> None:
        if not self.spec.deps:
            return
        self._set_phase(Phase.INSTALL)
        pkgs = " ".join(repr(d) for d in self.spec.deps)
        code = (
            "import subprocess, sys\n"
            f"pkgs = [{pkgs}]\n"
            "r = subprocess.run([sys.executable, '-m', 'pip', 'install', "
            "'--upgrade-strategy', 'only-if-needed', *pkgs],"
            " capture_output=True, text=True)\n"
            "print(r.stdout[-4000:])\n"
            "print(r.stderr[-4000:])\n"
            "print('PIP_RC=%d' % r.returncode)\n"
        )
        outputs = self._execute_code(code, timeout=INSTALL_TIMEOUT)
        text = _outputs_text(outputs)
        if "PIP_RC=0" not in text:
            raise PhaseError(
                Phase.INSTALL,
                f"pip install failed: {_tail(text)}",
                RetryClass.FIX_CODE,
                ["check the version pins in `deps` against what Colab preinstalls"],
            )

    def restart(self) -> None:
        """Unconditional after install, and only after install.

        A new version of a package that was already imported at kernel boot
        is not live until the interpreter restarts -- this is the wound
        `reinstall` was created to close. Restarting while `run` is in
        flight is out of scope: the runner is a detached process and does
        not care, but the verify evidence would no longer describe it.
        """
        if not self.spec.deps:
            return
        self._set_phase(Phase.RESTART)
        runtime = self._runtime_handle()
        try:
            runtime.restart(timeout=RESTART_TIMEOUT)
        except Exception as error:
            raise PhaseError(
                Phase.RESTART,
                f"kernel restart did not complete within {RESTART_TIMEOUT:.0f}s ({error})",
                RetryClass.RETRY_SAME,
            ) from error
        finally:
            self._sync_runtime_identity()


    def verify(self) -> None:
        """A gate, not a hope.

        Re-probed *after* the restart and against `sys.modules`, because the
        only question that matters is what the workload will actually
        import -- not what pip reported it had installed a minute ago.
        """
        self._set_phase(Phase.VERIFY)
        want_gpu = self.env.actual_accelerator not in (None, "NONE", "UNRECOGNIZED")
        code = (
            "import importlib.metadata as md, json, sys\n"
            f"names = {json.dumps([_dep_name(d) for d in self.spec.deps])}\n"
            "got = {}\n"
            "for n in names:\n"
            "    try: got[n] = md.version(n)\n"
            "    except Exception as e: got[n] = 'MISSING: %s' % type(e).__name__\n"
            "dev = None\n"
            "try:\n"
            "    import torch\n"
            "    dev = torch.cuda.get_device_name(0) if torch.cuda.is_available() else None\n"
            "except Exception:\n"
            "    try:\n"
            "        import subprocess\n"
            "        dev = subprocess.run(['nvidia-smi','--query-gpu=name',"
            "'--format=csv,noheader'], capture_output=True, text=True).stdout.strip() or None\n"
            "    except Exception:\n"
            "        dev = None\n"
            "import shutil\n"
            "free = shutil.disk_usage('/content').free\n"
            "print('VERIFY=' + json.dumps({'deps': got, 'device': dev, 'free': free}))\n"
        )
        outputs = self._execute_code(code, timeout=VERIFY_TIMEOUT)
        payload = _extract_tagged(_outputs_text(outputs), "VERIFY=")
        if payload is None:
            raise PhaseError(
                Phase.VERIFY,
                "verification probe produced no parseable output",
                RetryClass.RETRY_SAME,
            )
        missing = [n for n, v in payload["deps"].items() if str(v).startswith("MISSING")]
        if missing:
            raise PhaseError(
                Phase.VERIFY,
                f"declared dependencies not importable after restart: {missing}",
                RetryClass.FIX_CODE,
                ["the distribution name in `deps` may differ from the import name"],
            )
        if want_gpu and not payload.get("device"):
            raise PhaseError(
                Phase.VERIFY,
                f"requested {self.env.actual_accelerator} but no GPU is visible "
                "to the runtime after restart",
                RetryClass.RETRY_DIFFERENT,
                ["the VM was assigned a GPU that the driver cannot see; a fresh "
                 "assignment usually clears it"],
            )
        self.env.hints.append(
            f"verified device={payload.get('device')} free={payload.get('free')}"
        )
        self._check_disk(payload.get("free"))
        self._persist()

    def _check_disk(self, free: Optional[int]) -> None:
        """Refuse a job whose declared inputs and artifacts cannot fit."""
        if not free:
            return
        declared = sum(d.size_bytes or 0 for d in self.spec.data)
        declared += sum(a.size_bytes or 0 for a in self.spec.artifacts)
        if declared and declared > free * 0.8:
            raise PhaseError(
                Phase.VERIFY,
                f"declared inputs and artifacts are {declared} bytes but only "
                f"{free} free on /content",
                RetryClass.RETRY_DIFFERENT,
                ["request a high-RAM/larger-disk shape, or reduce staged data/output"],
            )

    def prepare_secret_channel(self) -> None:
        """Create the credential directory without transmitting credentials."""
        secret_dir = f"{self.remote_dir}/mighty_runtime/.secrets"
        code = (
            "import os\n"
            f"p = {secret_dir!r}\n"
            "os.makedirs(p, mode=0o700, exist_ok=True)\n"
            "os.chmod(p, 0o700)\n"
            "print('SECRET_CHANNEL_READY=1')\n"
        )
        outputs = self._execute_code(code, timeout=LAUNCH_TIMEOUT)
        if "SECRET_CHANNEL_READY=1" not in _outputs_text(outputs):
            raise PhaseError(
                Phase.STAGE,
                "credential channel preparation failed",
                RetryClass.RETRY_SAME,
            )
        self._secret_channel_prepared = True

    def seal_secret_channel(self) -> None:
        """Set uploaded credentials owner-only before launch can consume them."""
        secret_path = f"{self.remote_dir}/mighty_runtime/.secrets/transfer.json"
        code = (
            "import os, stat\n"
            f"p = {secret_path!r}\n"
            f"required = {self._secrets_required!r}\n"
            "if required and not os.path.exists(p):\n"
            "    raise RuntimeError('required transfer credential file is missing')\n"
            "if os.path.exists(p):\n"
            "    fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW)\n"
            "    try:\n"
            "        s = os.fstat(fd)\n"
            "        if not stat.S_ISREG(s.st_mode) or s.st_uid != os.getuid():\n"
            "            raise RuntimeError('unsafe transfer credential file')\n"
            "        os.fchmod(fd, 0o600)\n"
            "    finally:\n"
            "        os.close(fd)\n"
            "print('SECRET_CHANNEL_SEALED=1')\n"
        )
        outputs = self._execute_code(code, timeout=LAUNCH_TIMEOUT)
        if "SECRET_CHANNEL_SEALED=1" not in _outputs_text(outputs):
            raise PhaseError(
                Phase.STAGE,
                "credential channel sealing failed",
                RetryClass.RETRY_SAME,
            )

    def cleanup_secret_channel(self) -> bool:
        """Remove an unconsumed credential file and prove it is absent."""
        if not self._secret_channel_prepared:
            return True
        if self.session_state is None:
            return False
        secret_path = f"{self.remote_dir}/mighty_runtime/.secrets/transfer.json"
        secret_dir = secret_path.rsplit("/", 1)[0]
        kernel_absent = False
        code = (
            "import os\n"
            f"p = {secret_path!r}\n"
            "try:\n"
            "    os.unlink(p)\n"
            "except FileNotFoundError:\n"
            "    pass\n"
            f"try:\n    os.rmdir({secret_dir!r})\nexcept OSError:\n    pass\n"
            "print('SECRET_CHANNEL_ABSENT=%d' % (not os.path.lexists(p)))\n"
        )
        try:
            outputs = self._execute_code(code, timeout=LAUNCH_TIMEOUT)
            kernel_absent = "SECRET_CHANNEL_ABSENT=1" in _outputs_text(outputs)
        except Exception:  # noqa: BLE001 - use the independent Contents path
            pass

        contents_absent = False
        try:
            from colab_cli.job.transport import ReadStatus

            status = self.job_transport().remove(secret_path)
            contents_absent = status in (ReadStatus.OK, ReadStatus.SESSION_LOST)
        except Exception:  # noqa: BLE001 - kernel proof may still be available
            pass


        removed = kernel_absent or contents_absent
        if removed:
            self._secret_channel_prepared = False
        return removed

    def launch(self, payload_remote_path: str) -> int:
        """Start the runner after unlinking its owner-only credential file."""
        del payload_remote_path
        self._set_phase(Phase.RUN)
        args = json.dumps(self.spec.code.args)
        code = (
            "import os, stat, subprocess, sys\n"
            "def _launch_job():\n"
            f"    d = {self.remote_dir!r}\n"
            "    os.makedirs(d, exist_ok=True)\n"
            "    secret_fd = None\n"
            "    log = None\n"
            "    try:\n"
            "        env = dict(os.environ)\n"
            "        for name in ('MIGHTY_CONTROL_RESULT_PUT_URL', "
            "'MIGHTY_RESULT_PUT_URL', 'CONTROL_RESULT_PUT_URL'): "
            "env.pop(name, None)\n"
            f"        env['MIGHTY_JOB_ID'] = {self.job_id!r}\n"
            '        bootstrap = ("import runpy,sys;sys.path.insert(0,%r);" '
            '% d + "runpy.run_module(\'mighty_runtime.runner\',run_name=\'__main__\')")\n'
            "        cmd = [sys.executable, '-I', '-S', '-c', bootstrap,\n"
            "               '--job-dir', d,\n"
            f"              '--deadline', str({self.spec.budgets.wall_clock}),\n"
            f"              '--cli-version', {self.env.cli_version!r},\n"
            f"              '--entry', os.path.join(d, 'src', {self.spec.code.entry!r})]\n"
            "        secret_path = os.path.join(d, 'mighty_runtime', '.secrets', 'transfer.json')\n"
            f"        secrets_required = {self._secrets_required!r}\n"
            "        pass_fds = ()\n"
            "        if secrets_required and not os.path.exists(secret_path):\n"
            "            raise RuntimeError('required transfer credential file is missing')\n"
            "        if os.path.exists(secret_path):\n"
            "            secret_fd = os.open(secret_path, os.O_RDONLY | os.O_NOFOLLOW)\n"
            "            secret_stat = os.fstat(secret_fd)\n"
            "            if not stat.S_ISREG(secret_stat.st_mode) or secret_stat.st_uid != os.getuid():\n"
            "                raise RuntimeError('unsafe transfer credential file')\n"
            "            os.fchmod(secret_fd, 0o600)\n"
            "            os.unlink(secret_path)\n"
            "            cmd += ['--secrets-fd', str(secret_fd)]\n"
            "            pass_fds = (secret_fd,)\n"
            "        if secrets_required:\n"
            "            cmd += ['--secrets-required']\n"
            "        if os.path.exists(os.path.join(d, 'stage.manifest.json')):\n"
            "            cmd += ['--stage-manifest', os.path.join(d, 'stage.manifest.json')]\n"
            "        if os.path.exists(os.path.join(d, 'offload.manifest.json')):\n"
            "            cmd += ['--offload-manifest', os.path.join(d, 'offload.manifest.json')]\n"
            f"        cmd += ['--'] + {args}\n"
            "        log = open(os.path.join(d, 'runner.log'), 'ab')\n"
            "        p = subprocess.Popen(cmd, cwd=d, env=env, stdout=log, stderr=log,\n"
            "                             stdin=subprocess.DEVNULL, start_new_session=True,\n"
            "                             pass_fds=pass_fds)\n"
            "        return p.pid\n"
            "    finally:\n"
            "        if log is not None: log.close()\n"
            "        if secret_fd is not None: os.close(secret_fd)\n"
            "pid = _launch_job()\n"
            "del _launch_job\n"
            "print('LAUNCHED_PID=%d' % pid)\n"
        )
        outputs = self._execute_code(code, timeout=LAUNCH_TIMEOUT)
        text = _outputs_text(outputs)
        pid = _extract_tagged(text, "LAUNCHED_PID=", raw=True)
        if pid is None:
            raise PhaseError(
                Phase.RUN,
                f"launch RPC returned no pid: {_tail(text)}",
                RetryClass.RETRY_SAME,
            )
        self.env.workload = Workload.RUNNING
        self.env.started_at = _now()
        self._persist()
        return int(pid)

    # -- poll --------------------------------------------------------------

    def poll(self, transport, deadline: float, interval: int = 15) -> None:
        """Read the verdict through the Contents API, and only that.

        Never `execute_code` to ask whether the job is alive: that call
        competes with the workload for the kernel and, on a busy kernel,
        blocks -- turning an observation into an outage.
        """
        consecutive_degraded = 0
        while time.time() < deadline:
            result, status = transport.read_json(f"{self.remote_dir}/result.json")
            if status.name == "OK" and result:
                self._absorb_result(result)
                self.env.supervisor = Supervisor.FINISHED
                self._persist()
                return
            if status.name == "SESSION_LOST":
                self.env.workload = Workload.UNKNOWN
                self.env.supervisor = Supervisor.FINISHED
                self.env.reason = "the assignment is gone from the server"
                self.env.retry_class = RetryClass.RETRY_SAME
                self._persist()
                return
            if status.name == "DEGRADED":
                consecutive_degraded += 1
                self.env.supervisor = Supervisor.DEGRADED
                self.env.reason = (
                    f"transport failing for {consecutive_degraded} polls; "
                    "the assignment is still listed"
                )
            else:
                consecutive_degraded = 0
                self.env.supervisor = Supervisor.RUNNING
                self.env.reason = None
                wd, wd_status = transport.read_json(f"{self.remote_dir}/watchdog.json")
                if wd_status.name == "OK" and wd:
                    self.env.hints = [
                        f"t={wd.get('elapsed')}s "
                        f"remaining={wd.get('remaining')}s "
                        f"gpu={wd.get('gpu')} "
                        f"disk_free={wd.get('disk_free_bytes')} "
                        f"runner_alive={wd.get('runner_alive')}"
                    ]
            self._persist()
            time.sleep(interval)

        # Local deadline reached. The VM's own watchdog owns the kill; the
        # supervisor going home is not itself a verdict.
        self.env.supervisor = Supervisor.INTERRUPTED
        self.env.reason = "local supervisor deadline reached before a verdict"
        self.env.retry_class = RetryClass.RETRY_SAME
        self._persist()

    @staticmethod
    def absorb_provenance(env: JobEnvelope, result: dict) -> None:
        result_schema = result.get("schema_version", SCHEMA_VERSION)
        if result_schema not in {SCHEMA_VERSION, RESULT_SCHEMA_VERSION}:
            raise ValueError(f"unsupported result schema: {result_schema!r}")
        if result_schema == SCHEMA_VERSION:
            return

        for field in ("cli_version", "runtime_payload_version"):
            value = result.get(field)
            if not isinstance(value, str) or not value:
                raise ValueError(f"schema 2 result has invalid {field}")
            setattr(env, field, value)
        env.schema_version = result_schema

    @staticmethod
    def absorb_result(env: JobEnvelope, spec: JobSpec, result: dict) -> None:
        candidate = env.model_copy(deep=True)
        Orchestrator._absorb_result_in_place(candidate, spec, result)
        for field in JobEnvelope.model_fields:
            setattr(env, field, getattr(candidate, field))

    @staticmethod
    def _absorb_result_in_place(
        env: JobEnvelope, spec: JobSpec, result: dict
    ) -> None:
        remote_phase = result.get("phase")
        if remote_phase:
            try:
                env.phase = Phase(remote_phase)
            except ValueError:
                pass

        Orchestrator.absorb_provenance(env, result)

        env.workload = Workload(result.get("workload", "unknown"))
        env.exit_code = result.get("exit_code")
        env.signal = result.get("signal")
        env.exception = result.get("exception")
        env.surviving_descendants = result.get("surviving_descendants", []) or []
        env.finished_at = _now()
        env.artifacts = [
            ArtifactResult(**artifact)
            for artifact in (result.get("artifacts", []) or [])
        ]

        result_paths = {artifact.path for artifact in env.artifacts}
        missing_required = any(
            declared.required
            and (
                declared.path not in result_paths
                or any(
                    artifact.path == declared.path and artifact.status == "missing"
                    for artifact in env.artifacts
                )
            )
            for declared in spec.artifacts
        )
        if not spec.artifacts:
            env.offload = Offload.NOT_REQUIRED
        elif missing_required:
            env.offload = Offload.FAILED
            env.reason = "a required artifact was not produced"
            env.retry_class = RetryClass.FIX_CODE
        elif result.get("offload") == "failed" or any(
            artifact.status == "failed" for artifact in env.artifacts
        ):
            env.offload = Offload.FAILED
            env.reason = "artifact offload failed"
            env.retry_class = RetryClass.RETRY_SAME
        else:
            env.offload = Offload.OK

        # Stage errors are deliberately redacted by the runner because urllib
        # exception strings can contain signed query parameters. The surviving
        # evidence cannot distinguish expiry, access, and checksum failures.
        if env.phase is Phase.STAGE and env.workload is Workload.FAILED:
            env.retry_class = RetryClass.FIX_HUMAN
            env.reason = (
                "staging failed: a declared input could not be fetched, or "
                "failed its sha256 check. The consumer never started."
            )
            env.hints.append(
                "check, in order: the URL has not expired; the object exists "
                "and the grant covers it; data[].sha256 matches the object"
            )
        elif env.workload is Workload.FAILED and env.retry_class is None:
            env.retry_class = RetryClass.FIX_CODE
        if env.surviving_descendants:
            hint = (
                f"{len(env.surviving_descendants)} descendant(s) outlived the "
                "workload and may still hold the GPU"
            )
            if hint not in env.hints:
                env.hints.append(hint)

    def _absorb_result(self, result: dict) -> None:
        self.absorb_result(self.env, self.spec, result)

    # -- cleanup -----------------------------------------------------------

    def cleanup(self, force_leave_up: bool = False) -> None:
        """Always runs. Records its own outcome; never edits the verdict."""
        self._close_runtime()
        self._set_phase(Phase.CLEANUP)
        leave = force_leave_up or (
            self.env.offload is Offload.FAILED
            and self.spec.on_offload_fail == "leave_up"
        )
        if not self.env.endpoint:
            self._stop_keep_alive()
            self._drop_session()
            self.env.cleanup = Cleanup.ALREADY_ABSENT
            self._persist()
            return
        if leave:
            # A surviving descendant only matters while the VM lives: an
            # `unassign` takes the whole machine, escapee included. But if
            # we are deliberately leaving it up, cleanup did not do its job
            # -- there is now an unbounded GPU consumer the caller never
            # asked for, on a machine that keeps billing. That is a cleanup
            # failure in substance, and recording it as one is what makes
            # `ok` false; a hint an agent can skip past is not a guard.
            if self.env.surviving_descendants:
                self.env.cleanup = Cleanup.FAILED
                # `cleanup = FAILED` is on its own enough to make `ok`
                # false, so the escapee never needs to overwrite the
                # workload's verdict to be actionable. Writing `reason` or
                # `retry_class` unconditionally here would replace
                # "a required artifact was not produced" / `fix_code` with
                # `fix_human`, and send an agent to a human about a broken
                # script. Fill them only when the workload left them empty;
                # the detail always lands in `hints`.
                self.env.hints.append(
                    f"cleanup: VM left up with pids "
                    f"{self.env.surviving_descendants} still holding its "
                    f"resources; `mighty-colab job destroy {self.job_id}` "
                    "releases the VM and everything on it"
                )
                if self.env.reason is None:
                    self.env.reason = (
                        f"VM left up with {len(self.env.surviving_descendants)} "
                        "surviving descendant(s) still holding its resources"
                    )
                if self.env.retry_class is None:
                    self.env.retry_class = RetryClass.FIX_HUMAN
            else:
                self.env.cleanup = Cleanup.LEFT_UP
                self.env.hints.append(
                    f"VM left running deliberately and is still billing: "
                    f"`mighty-colab job destroy {self.job_id}` when done"
                )
            self._persist()
            return
        self._stop_keep_alive()
        try:
            self.client.unassign(self.env.endpoint)
            self.env.cleanup = Cleanup.RELEASED
        except Exception as e:  # noqa: BLE001 - teardown must not raise
            self.env.cleanup = Cleanup.FAILED
            self.env.hints.append(
                f"teardown failed ({type(e).__name__}); endpoint "
                f"{self.env.endpoint} may still be billing -- retry with "
                f"`mighty-colab job destroy {self.job_id}`"
            )
        finally:
            self._drop_session()
            self._persist()

    def _drop_session(self) -> None:
        if self.session_state is None:
            return
        try:
            self.session_store.remove(self.session_state.name)
        except Exception:  # noqa: BLE001
            pass



# -- helpers ---------------------------------------------------------------

def stop_session_keep_alive(session) -> None:
    """Stop a job session's keep-alive daemon if one is recorded."""
    pid = getattr(session, "keep_alive_pid", None) if session is not None else None
    if not pid:
        return
    from colab_cli.common import kill_process

    kill_process(pid)


def _dep_name(dep: str) -> str:
    for sep in ("==", ">=", "<=", "~=", "!=", ">", "<", "["):
        if sep in dep:
            return dep.split(sep)[0].strip()
    return dep.strip()


def _outputs_text(outputs) -> str:
    parts = []
    for o in outputs or []:
        if isinstance(o, dict):
            if "text" in o:
                parts.append(str(o["text"]))
            elif "data" in o and isinstance(o["data"], dict):
                parts.append(str(o["data"].get("text/plain", "")))
            elif o.get("output_type") == "error":
                parts.append("\n".join(o.get("traceback", [])))
    return "\n".join(parts)


def _extract_tagged(text: str, tag: str, raw: bool = False):
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(tag):
            payload = line[len(tag):]
            if raw:
                return payload
            try:
                return json.loads(payload)
            except json.JSONDecodeError:
                return None
    return None


def _tail(text: str, n: int = 600) -> str:
    return text[-n:] if len(text) > n else text

