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
import logging
import math
import re
import time
import uuid
from typing import Callable, List, Optional, Tuple


from colab_cli.auto_update import get_app_version
from colab_cli.job import RESULT_SCHEMA_VERSION, SCHEMA_VERSION
from colab_cli.job.models import (
    ArtifactResult,
    Cleanup,
    InputResult,
    InstallAttempt,
    ProvisionAttempt,
    JobEnvelope,
    JobSpec,
    Offload,
    Phase,
    Plan,
    RetryClass,
    Supervisor,
    Workload,
)
from colab_cli.job import verdict
from colab_cli.job.store import RUNNER_LOG_FILE, JobStore
from colab_cli.job.runtime_payload import RUNTIME_PAYLOAD_VERSION
from colab_cli.job.runtime_payload.redact import describe_error, redact_credentials

_logger = logging.getLogger(__name__)

# Remote layout. Everything the job owns lives under one directory so
# `destroy` has exactly one thing to remove and `plan` has exactly one
# prefix to validate `dest` paths against.
REMOTE_ROOT = "/content/jobs"
INSTALL_LOG_FILE = "install.log"
INSTALL_LOG_HINT = (
    "full installer output: install.log in the local job directory, copied "
    "from the VM before release"
)

# Records in the VM's job directory that explain a run, copied into the
# local job directory before every release. Never anything under
# `mighty_runtime/`, which holds the transfer credential handoff.
VM_RECORD_FILES = (
    RUNNER_LOG_FILE,
    INSTALL_LOG_FILE,
    "result.json",
    "exception.json",
    "watchdog.json",
    "launch.json",
    "cancel.json",
    "offload.manifest.json",
    "stage.manifest.json",
)
# A reachable VM serves these in seconds. The budget bounds how long an
# unreachable one can delay a release.
VM_RECORD_COPY_BUDGET_SECONDS = 120.0
# The runner writes launch.json within seconds of starting. A job whose
# launch.json is still absent this long after the launch RPC returned never
# started (an argument error, an import error in the runtime payload).
LAUNCH_RECORD_GRACE_SECONDS = 120.0
# One NOT_FOUND can be an expired proxy token: JobTransport refreshes the
# token on a 404 at most once per minute. launch.json must stay absent for
# longer than that before the runner is declared never started.
LAUNCH_ABSENCE_CONFIRM_SECONDS = 90.0
# A cancelled runner gets this long, by default, to stop the workload,
# upload artifacts and write result.json, before `job destroy` or apply's
# --timeout releases the VM anyway.
RUNNER_STOP_WAIT_SECONDS = 300
RUNNER_RESULT_POLL_SECONDS = 5

# The launch RPC must not inherit the 10s default: `execute_code`'s timeout
# is a wall-clock budget, and a cold import of the runtime package on a
# freshly restarted kernel can exceed it. It is still finite -- the call
# only spawns a process and returns a pid, so anything approaching this
# ceiling means the kernel itself is wedged.
LAUNCH_TIMEOUT = 120.0
VERIFY_TIMEOUT = 180.0
RESTART_TIMEOUT = 60.0


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# Characters of a JSON assign-failure body kept in a provision attempt.
ASSIGN_BODY_CHARS = 300


def _failed_assign(want: str, error: BaseException) -> ProvisionAttempt:
    """A provision attempt that raised: its HTTP status, the error with the
    assign URL's query removed, a JSON body excerpt or why the body was not
    kept, and its retry class."""
    import requests

    from colab_cli.client import response_body_if_json
    from colab_cli.utils import get_status_code

    status = get_status_code(error)
    raw_body = getattr(error, "response_body", None) or ""
    body = response_body_if_json(error, limit=len(raw_body))
    if body is not None:
        body = redact_credentials(" ".join(body.split()))
        if len(body) > ASSIGN_BODY_CHARS:
            omitted = len(body) - ASSIGN_BODY_CHARS
            body = body[:ASSIGN_BODY_CHARS] + f" [... {omitted} characters omitted]"
    else:
        raw = raw_body
        if raw:
            response = getattr(error, "response", None)
            content_type = (
                getattr(response, "headers", {}).get("Content-Type", "unknown type")
                if response is not None
                else "unknown type"
            )
            body = f"[{content_type} body of {len(raw)} characters not kept]"
    return ProvisionAttempt(
        accelerator=want,
        outcome="failed",
        http_status=status,
        error=describe_error(error),
        body=body,
        retry_class=verdict.assign_retry_class(
            status, isinstance(error, requests.exceptions.RequestException)
        ),
    )


def _vm_time(epoch) -> Optional[str]:
    """An epoch timestamp the VM recorded, as ISO 8601 UTC."""
    if not isinstance(epoch, (int, float)) or isinstance(epoch, bool):
        return None
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).isoformat()


# Messages the vendored kernel client raises when the kernel connection,
# not the code it ran, failed.
_KERNEL_TRANSPORT_MESSAGES = (
    "Connection was lost",
    "channel must be running",
    "must first start a kernel",
    "didn't respond to heartbeats",
    "Kernel died before replying",
)


class KernelInterrupted(Exception):
    """The running cell was interrupted (KeyboardInterrupt): the kernel was
    interrupted, restarted or shut down while the call ran. Jupyter
    interrupts a busy kernel before a shutdown or restart, so the call
    returns an error output instead of raising."""


def _kernel_transport_failure(error: BaseException) -> bool:
    """Whether a kernel execute call failed in transport (connection lost,
    reply timeout, websocket closed, cell interrupted) rather than in the
    code it ran."""
    if isinstance(error, (OSError, KernelInterrupted)):  # OSError includes TimeoutError
        return True
    try:
        import requests

        if isinstance(error, requests.RequestException):
            return True
    except ImportError:
        pass
    try:
        import websocket

        if isinstance(error, websocket.WebSocketException):
            return True
    except ImportError:
        pass
    return isinstance(error, RuntimeError) and any(
        message in str(error) for message in _KERNEL_TRANSPORT_MESSAGES
    )


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


class KernelCallError(PhaseError):
    """A kernel execute call raised instead of returning a reply.

    `transport` is true when the connection failed (lost websocket, reply
    timeout): retry_same. Anything else keeps its type and message and is
    do_not_retry.
    """

    def __init__(self, phase: Phase, error: BaseException, hints=None):
        self.transport = _kernel_transport_failure(error)
        if isinstance(error, KernelInterrupted):
            what = "kernel interrupted"
        elif self.transport:
            what = "kernel connection failed"
        else:
            what = "kernel call failed"
        super().__init__(
            phase,
            f"{what} during {phase.value}: {describe_error(error)}",
            RetryClass.RETRY_SAME if self.transport else RetryClass.DO_NOT_RETRY,
            hints,
        )


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
        # poll(): whether the runner's own records have ever been read, and
        # since when launch.json has been continuously absent.
        self._runner_seen = False
        self._watchdog_staleness = WatchdogStaleness()
        self._launch_absent_since: Optional[float] = None

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

    def _pull_runner_log(self, transport) -> None:
        """Best-effort: copy the VM's runner.log to local disk.

        Called every healthy poll tick. `cleanup()` copies runner.log again,
        with the other VM records, through `copy_vm_records` immediately
        before release. Any failure here (including a transport double that
        doesn't implement `read_text`) must never break the verdict poll.
        """
        try:
            log_text, log_status = transport.read_text(
                f"{self.remote_dir}/{RUNNER_LOG_FILE}"
            )
            if log_status.name == "OK" and log_text is not None:
                log_path = self.store.job_dir(self.job_id) / RUNNER_LOG_FILE
                log_path.parent.mkdir(parents=True, exist_ok=True)
                log_path.write_text(log_text)
        except Exception:  # noqa: BLE001 - best-effort, never fatal
            pass


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
        attempts: List[ProvisionAttempt] = []

        candidates = list(self.spec.accelerator.prefer)
        if self.spec.accelerator.accept_cpu:
            candidates.append("NONE")

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
                attempt = _failed_assign(want, e)
                attempt.retry_class = RetryClass.FIX_HUMAN
                attempts.append(attempt)
                self.env.provision_attempts = attempts
                raise PhaseError(
                    Phase.PROVISION,
                    f"account is at its concurrent-assignment limit: {attempt.error}"
                    + (f"; response body: {attempt.body}" if attempt.body else ""),
                    RetryClass.FIX_HUMAN,
                    ["run `mighty-colab sessions` and stop what you are not using"],
                ) from e
            except Exception as e:  # noqa: BLE001 - each attempt is classified
                attempt = _failed_assign(want, e)
                attempts.append(attempt)
                self.emit(
                    f"[job] {want} unavailable ({attempt.error}), trying next preference"
                )
                continue

            granted = getattr(res.accelerator, "name", str(res.accelerator))
            self.env.actual_accelerator = granted
            self.env.endpoint = res.endpoint

            # A GPU request satisfied by a CPU box is the silent failure that
            # publishes chance-level science. Refuse it unless asked.
            if granted in ("NONE", "UNRECOGNIZED") and want != "NONE":
                released, detail = release_assignment(self.client, res.endpoint)
                if released is Cleanup.FAILED:
                    # The endpoint stays in the envelope so cleanup retries
                    # the release; assigning the next preference would
                    # overwrite it while this VM still bills.
                    raise PhaseError(
                        Phase.PROVISION,
                        f"{want} was granted as {granted} and releasing it "
                        f"({res.endpoint}) failed: {detail}",
                        RetryClass.RETRY_SAME,
                    )
                self.env.endpoint = None
                attempts.append(
                    ProvisionAttempt(
                        accelerator=want,
                        outcome="refused_cpu",
                        granted=granted,
                        retry_class=RetryClass.RETRY_DIFFERENT,
                    )
                )
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
            if attempts:
                attempts.append(
                    ProvisionAttempt(accelerator=want, outcome="granted", granted=granted)
                )
                self.env.provision_attempts = attempts
            self._persist()
            self._start_keep_alive()
            self.emit(f"[job] provisioned {res.endpoint} accel={granted}")
            return

        self.env.provision_attempts = attempts
        retry = verdict.strongest(a.retry_class for a in attempts)
        hints = {
            RetryClass.FIX_HUMAN: [
                "Colab refused the account: a 400 means no quota or entitlement "
                "for that accelerator on this account's plan; 401/403 are its "
                "credentials or OAuth scope.",
            ],
            RetryClass.RETRY_DIFFERENT: [
                "Colab capacity varies by hour; a different accelerator in "
                "`accelerator.prefer` is usually available sooner than the "
                "same one later.",
                "Set `accelerator.accept_cpu: true` only if the workload is "
                "genuinely useful without a GPU.",
            ],
        }.get(retry, [])
        raise PhaseError(
            Phase.PROVISION,
            f"no acceptable accelerator from {candidates}: "
            + "; ".join(a.summary for a in attempts),
            retry,
            hints,
        )

    def _start_keep_alive(self) -> None:
        """Own the TFE daemon for this assignment.

        Persist the session first so the detached child cannot observe an
        empty store and exit with `session_not_found`.
        """
        from colab_cli.client import ColabRequestError
        from colab_cli.commands.session import (
            _is_scope_error,
            _record_keep_alive_success,
            spawn_keep_alive,
        )
        from colab_cli.utils import get_status_code

        session = self.session_state
        try:
            self.client.keep_alive_assignment(session.endpoint)
        except ColabRequestError as exc:
            if get_status_code(exc) == 403 and _is_scope_error(exc):
                released, detail = release_assignment(self.client, session.endpoint)
                if released is Cleanup.FAILED:
                    # Keep the endpoint: cleanup retries the release, and
                    # the envelope keeps the handle to a VM that may bill.
                    self.env.hints.append(
                        f"releasing {session.endpoint} after the keep-alive scope "
                        f"error failed ({detail}); it may still be billing"
                    )
                else:
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
            self._tolerate_keep_alive_preflight(session, exc)
        except Exception as exc:  # noqa: BLE001 - a network error, tolerated the same way
            self._tolerate_keep_alive_preflight(session, exc)
        else:
            _record_keep_alive_success(session)

        self.session_store.add(session)
        try:
            session.keep_alive_pid = spawn_keep_alive(
                session.endpoint,
                session.name,
                auth_provider=self.auth_provider,
                config_path=self.config_path,
            )
        except Exception as exc:  # noqa: BLE001 - reported as the provision failure
            # The endpoint stays in the envelope, so cleanup releases the VM.
            raise PhaseError(
                Phase.PROVISION,
                f"the keep-alive daemon could not be started ({describe_error(exc)}); "
                "without it Colab reclaims the idle VM while the job runs",
                RetryClass.FIX_HUMAN,
            ) from exc
        self.session_store.add(session)

    def _tolerate_keep_alive_preflight(self, session, error: BaseException) -> None:
        """A failed pre-flight ping that is not a missing scope: the daemon
        retries on its own schedule, so provisioning continues."""
        from colab_cli.commands.session import _record_keep_alive_failure
        from colab_cli.utils import get_status_code

        _record_keep_alive_failure(session)
        status = get_status_code(error)
        self.env.hints.append(
            "keep-alive pre-flight failed "
            f"({'HTTP ' + str(status) + '; ' if status else ''}{describe_error(error)}); "
            "the daemon was started anyway"
        )

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

    def _execute_code(
        self, code: str, *, timeout: float, phase: Phase, hints=None
    ):
        runtime = self._runtime_handle()
        try:
            outputs = runtime.execute_code(code, timeout=timeout)
        except Exception as error:  # noqa: BLE001 - classified per phase
            raise KernelCallError(phase, error, hints) from error
        finally:
            self._sync_runtime_identity()
        if any(
            isinstance(o, dict)
            and o.get("output_type") == "error"
            and o.get("ename") == "KeyboardInterrupt"
            for o in outputs or []
        ):
            raise KernelCallError(
                phase,
                KernelInterrupted(
                    "the cell was interrupted (KeyboardInterrupt): the kernel was "
                    "interrupted, restarted or shut down while it ran"
                ),
                hints,
            )
        return outputs

    def detach(self) -> None:
        """Stop supervising without touching the VM: close the local kernel
        client, whose websocket threads would otherwise keep this process
        from exiting. The runner, the VM and the keep-alive daemon go on."""
        self._close_runtime()

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
        """uv first, pip if uv fails; see colab_cli.job.install."""
        if not self.spec.deps:
            return
        from colab_cli.job import install as deps

        self._set_phase(Phase.INSTALL)
        outputs = self._execute_code(
            deps.install_code(f"{self.remote_dir}/{INSTALL_LOG_FILE}", list(self.spec.deps)),
            timeout=deps.INSTALL_KERNEL_TIMEOUT,
            phase=Phase.INSTALL,
            hints=[INSTALL_LOG_HINT],
        )
        text = _outputs_text(outputs)
        raw = deps.parse_install_result(text)
        if raw is None:
            # The kernel code itself failed (a full disk, a broken VM),
            # before any installer reported: not the user's pins.
            errors = [
                f"{o.get('ename')}: {o.get('evalue')}"
                for o in outputs or []
                if isinstance(o, dict) and o.get("output_type") == "error"
            ]
            detail = "; ".join(errors) if errors else _tail(text, phase="install")
            raise PhaseError(
                Phase.INSTALL,
                "the install step failed on the VM before an installer reported: "
                f"{redact_credentials(detail)}",
                RetryClass.DO_NOT_RETRY,
                [INSTALL_LOG_HINT],
            )
        attempts = []
        for attempt in raw:
            attempt = deps.redact_attempt(attempt)
            attempt["failure"], attempt["key_lines"] = deps.classify_attempt(attempt)
            attempts.append(attempt)
        records = [
            InstallAttempt(**{k: a[k] for k in InstallAttempt.model_fields if k in a})
            for a in attempts
        ]
        last = attempts[-1]
        if last["exit_code"] == 0:
            if len(attempts) == 1:
                self.env.hints.append(
                    f"dependencies installed with {deps.short_version(last['version'])} "
                    f"in {last['seconds']}s"
                )
            else:
                first = attempts[0]
                self.env.install_attempts = records
                self.env.hints.append(
                    f"{first['installer']} failed ({first['failure']}): "
                    f"{(first['key_lines'] or ['no error line'])[0]}; dependencies "
                    f"installed with {deps.short_version(last['version'])} instead"
                )
            return
        self.env.install_attempts = records
        retry_class, failure = deps.install_verdict(attempts)
        raise PhaseError(
            Phase.INSTALL,
            deps.failure_reason(list(self.spec.deps), attempts, failure),
            retry_class,
            [deps.FAILURE_HINT[failure], INSTALL_LOG_HINT],
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
            timed_out = isinstance(error, TimeoutError) or type(error).__name__ in (
                "Timeout",
                "ReadTimeout",
                "ConnectTimeout",
            )
            what = (
                f"kernel restart did not complete within {RESTART_TIMEOUT:.0f}s"
                if timed_out
                else "kernel restart failed"
            )
            raise PhaseError(
                Phase.RESTART,
                f"{what}: {describe_error(error)}",
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
        outputs = self._execute_code(code, timeout=VERIFY_TIMEOUT, phase=Phase.VERIFY)
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
        source_bytes = sum(
            item.size_bytes for item in (self.plan.source_files or [])
        )
        input_bytes = self.plan.input_bytes()
        output_bytes = sum(a.size_bytes or 0 for a in self.spec.artifacts)
        free = payload.get("free")
        self.env.hints.append(
            f"verified device={payload.get('device')} "
            f"source={source_bytes} input={input_bytes} "
            f"output={output_bytes} free={free}"
        )
        self._check_disk(free, source_bytes, input_bytes, output_bytes)
        self._check_url_expiry()
        self._persist()

    def _check_url_expiry(self) -> None:
        """Install has run, so the time left on each signed URL is checked
        again against what remains: staging, the run and offload. Apply's
        own check before assignment allowed for the longest install, so
        this fails only when provisioning, install and restart together
        took longer than that allowance."""
        from colab_cli.job.planner import revalidate_expiry

        problems = revalidate_expiry(self.plan, after_install=True)
        if problems:
            raise PhaseError(
                Phase.VERIFY,
                "signed URLs no longer last until the end of the run: "
                + "; ".join(p.message for p in problems),
                RetryClass.REFRESH_URLS,
                ["re-sign the URLs with a longer expiry, re-plan and re-apply"],
            )

    def _check_disk(
        self,
        free: Optional[int],
        source_bytes: int,
        input_bytes: int,
        output_bytes: int,
    ) -> None:
        """Refuse a job whose declared payloads cannot fit."""
        if not free:
            return
        declared = source_bytes + input_bytes + output_bytes
        if declared and declared > free * 0.8:
            raise PhaseError(
                Phase.VERIFY,
                f"source={source_bytes} input={input_bytes} output={output_bytes} "
                f"bytes but only {free} free on /content",
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
        outputs = self._execute_code(code, timeout=LAUNCH_TIMEOUT, phase=Phase.STAGE)
        if "SECRET_CHANNEL_READY=1" not in _outputs_text(outputs):
            raise PhaseError(
                Phase.STAGE,
                # This kernel code is ours, not the consumer's -- its output
                # can't contain user data or signed URLs, so there's no
                # reason to withhold it the way stage/pip output must be.
                f"credential channel preparation failed: "
                f"{_tail(_outputs_text(outputs), phase='secret-channel-prepare')}",
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
        outputs = self._execute_code(code, timeout=LAUNCH_TIMEOUT, phase=Phase.STAGE)
        if "SECRET_CHANNEL_SEALED=1" not in _outputs_text(outputs):
            raise PhaseError(
                Phase.STAGE,
                f"credential channel sealing failed: "
                f"{_tail(_outputs_text(outputs), phase='secret-channel-seal')}",
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
            outputs = self._execute_code(code, timeout=LAUNCH_TIMEOUT, phase=Phase.CLEANUP)
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

    def launch(self, payload_remote_path: str) -> Optional[int]:
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
            + (
                f"        cmd += ['--artifact-sync-interval', "
                f"str({self.spec.budgets.artifact_sync_interval_seconds!r})]\n"
                if self.spec.budgets.artifact_sync_interval_seconds is not None
                else ""
            )
            + f"        cmd += ['--'] + {args}\n"
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
        try:
            outputs = self._execute_code(code, timeout=LAUNCH_TIMEOUT, phase=Phase.RUN)
        except KernelCallError as error:
            if not error.transport:
                raise
            # The runner is detached; the kernel connection only carried the
            # launch request. Whether the runner started is decided from its
            # own files by poll(): no launch.json within the grace and
            # confirmation windows means it never started.
            self.env.workload = Workload.RUNNING
            self.env.started_at = _now()
            self.env.hints.append(
                f"the launch call's reply was lost ({error.reason}); the runner "
                "is observed through its files instead"
            )
            self._persist()
            return None
        text = _outputs_text(outputs)
        pid = _extract_tagged(text, "LAUNCHED_PID=", raw=True)
        if pid is None:
            raise PhaseError(
                Phase.RUN,
                f"launch RPC returned no pid: {_tail(text, phase='launch')}",
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
                self._absorb_or_keep(result, transport)
                return
            if status.name == "SESSION_LOST":
                self._finish_without_result(
                    "the assignment is gone from the server", transport
                )
                return
            if status.name == "DEGRADED":
                consecutive_degraded += 1
                self.env.supervisor = Supervisor.DEGRADED
                self.env.reason = degraded_reason(transport, consecutive_degraded)
            else:
                consecutive_degraded = 0
                self.env.supervisor = Supervisor.RUNNING
                self.env.reason = None
                wd, wd_status = transport.read_json(f"{self.remote_dir}/watchdog.json")
                if wd_status.name == "OK" and wd:
                    self._runner_seen = True
                    replace_hint(self.env.hints, WATCHDOG_HINT_PREFIX, watchdog_hint(wd))
                    stalled = self._watchdog_staleness.observe(wd)
                    replace_hint(self.env.hints, WATCHDOG_STALLED_PREFIX, stalled)
                    if stalled:
                        self.env.reason = stalled
                    if wd.get("runner_alive") is False:
                        if self._absorb_late_result(transport):
                            return
                        self._finish_without_result(
                            "the runner is dead and wrote no result.json "
                            f"(watchdog: elapsed={wd.get('elapsed')}s, "
                            f"remaining={wd.get('remaining')}s); its last output "
                            "is in runner.log",
                            transport,
                        )
                        return
                elif wd_status.name == "NOT_FOUND" and not self._runner_seen:
                    # Only a runner never observed can be "never started";
                    # once seen, a later NOT_FOUND is a transport question.
                    since_launch = self._seconds_since_launch()
                    if (
                        since_launch is not None
                        and since_launch > LAUNCH_RECORD_GRACE_SECONDS
                    ):
                        _launch, launch_status = transport.read_json(
                            f"{self.remote_dir}/launch.json"
                        )
                        if launch_status.name == "OK":
                            self._runner_seen = True
                            self._launch_absent_since = None
                        elif self._launch_absent_since is None:
                            if launch_status.name == "NOT_FOUND":
                                self._launch_absent_since = time.monotonic()
                        if (
                            launch_status.name == "NOT_FOUND"
                            and self._launch_absent_since is not None
                            and time.monotonic() - self._launch_absent_since
                            >= LAUNCH_ABSENCE_CONFIRM_SECONDS
                        ):
                            if self._absorb_late_result(transport):
                                return
                            self._finish_without_result(
                                "the runner never started: no launch.json "
                                f"{since_launch:.0f}s after launch; its error "
                                "output is in runner.log",
                                transport,
                            )
                            return
                # Every poll, not just at the end: runner.log is on VM disk
                # the whole run (orchestrator.launch() redirects the
                # runner's stdout/stderr there) and nothing ever reads it
                # back today -- if the VM disappears before offload, it's
                # gone with everything else, no different than never
                # having been written. No new signed URL, no watchdog
                # changes: same Contents connection already open for
                # result.json/watchdog.json above.
                self._pull_runner_log(transport)
            self._persist()
            time.sleep(interval)

        # Local deadline reached. The VM's own watchdog owns the kill; the
        # supervisor going home is not itself a verdict.
        self.env.supervisor = Supervisor.INTERRUPTED
        self.env.reason = "local supervisor deadline reached before a verdict"
        self.env.retry_class = RetryClass.RETRY_SAME
        self._persist()

    def _absorb_late_result(self, transport) -> bool:
        """Read result.json once more before declaring the runner gone: it
        writes the result, then exits, and the watchdog can see it dead
        in between."""
        result, status = transport.read_json(f"{self.remote_dir}/result.json")
        if status.name == "OK" and result:
            self._absorb_or_keep(result, transport)
            return True
        return False

    def cancel_after_deadline(
        self,
        transport,
        passed: str,
        requester: str,
        wait: float = RUNNER_STOP_WAIT_SECONDS,
    ) -> None:
        """A deadline passed with no verdict: cancel the runner, wait up to
        `wait` seconds for its result, and end the job so cleanup releases
        the VM. Reaching the deadline is a failure of the run to produce a
        verdict in time, whatever the runner reports after. `passed` says
        which deadline, `requester` names the canceller in cancel.json."""
        note, confirmed = request_cancel(transport, self.job_id, requester)
        kind, outcome = await_runner_result(transport, self.job_id, wait)
        if kind == "result":
            self._absorb_or_keep(outcome, transport)
            self.env.record_failure(Phase.RUN)
            self.env.reason = deadline_reason(
                passed, note, confirmed, requester, outcome, self.env.reason
            )
            if self.env.retry_class is None:
                self.env.retry_class = RetryClass.RETRY_SAME
        else:
            self._finish_without_result(f"{passed}; {note}; {outcome}", transport)
        self.env.hints.append(
            "if the work needs longer, raise apply's --timeout or budgets.wall_clock"
        )
        self._persist()

    def _absorb_or_keep(self, result: dict, transport) -> None:
        """Absorb the runner's result. One this CLI cannot parse (a newer
        schema, an invalid field) still ends the poll, with the parse
        error and the raw verdict fields in the reason."""
        try:
            self._absorb_result(result)
        except Exception as error:  # noqa: BLE001 - kept in the reason
            self._finish_without_result(
                f"runner result could not be absorbed ({describe_error(error)}); "
                f"raw result: {raw_verdict(result)}",
                transport,
            )
            return
        self.env.supervisor = Supervisor.FINISHED
        self._persist()

    def _seconds_since_launch(self) -> Optional[float]:
        if not self.env.started_at:
            return None
        try:
            started = datetime.datetime.fromisoformat(self.env.started_at)
        except ValueError:
            return None
        return (datetime.datetime.now(datetime.timezone.utc) - started).total_seconds()

    def _finish_without_result(self, reason: str, transport) -> None:
        """No result.json will arrive. The verdict is unknown; nothing was
        offloaded by the runner, so offload is terminal too, and cleanup
        can release the VM."""
        self.env.workload = Workload.UNKNOWN
        if not self.env.offload.terminal:
            self.env.offload = (
                Offload.SKIPPED if self.spec.artifacts else Offload.NOT_REQUIRED
            )
        self.env.supervisor = Supervisor.FINISHED
        self.env.reason = reason
        self.env.retry_class = RetryClass.RETRY_SAME
        self.env.finished_at = _now()
        self.env.record_failure(Phase.RUN)
        self._pull_runner_log(transport)
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
        candidate = JobEnvelope.model_validate(
            {field: getattr(candidate, field) for field in JobEnvelope.model_fields}
        )
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

        # The runner's verdict supersedes reasons written while it was
        # unknown (an interruption, a degraded transport); they are set
        # again from the result below.
        env.reason = None
        env.retry_class = None
        env.workload = Workload(result.get("workload", "unknown"))
        env.exit_code = result.get("exit_code")
        env.signal = result.get("signal")
        env.exception = result.get("exception")
        env.surviving_descendants = result.get("surviving_descendants", []) or []
        env.finished_at = _vm_time(result.get("finished_at"))
        if env.finished_at is None:
            env.finished_at = _now()
            _logger.warning(
                "result.json finished_at %r is not an epoch time; "
                "finished_at is this machine's time %s",
                result.get("finished_at"),
                env.finished_at,
            )
        env.inputs = [InputResult(**item) for item in (result.get("inputs") or [])]
        env.artifacts = [
            ArtifactResult(**artifact)
            for artifact in (result.get("artifacts", []) or [])
        ]

        # A stage failure means the consumer never ran and the runner never
        # reached offload: nothing was uploaded because nothing was tried.
        stage_failed = env.phase is Phase.STAGE and env.workload is Workload.FAILED
        offload_reason = offload_retry = None
        if not spec.artifacts:
            env.offload = Offload.NOT_REQUIRED
        elif stage_failed:
            env.offload = Offload.SKIPPED
        else:
            env.offload, offload_reason, offload_retry = verdict.offload_outcome(
                spec.artifacts,
                env.artifacts,
                result.get("offload"),
                result.get("offload_error"),
            )

        if stage_failed:
            env.reason, env.retry_class = verdict.stage_outcome(
                env.inputs, env.exception
            )
            env.record_failure(Phase.STAGE)
        else:
            workload_reason, workload_retry = verdict.workload_outcome(
                result, spec.budgets.wall_clock
            )
            env.reason = (
                "; ".join(r for r in (workload_reason, offload_reason) if r) or None
            )
            # The workload's own failure decides the next action; an upload
            # failure alongside it is named in the reason.
            env.retry_class = workload_retry or offload_retry
            if workload_retry is not None:
                env.record_failure(Phase.RUN)
            elif env.offload is Offload.FAILED:
                env.record_failure(Phase.OFFLOAD)
        for warning in result.get("runner_warnings") or []:
            hint = f"runner: {warning}"
            if hint not in env.hints:
                env.hints.append(hint)
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

    def cleanup(self, leave_up: bool = False) -> None:
        """Always runs. Records its own outcome; never edits the verdict.
        `leave_up` is the caller's decision to keep the VM."""
        self._close_runtime()
        self._set_phase(Phase.CLEANUP)
        leave = leave_up
        if not self.env.endpoint:
            self._stop_keep_alive()
            self._drop_session()
            self.env.cleanup = Cleanup.ALREADY_ABSENT
            self._persist()
            return
        if leave:
            record_vm_kept(self.env, self.job_id)
            self._persist()
            return
        # Last chance: whatever explains this run is on the VM, and the VM
        # is about to go.
        try:
            transport = self.job_transport()
        except Exception as e:  # noqa: BLE001 - teardown must not raise
            self.env.hints.append(
                f"VM records not copied before release ({describe_error(e)})"
            )
        else:
            self.env.hints.append(copy_vm_records(transport, self.store, self.job_id))
        self._stop_keep_alive()
        try:
            self.env.cleanup, detail = release_assignment(self.client, self.env.endpoint)
            if self.env.cleanup is Cleanup.FAILED:
                self.env.record_failure(Phase.CLEANUP)
            if detail:
                self.env.hints.append(
                    f"teardown failed ({detail}); endpoint "
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


_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def _outputs_text(outputs) -> str:
    """Kernel outputs as plain text: streams, results, and errors with
    their name and value even when the traceback is empty. ANSI colour
    codes are removed, so they do not use up `_tail`'s budget."""
    parts = []
    for o in outputs or []:
        if isinstance(o, dict):
            if "text" in o:
                parts.append(str(o["text"]))
            elif "data" in o and isinstance(o["data"], dict):
                parts.append(str(o["data"].get("text/plain", "")))
            elif o.get("output_type") == "error":
                parts.append("\n".join(o.get("traceback", [])))
                parts.append(f"{o.get('ename', 'Error')}: {o.get('evalue', '')}")
    return _ANSI_ESCAPE.sub("", "\n".join(parts))


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


def keep_vm(env: JobEnvelope, spec: Optional[JobSpec], *, secret_removed: bool) -> bool:
    """Whether to keep the VM after the job: `--leave-up` was given, or an
    artifact upload was attempted and failed under `on_offload_fail:
    leave_up`, so the file is on the VM to rescue. A required artifact that
    was never produced also fails offload, but leaves nothing to rescue.
    Never when the transfer credential's deletion is unconfirmed: the
    release removes it with the VM. `spec` is None when the plan cannot be
    read; then only `--leave-up` counts."""
    if not secret_removed:
        return False
    upload_failed = spec is not None and spec.on_offload_fail == "leave_up" and any(
        artifact.status == "failed" for artifact in env.artifacts
    )
    return env.leave_up or upload_failed


def record_vm_kept(env: JobEnvelope, job_id: str) -> None:
    """Record the VM kept up on purpose. A surviving descendant on it is a
    cleanup failure: an unbounded consumer nobody asked for, on a machine
    that keeps billing."""
    if env.surviving_descendants:
        env.cleanup = Cleanup.FAILED
        env.record_failure(Phase.CLEANUP)
        # `cleanup = FAILED` alone makes `ok` false; the workload's reason
        # and retry class are filled only when it left them empty, so a
        # broken script is not sent to a human.
        env.hints.append(
            f"cleanup: VM left up with pids {env.surviving_descendants} still "
            f"holding its resources; `mighty-colab job destroy {job_id}` "
            "releases the VM and everything on it"
        )
        if env.reason is None:
            env.reason = (
                f"VM left up with {len(env.surviving_descendants)} "
                "surviving descendant(s) still holding its resources"
            )
        if env.retry_class is None:
            env.retry_class = RetryClass.FIX_HUMAN
    else:
        env.cleanup = Cleanup.LEFT_UP
        env.hints.append(
            f"VM left running deliberately and is still billing: "
            f"`mighty-colab job destroy {job_id}` when done"
        )


def release_assignment(client, endpoint: str) -> Tuple[Cleanup, Optional[str]]:
    """Unassign `endpoint`.

    Returns `RELEASED`, `ALREADY_ABSENT` when the server answers 404, or
    `FAILED` with a description of the error: HTTP status, exception type
    and message, and the start of a JSON response body, query strings
    redacted. Never raises.
    """
    from colab_cli.client import response_body_if_json
    from colab_cli.utils import get_status_code

    try:
        client.unassign(endpoint)
        return Cleanup.RELEASED, None
    except Exception as error:  # noqa: BLE001 - every caller records the outcome
        status = get_status_code(error)
        if status == 404:
            # A 404 can come from a changed control-plane path as well as
            # from a VM that is gone, and already_absent records get pruned.
            # Only the assignment listing confirms the VM is gone.
            try:
                listed = any(
                    _listed_endpoint(assignment) == endpoint
                    for assignment in client.list_assignments()
                )
            except Exception as listing_error:  # noqa: BLE001
                return Cleanup.FAILED, (
                    f"HTTP 404 from unassign ({describe_error(error)}), and the "
                    f"assignment listing that would confirm it failed "
                    f"({describe_error(listing_error)})"
                )
            if not listed:
                return Cleanup.ALREADY_ABSENT, None
            return Cleanup.FAILED, (
                f"HTTP 404 from unassign, but {endpoint} is still listed; "
                f"{describe_error(error)}"
            )
        detail = describe_error(error)
        if status is not None:
            detail = f"HTTP {status}; {detail}"
        body = response_body_if_json(error, limit=300)
        if body:
            detail += f"; body: {redact_credentials(body)}"
        return Cleanup.FAILED, detail


def observe_remote(transport, job_id: str):
    result, status = transport.read_json(f"/content/jobs/{job_id}/result.json")
    if status.name == "SESSION_LOST":
        return "session_lost", None
    if status.name == "OK" and result:
        return "result", result
    if status.name == "DEGRADED":
        return "degraded", None
    launch, launch_status = transport.read_json(
        f"/content/jobs/{job_id}/launch.json"
    )
    if launch_status.name == "SESSION_LOST":
        return "session_lost", None
    if launch_status.name == "DEGRADED":
        return "degraded", None
    if launch_status.name != "OK" or not launch:
        return "never_started", None
    pid = launch.get("pid")
    starttime = launch.get("starttime")
    boot_id = launch.get("boot_id")
    if (
        not isinstance(pid, int)
        or not isinstance(starttime, str)
        or not isinstance(boot_id, str)
    ):
        return "launch_invalid", launch
    watchdog, watchdog_status = transport.read_json(
        f"/content/jobs/{job_id}/watchdog.json"
    )
    if watchdog_status.name == "SESSION_LOST":
        return "session_lost", None
    record = watchdog if watchdog_status.name == "OK" and watchdog else None
    if record is not None and record.get("runner_alive") is False:
        return "runner_dead", record
    return "runner_alive", record


# A run's local deadline, after launch: its wall_clock plus this margin
# for the runner's own escalation, offload and result write.
RUN_DEADLINE_MARGIN_SECONDS = 600


def run_deadline_seconds(spec: JobSpec) -> int:
    return spec.budgets.wall_clock + RUN_DEADLINE_MARGIN_SECONDS


# A healthy watchdog rewrites watchdog.json every 30 seconds.
WATCHDOG_STALE_SECONDS = 300
WATCHDOG_HINT_PREFIX = "watchdog: "
WATCHDOG_STALLED_PREFIX = "watchdog stalled: "


class WatchdogStaleness:
    """Whether watchdog.json has stopped changing. Compares successive `ts`
    values for equality and times the gap with this machine's monotonic
    clock: the VM's clock is never compared with this one. Only
    successful reads count."""

    def __init__(self, clock: Optional[Callable[[], float]] = None) -> None:
        self._clock = clock or (lambda: time.monotonic())
        self._ts = None
        self._since: Optional[float] = None

    def observe(self, record: dict) -> Optional[str]:
        """The stall, in words, when `ts` has not changed for
        WATCHDOG_STALE_SECONDS; otherwise None."""
        ts = record.get("ts")
        now = self._clock()
        if self._since is None or ts != self._ts:
            self._ts, self._since = ts, now
            return None
        unchanged = now - self._since
        if unchanged < WATCHDOG_STALE_SECONDS:
            return None
        return (
            f"watchdog.json has not changed for {unchanged:.0f}s (ts={ts}): the "
            "watchdog has stopped, or cannot write its record (for example a "
            "full disk), so whether the runner is alive is unknown"
        )


def degraded_reason(transport, polls: Optional[int] = None) -> str:
    """Why reading the VM is failing: the transport's last recorded
    problem, and what the last assignment listing showed."""
    problem = getattr(transport, "last_problem", None)
    listing_note = getattr(transport, "listing_note", None)
    listing = listing_note() if callable(listing_note) else None
    text = (
        f"transport failing for {polls} polls" if polls is not None else "transport failing"
    )
    if isinstance(problem, str):
        text += f" (last: {problem})"
    if isinstance(listing, str):
        text += f"; {listing}"
    return text


def replace_hint(hints: List[str], prefix: str, text: Optional[str]) -> None:
    """Replace the hint starting with `prefix` in place (or drop it when
    `text` is None), leaving every other hint as it was."""
    kept = [h for h in hints if not h.startswith(prefix)]
    if text is not None:
        kept.append(prefix + text)
    hints[:] = kept


def request_cancel(transport, job_id: str, requester: str) -> Tuple[str, bool]:
    """Write the cancel intent. Returns a note saying what happened and
    whether the write was confirmed."""
    try:
        intent = transport.write_json(
            f"/content/jobs/{job_id}/cancel.json",
            {"intent": "cancelled", "by": requester, "at": _now()},
        )
    except Exception as error:  # noqa: BLE001 - release must still happen
        return f"cancel intent not written ({describe_error(error)})", False
    if getattr(intent, "name", "") == "OK":
        return "cancel requested", True
    return f"cancel intent not confirmed ({getattr(intent, 'name', intent)})", False


def deadline_reason(
    passed: str,
    note: str,
    confirmed: bool,
    requester: str,
    result: Optional[dict],
    absorbed_reason: Optional[str],
) -> str:
    """The reason after a deadline cancel that got a result: what passed,
    the cancel note unless the result shows this cancel arrived, and the
    result's own reason."""
    received = (
        result is not None
        and verdict.cancel_source(result.get("cancel_intent")) == requester
    )
    return "; ".join(
        part
        for part in (passed, None if (received and confirmed) else note, absorbed_reason)
        if part
    )


def watchdog_hint(wd: dict) -> str:
    """One line from watchdog.json: elapsed and remaining time, GPU (or why
    nvidia-smi gave nothing), free disk, and whether the runner is alive
    (or why that is unknown)."""
    gpu = wd.get("gpu")
    if gpu is None and wd.get("gpu_error"):
        gpu = f"none ({wd['gpu_error']})"
    alive = f"{wd.get('runner_alive')}"
    if wd.get("runner_identity_error"):
        alive += f" ({wd['runner_identity_error']})"
    return (
        f"t={wd.get('elapsed')}s "
        f"remaining={wd.get('remaining')}s "
        f"gpu={gpu} "
        f"disk_free={wd.get('disk_free_bytes')} "
        f"runner_alive={alive}"
    )


def await_runner_result(transport, job_id: str, wait: int):
    """Poll for the runner's result.json for up to `wait` seconds.

    Returns `("result", result)`, or `(None, why)` with a sentence saying
    why no result arrived: the wait ran out, the runner is dead or never
    started, the assignment is gone, or reading the VM failed.
    """
    polls = math.ceil(wait / RUNNER_RESULT_POLL_SECONDS) if wait > 0 else 0
    unreadable = 0
    for _ in range(polls):
        time.sleep(RUNNER_RESULT_POLL_SECONDS)
        try:
            kind, payload = observe_remote(transport, job_id)
        except Exception as error:  # noqa: BLE001 - waiting must not block teardown
            return None, f"reading the runner's records failed ({describe_error(error)})"
        if kind == "result":
            return "result", payload
        if kind == "runner_dead":
            return None, "the runner is dead and wrote no result.json"
        if kind == "never_started":
            return None, "the runner never started (no launch.json)"
        if kind == "session_lost":
            return None, "the assignment disappeared while waiting for the runner"
        if kind in ("degraded", "launch_invalid"):
            unreadable += 1
    if polls and unreadable == polls:
        return None, (
            f"no result.json could be read within {wait}s of the cancel request: "
            f"every one of {polls} reads of the runner's records failed"
        )
    return None, f"the runner wrote no result.json within {wait}s of the cancel request"


def raw_verdict(result: dict) -> str:
    """The verdict fields of a result.json, for a reason when the result
    itself cannot be absorbed."""
    fields = ("schema_version", "workload", "exit_code", "signal", "offload", "phase")
    return " ".join(f"{name}={result.get(name)!r}" for name in fields if name in result)


def _listed_endpoint(assignment) -> Optional[str]:
    if isinstance(assignment, dict):
        return assignment.get("endpoint")
    return getattr(assignment, "endpoint", None)


def copy_vm_records(
    transport,
    store: JobStore,
    job_id: str,
    *,
    budget: float = VM_RECORD_COPY_BUDGET_SECONDS,
) -> str:
    """Copy the job's `VM_RECORD_FILES` into its local job directory.

    Called before every release of a job VM, whatever asked for it: once
    the VM is gone these files are the only account of what happened.
    Files the VM does not have are skipped. Copying stops when the session
    is lost, and no new read starts after `budget` seconds, so an
    unreachable VM cannot hold up its own release for long; a read already
    in progress runs to the transport's own timeout. Never raises; returns an envelope hint that
    names what was copied and where, or why copying stopped.
    """
    local_dir = store.job_dir(job_id)
    copied: List[str] = []
    unreadable: List[str] = []
    stopped = None
    deadline = time.monotonic() + budget
    for name in VM_RECORD_FILES:
        if time.monotonic() > deadline:
            stopped = f"{budget:.0f}s budget spent"
            break
        try:
            text, status = transport.read_text(f"{REMOTE_ROOT}/{job_id}/{name}")
            status_name = getattr(status, "name", str(status))
            if status_name == "NOT_FOUND":
                continue
            if status_name == "SESSION_LOST":
                stopped = "session_lost"
                break
            if status_name != "OK" or text is None:
                unreadable.append(name)
                continue
            local_dir.mkdir(parents=True, exist_ok=True)
            (local_dir / name).write_text(text)
            copied.append(name)
        except Exception as e:  # noqa: BLE001 - must never block a release
            stopped = describe_error(e)
            break
    hint = (
        f"VM records copied before release to {local_dir}: "
        f"{', '.join(copied) if copied else 'none'}"
    )
    if unreadable:
        hint += f"; unreadable: {', '.join(unreadable)}"
    if stopped is not None:
        hint += f"; copying stopped ({stopped})"
    return hint


def _tail(text: str, n: int = 600, *, phase: str = "") -> str:
    """Truncate `text` for the envelope's `reason`, but never lose it: pip's
    (and similar tools') generic boilerplate ("did not run successfully",
    "This error originates from a subprocess...") is often the *last* few
    hundred characters regardless of which package or line actually failed,
    so a naive tail keeps the one part that is the same for every failure
    and discards the one part that names the cause. Log the untruncated
    text to the persistent rotating log (~/.config/colab-cli/colab.log,
    wired up in common.py:setup_logging) before slicing.
    """
    if len(text) > n:
        _logger.info("full %s output:\n%s", phase or "phase", text)
    return text[-n:] if len(text) > n else text

