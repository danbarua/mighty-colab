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

"""`mighty-colab job` -- plan / apply / status / destroy.

Terraform's verbs, deliberately: an agent that has ever driven `terraform`
already knows that `plan` is free and side-effect-free, that `apply` is the
only thing that spends money, and that `destroy` is unconditional. Inventing
a novel vocabulary here would buy nothing.

The analogy stops at one place, and it matters: `apply` does **not** loop
until the world matches the spec. A training run is not desired state. It
runs once, and `status` reports what happened rather than what is still
missing.
"""

import datetime
import json
import logging
import math
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import typer
from typing_extensions import Annotated

from colab_cli.common import build_envelope, emit_json
from colab_cli.envelopes import (
    JobApplyAsyncStarted,
    JobApplyRefusedEnvelope,
    JobEnvelopeWrapper,
    JobListEnvelope,
    JobPlanEnvelope,
    JobPruneEnvelope,
)
from colab_cli.job.models import (
    Cleanup,
    JobEnvelope,
    Offload,
    Phase,
    RetryClass,
    Supervisor,
    Workload,
)
from colab_cli.job.orchestrator import (
    Orchestrator,
    PhaseError,
    RUN_DEADLINE_MARGIN_SECONDS,
    RUNNER_RESULT_POLL_SECONDS,
    RUNNER_STOP_WAIT_SECONDS,
    WATCHDOG_STALLED_PREFIX,
    WatchdogStaleness,
    _now,
    await_runner_result,
    copy_vm_records,
    deadline_reason,
    degraded_reason,
    observe_remote,
    raw_verdict,
    keep_vm,
    record_vm_kept,
    pull_runner_log,
    release_assignment,
    replace_hint,
    request_cancel,
    run_deadline_seconds,
    stop_session_keep_alive,
)
from colab_cli.job.runtime_payload import ident
from colab_cli.job.runtime_payload.redact import describe_error
from colab_cli.job.spec_io import fetch_control_result, validation_messages
from colab_cli.job.store import (
    ApplyInProgress,
    JobStore,
    load_plan_file,
    write_plan_file,
)

_logger = logging.getLogger(__name__)
job_app = typer.Typer(
    help="Run an unattended job on a Colab VM: plan, apply, status, destroy.",
    no_args_is_help=True,
)
jobs_app = typer.Typer(
    help="Manage local job records as a collection: list, prune.",
    no_args_is_help=True,
)

class SupervisorStopped(BaseException):
    """apply was asked to stop by SIGTERM or SIGHUP: how an agent harness
    ends a tool call that ran too long, and what a closed terminal sends.
    Python's default for both exits at once with no cleanup; apply handles
    them like Ctrl-C."""

    def __init__(self, signal_name: str):
        super().__init__(signal_name)
        self.signal_name = signal_name


def _stop_supervisor(signum, _frame):
    # A second signal must not abort the cleanup the first one started.
    signal.signal(signum, signal.SIG_IGN)
    raise SupervisorStopped(signal.Signals(signum).name)


def _store() -> JobStore:
    """Job records live beside the session store, and follow `--config`.

    `state.config_path` is None unless `--config` was passed, which is the
    normal case -- mirror `StateStore`'s own default rather than assuming
    a path is set. Honouring `--config` matters for the same reason it does
    for sessions: a test or a second agent pointed at a scratch config must
    not write into the developer's real job history.
    """
    from colab_cli.common import state
    if state.config_path:
        root = Path(state.config_path).parent / "jobs"
    else:
        root = Path(os.path.expanduser("~/.config/colab-cli")) / "jobs"
    return JobStore(root)


def _new_job_id(name: str) -> str:
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{name}-{stamp}-{uuid.uuid4().hex[:6]}"


def spawn_apply_async(
    *,
    plan_file: Optional[str],
    job_id_opt: Optional[str],
    timeout: Optional[int],
    leave_up: bool,
    log_path: str,
    auth_provider=None,
    config_path: Optional[str] = None,
) -> int:
    """Spawns a detached `job apply` with stdio redirected to a log file.

    Re-invokes the real (synchronous) `job apply` as the child rather than
    a bespoke worker, for the same reason `spawn_exec_async` does: every
    existing guarantee (plan-hash revalidation, expiry checks, the apply
    lock, unconditional cleanup in `finally`) keeps working unmodified.
    The only difference from a foreground `apply` is where stdout/stderr
    land, and that this process returns before the child does.

    `auth_provider`/`config_path` are propagated as global flags: the
    detached child re-parses argv from scratch and does not inherit the
    parent's parsed Typer flags (AGENTS.md item 16). The child never gets
    `--async` itself -- it must run the real, blocking lifecycle.
    """
    args = ["job", "apply"]
    if plan_file:
        args.append(plan_file)
    if job_id_opt:
        args.extend(["--job-id", job_id_opt])
    if timeout is not None:
        args.extend(["--timeout", str(timeout)])
    if leave_up:
        args.append("--leave-up")
    return _spawn_detached(args, log_path, auth_provider, config_path)


def spawn_status_poll(
    *, job_id: str, log_path: str, auth_provider=None, config_path: Optional[str] = None
) -> int:
    """Spawns a detached `job status JOB_ID --poll`: the supervisor that
    finishes an orphaned job, collecting its result and releasing the VM
    when it ends. apply hands a launched job to it when stopped by SIGTERM
    or SIGHUP, so nobody has to come back for the VM."""
    return _spawn_detached(
        ["job", "status", job_id, "--poll"], log_path, auth_provider, config_path
    )


def _spawn_detached(args, log_path: str, auth_provider, config_path) -> int:
    """Runs `mighty-colab ARGS` as a detached process with stdio sent to
    `log_path`, propagating the global flags it does not inherit."""
    cmd = [sys.executable, "-m", "colab_cli.cli"]
    if auth_provider is not None:
        cmd.append(f"--auth={auth_provider.value}")
    if config_path is not None:
        cmd.extend(["--config", config_path])
    cmd.extend(args)

    kwargs = {}
    if sys.platform != "win32":
        kwargs["start_new_session"] = True
    else:
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        DETACHED_PROCESS = 0x00000008
        kwargs["creationflags"] = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP

    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    log_fp = open(log_path, "wb")
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=log_fp,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            **kwargs,
        )
    finally:
        log_fp.close()
    return proc.pid


def _emit(env: JobEnvelope, command: str, exit_code: int = 0) -> None:
    """Emit a job verdict without confusing it with the CLI transaction.

    The outer status is the CLI-transaction field (did this invocation work),
    which is not the same question as whether the job succeeded. A successful
    job status call reporting a failed run exits zero, while job apply exits
    non-zero when the run it performed failed.
    """
    from colab_cli.common import state

    if state.json_output:
        emit_json(
            build_envelope(
                status="error" if exit_code else "ok",
                command=f"job {command}",
                exit_code=exit_code,
                job=json.loads(env.model_dump_json()),
                done=env.done,
                ok=env.ok,
            ),
            JobEnvelopeWrapper,
        )
    else:
        typer.echo(_human(env))


def _emit_command_message(
    command: str,
    message: str,
    *,
    exit_code: int = 1,
    reason: str | None = None,
) -> None:
    """Emit one validated envelope, or the equivalent human message."""
    from colab_cli.common import state

    if state.json_output:
        emit_json(
            build_envelope(
                status="error" if exit_code else "ok",
                command=f"job {command}",
                exit_code=exit_code,
                reason=reason,
                message=message,
            )
        )
    else:
        typer.echo(message, err=bool(exit_code))


def _human(env: JobEnvelope) -> str:
    lines = [
        f"[job] {env.job_id}",
        f"  phase:      {env.phase.value}",
        *([f"  failed in:  {env.failed_phase.value}"] if env.failed_phase else []),
        f"  workload:   {env.workload.value}"
        + (f" (exit {env.exit_code})" if env.exit_code is not None else "")
        + (f" (signal {env.signal})" if env.signal else ""),
        f"  offload:    {env.offload.value}",
        f"  cleanup:    {env.cleanup.value}",
        f"  supervisor: {env.supervisor.value}",
        f"  done={env.done} ok={env.ok}",
    ]
    if env.endpoint:
        lines.append(f"  endpoint:   {env.endpoint}")
    if env.actual_accelerator:
        lines.append(
            f"  accel:      requested={env.requested_accelerator} "
            f"actual={env.actual_accelerator}"
        )
    if env.exception:
        lines.append(
            f"  exception:  {env.exception.get('type')}: {env.exception.get('message')}"
        )
    for i in env.inputs:
        if i.error is not None:
            lines.append(f"  input:      {i.dest} -> {i.status}: {i.error.summary}")
            if i.error.body:
                lines.append(f"              response body: {' '.join(i.error.body.split())}")
    if env.artifacts:
        for a in env.artifacts:
            if a.error is None:
                lines.append(f"  artifact:   {a.path} -> {a.status}")
                continue
            lines.append(f"  artifact:   {a.path} -> {a.status}: {a.error.summary}")
            if a.error.body:
                body = " ".join(a.error.body.split())
                lines.append(f"              response body: {body}")
    if env.retry_class:
        lines.append(f"  retry:      {env.retry_class.value}")
    if env.reason:
        lines.append(f"  reason:     {env.reason}")
    for h in env.hints:
        lines.append(f"  hint:       {h}")
    return "\n".join(lines)


# --------------------------------------------------------------------------


def _emit_spec_errors(exc) -> None:
    """Render pydantic validation failures without reflecting spec values."""
    from colab_cli.common import state
    diags = [
        {
            "severity": "error",
            "code": "spec_invalid",
            "message": message,
            "retry_class": "fix_code",
            "hint": None,
        }
        for message in validation_messages(exc)
    ]
    if state.json_output:
        emit_json(
            build_envelope(
                status="error",
                command="job plan",
                exit_code=1,
                reason="the spec does not validate",
                diagnostics=diags,
            ),
            JobPlanEnvelope,
        )
    else:
        for d in diags:
            typer.echo(f"  ERROR {d['code']}: {d['message']}", err=True)


def _refuse_plan(job_id: str, message: str, diagnostics) -> None:
    """Refuse to apply before assignment, with the diagnostics that decided
    it in the `--json` envelope or on stderr."""
    from colab_cli.common import state

    if state.json_output:
        emit_json(
            build_envelope(
                status="error",
                command="job apply",
                exit_code=1,
                reason="plan_refused",
                message=message,
                job_id=job_id,
                diagnostics=[json.loads(d.model_dump_json()) for d in diagnostics],
            ),
            JobApplyRefusedEnvelope,
        )
    else:
        for d in diagnostics:
            typer.echo(f"  {d.severity.upper()} {d.code}: {d.message}", err=True)
        typer.echo(message, err=True)
    raise typer.Exit(1)


def plan(
    spec_file: Annotated[str, typer.Argument(help="Path to the job spec (YAML or JSON)")],
    out: Annotated[
        Optional[str], typer.Option("--out", help="Write the plan JSON here")
    ] = None,
    no_probe: Annotated[
        bool, typer.Option("--no-probe", help="Skip network probes of data URLs")
    ] = False,
):
    """Validate a spec and report what `apply` would do. Allocates nothing."""
    from colab_cli.common import state
    from pydantic import ValidationError

    from colab_cli.job.planner import build_plan
    from colab_cli.job.spec_io import load_spec, plan_hash

    try:
        spec = load_spec(spec_file)
    except ValidationError as e:
        # A malformed spec is a diagnostic, not a traceback. Model
        # invariants (a `control.result` with only one of its two URLs, an
        # absolute `entry`) are enforced by pydantic rather than duplicated
        # as planner checks -- but the caller still deserves the same
        # `code: message` surface as every other plan error, because the
        # agent reading this output cannot parse a stack trace into a fix.
        _emit_spec_errors(e)
        raise typer.Exit(1) from None
    except OSError as e:
        _emit_command_message(
            "plan",
            f"[colab] Could not read spec {spec_file!r}: {describe_error(e)}",
            reason="spec_unreadable",
        )
        raise typer.Exit(1) from None
    except ValueError as e:
        # load_spec's own messages: the YAML problem and its position, a
        # non-mapping top level, a redacted record used as a spec.
        _emit_command_message(
            "plan",
            f"[colab] Could not read spec {spec_file!r}: {e}",
            reason="spec_unreadable",
        )
        raise typer.Exit(1) from None
    job_id = _new_job_id(spec.name)
    source_spec_path = str(Path(spec_file).expanduser().resolve(strict=False))
    p = build_plan(
        spec,
        job_id,
        probe=not no_probe,
        source_spec_path=source_spec_path,
    )
    p.spec_hash = plan_hash(p.spec, p.source_spec_path, p.source_files)

    store = _store()
    store.write_spec(job_id, spec)
    store.write_plan(p)
    if out:
        write_plan_file(out, p)

    errors = [d for d in p.diagnostics if d.severity == "error"]
    warnings = [d for d in p.diagnostics if d.severity == "warn"]

    if state.json_output:
        emit_json(
            build_envelope(
                status="error" if errors else "ok",
                command="job plan",
                exit_code=1 if errors else 0,
                job_id=job_id,
                spec_hash=p.spec_hash,
                plan_path=str(store.job_dir(job_id) / "plan.json"),
                diagnostics=[json.loads(d.model_dump_json()) for d in p.diagnostics],
            ),
            JobPlanEnvelope,
        )
    else:
        typer.echo(f"[job] plan {job_id}  spec_hash={p.spec_hash[:12]}")
        for d in p.diagnostics:
            typer.echo(f"  {d.severity.upper():5s} {d.code}: {d.message}")
            if d.hint:
                typer.echo(f"        hint: {d.hint}")
        if warnings and not errors:
            typer.echo(f"  {len(warnings)} warning(s); apply will refuse without "
                       "`ignore_warnings: true` in the spec")
        elif not errors:
            typer.echo("  no errors or warnings; ready to apply")
    if errors:
        raise typer.Exit(1)


def apply(
    plan_file: Annotated[
        Optional[str],
        typer.Argument(help="Path to a plan.json (omit to use --job-id)"),
    ] = None,
    job_id_opt: Annotated[
        Optional[str], typer.Option("--job-id", help="Apply a previously-written plan")
    ] = None,
    timeout: Annotated[
        Optional[int],
        typer.Option(
            "--timeout",
            help=(
                "Local supervisor budget (s), default wall_clock + 600. When it "
                "passes with no verdict, the job is cancelled, its result "
                "collected if it arrives, and the VM released."
            ),
        ),
    ] = None,
    leave_up: Annotated[
        bool, typer.Option("--leave-up", help="Do not unassign the VM when finished")
    ] = False,
    run_async: Annotated[
        bool,
        typer.Option(
            "--async",
            help="Spawn apply as a detached background process and return immediately",
        ),
    ] = False,
):
    """Execute a plan: provision through teardown.

    Refuses a plan.json whose embedded spec no longer matches its recorded hash.
    """
    from colab_cli.common import state
    from colab_cli.job.planner import revalidate_expiry
    from colab_cli.job.spec_io import plan_hash
    from colab_cli.job.transport import JobTransport

    store = _store()

    if run_async:
        # Resolve just enough to know where the log goes and what to
        # report -- not enough to duplicate any real validation. The
        # detached child re-runs this same function without --async and
        # does every check (plan-hash revalidation, expiry, the apply
        # lock, plan errors/warnings) for real; this process never claims
        # the apply lock, so there is nothing to race or double-release.
        if job_id_opt:
            job_id = job_id_opt
        elif plan_file:
            try:
                job_id = load_plan_file(plan_file, hydrate=False).job_id
            except ValueError as e:
                _emit_command_message(
                    "apply",
                    f"[colab] Could not load protected plan: {e}",
                    reason="plan_unreadable",
                )
                raise typer.Exit(1) from None
        else:
            _emit_command_message(
                "apply",
                "[colab] Pass a plan file or --job-id.",
                reason="usage_error",
            )
            raise typer.Exit(1)
        log_path = str(store.job_dir(job_id) / "apply.log")
        pid = spawn_apply_async(
            plan_file=plan_file,
            job_id_opt=job_id_opt,
            timeout=timeout,
            leave_up=leave_up,
            log_path=log_path,
            auth_provider=state.auth_provider,
            config_path=state.config_path,
        )
        if state.json_output:
            emit_json(
                build_envelope(
                    status="ok",
                    command="job apply",
                    job_id=job_id,
                    pid=pid,
                    log_path=log_path,
                ),
                JobApplyAsyncStarted,
            )
        else:
            typer.echo(f"[job] apply started in background: {job_id} (pid {pid})")
            typer.echo(f"  log:    {log_path}")
            typer.echo(f"  status: mighty-colab job status {job_id} --poll")
        return


    try:
        if plan_file:
            p = load_plan_file(plan_file, hydrate=True)
        elif job_id_opt:
            p = store.read_plan_for_apply(job_id_opt)
            if p is None:
                _emit_command_message(
                    "apply",
                    f"[colab] No plan found for job {job_id_opt!r}.",
                    reason="plan_not_found",
                )
                raise typer.Exit(1)
        else:
            _emit_command_message(
                "apply",
                "[colab] Pass a plan file or --job-id.",
                reason="usage_error",
            )
            raise typer.Exit(1)
    except ValueError as e:
        # The store's messages name the problem without plan values.
        _emit_command_message(
            "apply",
            f"[colab] Could not load protected plan: {e}",
            reason="plan_unreadable",
        )
        raise typer.Exit(1) from None

    # Integrity of `plan.json` itself -- NOT drift from the user's YAML.
    # `apply` never re-reads the spec file: the plan embeds the spec it
    # captured, so editing `spec.yaml` afterwards cannot affect this run.
    # What can happen is the plan file being hand-edited or truncated, and
    # then `spec` and `spec_hash` disagree. That matters because `apply`
    # does not re-run the plan-time gates (unknown accelerator, path
    # escape, non-HTTPS URL), so an edited plan would walk straight past
    # them.
    #
    # Re-derive rather than trust the recorded value: a self-certifying
    # document certifies nothing. The hash canonicalises URL query strings
    # out, so re-signing the same object does not trip this, while
    # pointing at a different object does.
    if p.spec.code.kind == "bundle" and p.source_spec_path is None:
        _emit_command_message(
            "apply",
            "[colab] This bundle plan lacks its protected source exclusion. "
            "Re-run job plan to produce a fresh one.",
            reason="plan_refused",
        )
        raise typer.Exit(1)

    actual = plan_hash(p.spec, p.source_spec_path, p.source_files)
    if actual != p.spec_hash:
        _emit_command_message(
            "apply",
            "[colab] This plan file is inconsistent: its spec does not match "
            f"its own recorded hash (recorded {p.spec_hash[:12]}, spec hashes "
            f"to {actual[:12]}). The plan was modified or truncated after it "
            "was written. Re-run job plan to produce a fresh one.",
            reason="plan_refused",
        )
        raise typer.Exit(1)

    # The plan's own errors first: they already say why, for example why
    # the source lock could not be built, which the source check below
    # would only report as a missing lock.
    if p.has_errors:
        _refuse_plan(
            p.job_id,
            "[colab] This plan has errors and will not be applied. "
            "Fix the spec and re-plan.",
            [d for d in p.diagnostics if d.severity == "error"],
        )
    if p.has_warnings and not p.spec.ignore_warnings:
        _refuse_plan(
            p.job_id,
            "[colab] This plan has warnings. Set ignore_warnings: true in the "
            "spec to accept them explicitly.",
            [d for d in p.diagnostics if d.severity == "warn"],
        )

    from colab_cli.job.payload_bundle import CONTENTS_UPLOAD_CEILING, verify_source_files

    try:
        verify_source_files(p.spec, p.source_files, p.source_spec_path)
    except ValueError as error:
        _emit_command_message(
            "apply", f"[colab] {error}", reason="plan_refused"
        )
        raise typer.Exit(1) from None
    except OSError as error:
        # A locked source file that is gone or unreadable since planning.
        _emit_command_message(
            "apply",
            f"[colab] A source file in the plan cannot be read: {describe_error(error)}. "
            "Restore it, or re-run job plan.",
            reason="plan_refused",
        )
        raise typer.Exit(1) from None
    oversized = [
        item.path
        for item in (p.source_files or [])
        if item.size_bytes > CONTENTS_UPLOAD_CEILING
    ]
    if oversized:
        _emit_command_message(
            "apply",
            "[colab] Source files exceed the 250 MB Contents ceiling: "
            + ", ".join(oversized)
            + ". Move them to a data URL and re-plan.",
            reason="plan_refused",
        )
        raise typer.Exit(1)


    # Expiry is revalidated here, not trusted from plan time: a plan is
    # durable and may be applied long after its signatures were minted.
    # Checked BEFORE assign, because failing after costs a VM.
    expired = revalidate_expiry(p)
    if expired:
        _refuse_plan(
            p.job_id,
            "[colab] Signed URLs in this plan have expired or will expire before "
            "the job needs them. Re-sign and re-plan.",
            expired,
        )

    from colab_cli.runtime import ColabRuntime

    try:
        claim = store.claim_apply(
            p.job_id,
            pid=os.getpid(),
            starttime=ident.starttime(os.getpid()),
            boot_id=ident.boot_id(),
        )
    except ApplyInProgress as error:
        _emit_command_message(
            "apply", f"[colab] {error}", reason="apply_in_progress"
        )
        raise typer.Exit(1) from None

    existing = store.read_envelope(p.job_id)
    if existing is not None and existing.endpoint:
        claim.release()
        _emit_command_message(
            "apply",
            f"[colab] Job {p.job_id} already has endpoint {existing.endpoint}. "
            "Use `mighty-colab job status --poll` instead of a second apply.",
            reason="job_already_active",
        )
        raise typer.Exit(1)

    orch = Orchestrator(
        plan=p,
        store=store,
        client=state.client,
        runtime_factory=lambda url, token: ColabRuntime(url, token),
        transport_factory=lambda s: JobTransport(s, state.client, state.store),
        session_store=state.store,
        emit=lambda m: typer.echo(m),
        auth_provider=state.auth_provider,
        config_path=state.config_path,
    )
    # Recorded before the first envelope write, so a detached
    # `job status --poll` that finishes the job honours it too.
    orch.env.leave_up = leave_up


    # An explicit --timeout bounds this whole call from now (an agent's
    # tool-call limit). The default bounds the run: it starts at launch, so
    # provisioning and a long install do not eat into the run's wall_clock.
    deadline = time.time() + timeout if timeout else None
    secret_handoff = False
    previous_handlers = {}
    hand_off_log = None
    for stop_signal in (signal.SIGTERM, signal.SIGHUP):
        try:
            previous_handlers[stop_signal] = signal.signal(stop_signal, _stop_supervisor)
        except ValueError as error:  # only the main thread can install handlers
            _logger.warning(
                "job apply %s: %s not handled (%s); it would exit without cleanup",
                p.job_id,
                stop_signal.name,
                error,
            )
    try:
        orch.provision()
        orch.install()
        orch.restart()
        orch.verify()
        _stage_payload(orch, p)
        orch.seal_secret_channel()
        launched_pid = orch.launch(f"{orch.remote_dir}/src")
        # Only a pid proves the launch kernel opened and unlinked the
        # credential handoff file; otherwise cleanup checks for it.
        secret_handoff = launched_pid is not None
        transport = orch.job_transport()
        if deadline is None:
            deadline = time.time() + run_deadline_seconds(p.spec)
            passed = (
                f"no verdict within {run_deadline_seconds(p.spec)}s of launch "
                f"(wall_clock {p.spec.budgets.wall_clock}s + "
                f"{RUN_DEADLINE_MARGIN_SECONDS}s)"
            )
            requester = "job apply deadline"
        else:
            passed = f"apply's --timeout of {timeout}s passed before a verdict"
            requester = "job apply --timeout"
        orch.poll(transport, deadline=deadline)
        if orch.env.supervisor is Supervisor.INTERRUPTED:
            # poll's deadline passed with no verdict.
            orch.cancel_after_deadline(transport, passed, requester)
    except PhaseError as e:
        orch.env.record_failure(e.phase)
        orch.env.finished_at = orch.env.finished_at or _now()
        orch.env.workload = Workload.FAILED
        orch.env.offload = (
            Offload.NOT_REQUIRED if not p.spec.artifacts else Offload.SKIPPED
        )
        orch.env.supervisor = Supervisor.FINISHED
        orch.env.reason = e.reason
        orch.env.retry_class = e.retry_class
        orch.env.hints.extend(e.hints)
        orch.env.phase = e.phase
    except (KeyboardInterrupt, SupervisorStopped) as stop:
        how = (
            "Ctrl-C (SIGINT)"
            if isinstance(stop, KeyboardInterrupt)
            else stop.signal_name
        )
        if orch.env.workload is Workload.PENDING:
            # Nothing runs on the VM before launch: release it.
            orch.env.workload = Workload.CANCELLED
            orch.env.offload = (
                Offload.NOT_REQUIRED if not p.spec.artifacts else Offload.SKIPPED
            )
            orch.env.supervisor = Supervisor.FINISHED
            orch.env.finished_at = _now()
            orch.env.reason = (
                f"interrupted locally by {how} during {orch.env.phase.value}, "
                "before the runner was launched; the VM is released"
            )
            orch.env.retry_class = RetryClass.RETRY_SAME
        elif isinstance(stop, SupervisorStopped):
            # SIGTERM (an agent's tool-call limit) or SIGHUP (a closed
            # terminal): whoever started apply may not come back. A
            # detached `job status --poll` takes over after this process
            # has cleared its supervisor identity (below).
            hand_off_log = str(store.job_dir(p.job_id) / "status-poll.log")
            orch.env.supervisor = Supervisor.INTERRUPTED
            orch.env.reason = (
                f"stopped by {how} after the runner was launched; the job keeps "
                "running, and a detached `job status --poll` collects the result "
                "and releases the VM when it ends"
            )
            orch.env.retry_class = None
            orch.env.hints.extend(
                [
                    f"the VM is still billing: the detached poll logs to {hand_off_log}",
                    f"the VM is still billing: `mighty-colab job destroy {p.job_id}` "
                    "stops the job and releases the VM now",
                ]
            )
        else:
            # Ctrl-C: the person who pressed it comes back. The detached
            # runner keeps going and cleanup stays pending, so `job status
            # --poll` collects the result and releases the VM.
            orch.env.supervisor = Supervisor.INTERRUPTED
            orch.env.reason = (
                f"interrupted locally by {how} after the runner was launched; the "
                "job keeps running on the VM, which bills until the job is released"
            )
            orch.env.retry_class = None
            # "still billing" lets _finalize_hints drop these once the VM
            # has been released.
            orch.env.hints.extend(
                [
                    f"the VM is still billing: `mighty-colab job status {p.job_id} "
                    "--poll` collects the result and releases the VM when the job ends",
                    f"the VM is still billing: `mighty-colab job destroy {p.job_id}` "
                    "stops the job and releases the VM now",
                ]
            )
    except Exception as e:  # noqa: BLE001 - every non-debug path needs a verdict
        if state.debug:
            raise
        orch.env.record_failure(orch.env.phase)
        orch.env.finished_at = orch.env.finished_at or _now()
        if not orch.env.workload.terminal:
            before_run = orch.env.phase in {
                Phase.PLAN,
                Phase.PROVISION,
                Phase.INSTALL,
                Phase.RESTART,
                Phase.VERIFY,
                Phase.STAGE,
            }
            orch.env.workload = Workload.FAILED if before_run else Workload.UNKNOWN
        if not orch.env.offload.terminal:
            if not p.spec.artifacts:
                orch.env.offload = Offload.NOT_REQUIRED
            elif orch.env.phase is Phase.OFFLOAD:
                orch.env.offload = Offload.FAILED
            else:
                orch.env.offload = Offload.SKIPPED
        orch.env.supervisor = Supervisor.FINISHED
        orch.env.reason = (
            f"internal supervisor failure in {orch.env.phase.value}: {describe_error(e)}"
        )
        orch.env.retry_class = RetryClass.DO_NOT_RETRY
        # `--debug` only helps while the exception is in flight (it makes
        # this except-clause re-raise instead of swallowing) -- once the
        # job is terminal there is nothing left to re-run: `job apply` on
        # the same --job-id refuses (endpoint already assigned) and `job
        # status --debug` never re-enters this code path at all. Log the
        # traceback to the persistent rotating file every invocation
        # already writes to (see common.py:setup_logging) and point the
        # hint there instead of promising a re-run that can't work.
        _logger.exception(
            "job apply supervisor failure for job %s", p.job_id
        )
        orch.env.hints.append(
            "local traceback logged to ~/.config/colab-cli/colab.log"
        )
    finally:
        secret_removed = secret_handoff or orch.cleanup_secret_channel()
        # Teardown is how you leave, not a phase you reach: an early
        # failure must still release the VM, or the cost of a typo is an
        # A100 left assigned. An unconfirmed credential deletion also
        # overrides every leave-up request.
        keep = keep_vm(orch.env, p.spec, secret_removed=secret_removed)
        if not secret_removed:
            # A cleanup event, not the job's verdict: the workload's own
            # reason and retry advice stay. The release removes the secret
            # with the VM.
            if orch.env.reason is None:
                orch.env.reason = "transfer credential deletion could not be confirmed"
            if orch.env.retry_class is None:
                orch.env.retry_class = RetryClass.DO_NOT_RETRY
            orch.env.hints.append(
                "transfer credential deletion could not be confirmed"
                + (
                    f" ({orch.secret_cleanup_problem})"
                    if orch.secret_cleanup_problem
                    else ""
                )
                + "; forced VM teardown to remove the secret"
            )
        if orch.env.supervisor is Supervisor.INTERRUPTED and secret_removed:
            # Interrupted after launch: the run is still going on the VM.
            # Cleanup stays pending (not left_up), so `job status --poll`
            # treats the job as orphaned, absorbs its result and releases.
            orch.detach()
            store.write_envelope(orch.env)
        else:
            orch.cleanup(leave_up=keep)
        store.clear_supervisor_identity(p.job_id)
        claim.release()
        if hand_off_log is not None and orch.env.cleanup is Cleanup.PENDING:
            # Only now: a detached poll that saw this process's supervisor
            # identity would treat the job as supervised and never release.
            try:
                spawn_status_poll(
                    job_id=p.job_id,
                    log_path=hand_off_log,
                    auth_provider=state.auth_provider,
                    config_path=state.config_path,
                )
            except Exception as error:  # noqa: BLE001 - the job stays recoverable
                orch.env.hints.append(
                    f"the VM is still billing: the detached poll could not start "
                    f"({describe_error(error)}); run `mighty-colab job status "
                    f"{p.job_id} --poll` or `mighty-colab job destroy {p.job_id}`"
                )
                store.write_envelope(orch.env)
        for stop_signal, handler in previous_handlers.items():
            signal.signal(stop_signal, handler)

    _emit(orch.env, "apply", exit_code=0 if orch.env.ok else 1)
    if not orch.env.ok:
        raise typer.Exit(1)


def _stage_payload(orch: Orchestrator, p) -> None:
    """Upload public payload files, then the private transfer channel."""

    from colab_cli.job.payload_bundle import stage_payload
    from colab_cli.job.transport import ReadStatus, TransportError

    orch._set_phase(Phase.STAGE)
    result_channel = p.spec.control.result
    if p.spec.data or p.spec.artifacts or (result_channel and result_channel.put_url):
        orch.prepare_secret_channel()
    try:
        stage_payload(
            spec=p.spec,
            job_id=p.job_id,
            transport=orch.job_transport(),
            remote_dir=orch.remote_dir,
            source_spec_path=p.source_spec_path,
            source_files=p.source_files,
        )
    except TransportError as error:
        retry = (
            RetryClass.RETRY_DIFFERENT
            if error.status is ReadStatus.SESSION_LOST
            else RetryClass.RETRY_SAME
        )
        raise PhaseError(Phase.STAGE, str(error), retry) from error
    except ValueError as error:
        # payload_bundle's own refusals: a source file changed or appeared
        # after planning, a credential-bearing URL in the code, a symlink,
        # a file over the upload limit. Each names the file and the fix.
        raise PhaseError(
            Phase.STAGE, f"staging refused: {describe_error(error)}", RetryClass.FIX_CODE
        ) from error
    except OSError as error:
        raise PhaseError(
            Phase.STAGE,
            f"a local source file could not be read: {describe_error(error)}",
            RetryClass.FIX_HUMAN,
        ) from error




def _scrub_transfer_secret(transport, job_id: str) -> tuple[bool, Optional[str]]:
    """(removed, why not): remove the transfer credential file on the VM."""
    path = f"/content/jobs/{job_id}/mighty_runtime/.secrets/transfer.json"
    try:
        status = transport.remove(path)
    except Exception as error:  # noqa: BLE001 - callers enforce teardown on uncertainty
        return False, f"removing it failed ({describe_error(error)})"
    if status.name == "OK":
        return True, None
    return False, f"removing it returned {status.name}"


def _read_off_vm_result(store: JobStore, job_id: str) -> tuple[Optional[dict], Optional[str]]:
    """(terminal result, None), (None, None) when no control.result is
    configured, or (None, why) when it could not be used."""
    from colab_cli.job.runtime_payload.redact import redact_url
    from colab_cli.job.spec_io import url_id

    try:
        plan = store.read_plan_for_apply(job_id)
    except ValueError as error:
        return None, f"the plan could not be loaded ({error})"
    channel = plan.spec.control.result if plan is not None else None
    if channel is None:
        return None, None
    try:
        result = fetch_control_result(channel.get_url)
    except Exception as error:  # noqa: BLE001 - reported to the caller
        return None, redact_url(describe_error(error), channel.get_url, url_id(channel.get_url))
    workload = result.get("workload")
    if workload not in {w.value for w in Workload if w.terminal}:
        return None, f"it holds no terminal result (workload={workload!r})"
    return result, None




def _supervisor_alive(store, job_id: str) -> bool:
    """Whether the `job apply` process that owns this job is still running.

    A PID alone is not identity: after reuse, an unrelated process could be
    mistaken for the supervisor and a credential-bearing VM left alive.
    """
    identity = store.supervisor_identity(job_id)
    return bool(
        identity
        and ident.alive(identity["pid"], identity["starttime"], identity["boot_id"])
    )


def _await_supervisor_cleanup(store, job_id: str, wait: int):
    """Poll the local envelope while a live `job apply` acts on the cancel
    intent. Returns the envelope once its cleanup is terminal or the
    supervisor has exited, or None when `wait` seconds pass first."""
    polls = math.ceil(wait / RUNNER_RESULT_POLL_SECONDS) if wait > 0 else 0
    for _ in range(polls):
        time.sleep(RUNNER_RESULT_POLL_SECONDS)
        try:
            current = store.read_envelope(job_id)
        except Exception:  # noqa: BLE001 - a partly written envelope; poll again
            continue
        if current is not None and current.cleanup.terminal:
            return current
        if not _supervisor_alive(store, job_id):
            return current
    return None


def _copy_before_release(env, transport, store) -> None:
    """Copy the VM's records into the local job directory, then the
    caller releases the VM."""
    if not env.endpoint:
        return
    if transport is None:
        env.hints.append(
            "VM records not copied before release: no local session for this "
            "job, so the VM's files could not be read"
        )
        return
    env.hints.append(copy_vm_records(transport, store, env.job_id))


def _release(env, state, failure: str) -> None:
    """Unassign the job's VM and record the outcome in `env.cleanup`; on
    failure, a hint starting with `failure` keeps the error detail."""
    if not env.endpoint:
        env.cleanup = Cleanup.ALREADY_ABSENT
        return
    env.cleanup, detail = release_assignment(state.client, env.endpoint)
    if env.cleanup is Cleanup.FAILED:
        env.record_failure(Phase.CLEANUP)
    if detail:
        env.hints.append(
            f"{failure} ({detail}); endpoint {env.endpoint} may still be billing"
        )


def _forget_session(env, state) -> None:
    """Drop the local session record once the VM is confirmed gone."""
    if not env.session or env.cleanup is Cleanup.FAILED:
        return
    try:
        state.store.remove(env.session)
    except Exception as error:  # noqa: BLE001 - the release itself succeeded
        env.hints.append(
            f"local session record {env.session} not removed ({describe_error(error)})"
        )


def _force_release_unconfirmed_secret(
    env, state, store, action: str, transport=None, cause: Optional[str] = None
) -> None:
    """Release the VM because the transfer credential file could not be
    confirmed removed; `cause` says why not."""
    because = "transfer credential deletion could not be confirmed" + (
        f" ({cause})" if cause else ""
    )
    _copy_before_release(env, transport, store)
    _release(env, state, "forced unassign failed")
    _forget_session(env, state)
    if not env.workload.terminal:
        if env.reason:
            env.hints.append(f"before the forced teardown: {env.reason}")
        env.workload = Workload.UNKNOWN
        env.record_failure(Phase.CLEANUP)
        env.reason = f"forced teardown because {because}"
        env.retry_class = RetryClass.DO_NOT_RETRY
    else:
        env.hints.append(f"forced VM teardown because {because}")
    env.supervisor = Supervisor.FINISHED
    store.write_envelope(env)
    _emit(env, action, exit_code=1)
    raise typer.Exit(1)


def _absorb_remote_result(env, store, job_id, result) -> None:
    candidate = env.model_copy(deep=True)
    saved_plan = store.read_plan(job_id)
    if saved_plan is not None:
        Orchestrator.absorb_result(candidate, saved_plan.spec, result)
    else:
        Orchestrator.absorb_provenance(candidate, result)
        candidate.workload = Workload(result.get("workload", "unknown"))
        candidate.exit_code = result.get("exit_code")
        candidate.signal = result.get("signal")
        candidate.exception = result.get("exception")
    candidate = JobEnvelope.model_validate(
        {field: getattr(candidate, field) for field in JobEnvelope.model_fields}
    )
    for field in JobEnvelope.model_fields:
        setattr(env, field, getattr(candidate, field))


def _recover_off_vm_result(
    env: JobEnvelope, store: JobStore, job_id: str
) -> JobEnvelope | None:
    """A copy of `env` with the result from control.result absorbed, or
    None; when control.result is configured but unusable, `env` gets a
    hint saying why."""
    result, problem = _read_off_vm_result(store, job_id)
    if result is None:
        if problem:
            env.hints.append(f"control.result could not be used: {problem}")
        return None
    recovered = env.model_copy(deep=True)
    try:
        _absorb_remote_result(recovered, store, job_id, result)
    except Exception as error:  # noqa: BLE001 - optional recovery must not block teardown
        env.hints.append(
            f"control.result could not be absorbed ({describe_error(error)}); "
            f"raw result: {raw_verdict(result)}"
        )
        return None
    return recovered


# Substrings, not exact matches: every "still billing"/"left running"/
# "left up" hint in this file uses different wording (job-id, endpoint,
# and surviving-descendant details vary per call site), so this can't be
# a fixed set of strings.
_STALE_BILLING_HINT_MARKERS = ("still billing", "left running", "left up")


def _finalize_hints(env) -> None:
    """Call once, right before persisting/emitting a `status`/`destroy`
    envelope -- never while a job is still in flight.

    Two independent problems, one fix point:

    1. `env.hints` is loaded from the *previous* persisted envelope and
       every call site downstream does `.append(...)` assuming a fresh
       list. A hint that was true at an earlier poll -- "still billing"
       before teardown actually succeeded -- survives forever into every
       later envelope, including ones where `cleanup` has since become
       `released`/`already_absent` and directly contradicts it. Once
       cleanup is confirmed terminal-and-gone, any such hint is now
       false; drop it. Diagnostic hints that remain true regardless of
       cleanup outcome (e.g. "check the URL has not expired" for a stage
       failure) are untouched -- this only targets hints about the VM's
       own up/down state.
    2. The same hint text can get appended more than once across repeat
       calls (e.g. a `status --poll` re-absorbing a result that was
       already absorbed once). Deduplicate, preserving order.
    """
    from colab_cli.job.models import Cleanup

    hints = env.hints
    if env.cleanup in (Cleanup.RELEASED, Cleanup.ALREADY_ABSENT):
        hints = [
            h
            for h in hints
            if not any(marker in h.lower() for marker in _STALE_BILLING_HINT_MARKERS)
        ]
    seen: set[str] = set()
    deduped = []
    for h in hints:
        if h not in seen:
            seen.add(h)
            deduped.append(h)
    env.hints = deduped


def _status_poll_deadline(env, store, job_id: str):
    """(epoch, description) of an orphaned job's run deadline: launch plus
    wall_clock plus the margin, as apply's default. None when the job has no
    launch time (it was orphaned before launch) or no readable plan; that
    is logged."""
    if not env.started_at:
        _logger.warning(
            "job status %s: no launch time recorded, so status --poll has no deadline",
            job_id,
        )
        return None
    try:
        plan = store.read_plan(job_id)
    except ValueError as error:
        _logger.warning(
            "job status %s: plan unreadable (%s), so status --poll has no deadline",
            job_id,
            error,
        )
        return None
    if plan is None:
        _logger.warning("job status %s: no plan, so status --poll has no deadline", job_id)
        return None
    try:
        launched = datetime.datetime.fromisoformat(env.started_at).timestamp()
    except ValueError:
        _logger.warning(
            "job status %s: launch time %r unparseable, so status --poll has no deadline",
            job_id,
            env.started_at,
        )
        return None
    seconds = run_deadline_seconds(plan.spec)
    return launched + seconds, (
        f"no verdict within {seconds}s of launch "
        f"(wall_clock {plan.spec.budgets.wall_clock}s + {RUN_DEADLINE_MARGIN_SECONDS}s); "
        "the job's supervisor is gone, so job status --poll cancelled it"
    )


def _cancel_orphan_after_deadline(env, store, job_id: str, transport, passed: str) -> None:
    """status --poll's counterpart to apply's deadline cancel, for a job
    nobody supervises: cancel, wait for the result, and end the job so the
    orphan release that follows can act."""
    requester = "job status --poll"
    note, confirmed = request_cancel(transport, job_id, requester)
    kind, outcome = await_runner_result(transport, job_id, RUNNER_STOP_WAIT_SECONDS)
    if kind == "result":
        try:
            _absorb_remote_result(env, store, job_id, outcome)
        except Exception as error:  # noqa: BLE001 - the release must still happen
            env.hints.append(
                f"runner result could not be absorbed ({describe_error(error)}); "
                f"raw result: {raw_verdict(outcome)}"
            )
        env.reason = deadline_reason(passed, note, confirmed, requester, outcome, env.reason)
    else:
        env.workload = Workload.UNKNOWN
        if not env.offload.terminal:
            env.offload = Offload.SKIPPED
        env.reason = f"{passed}; {note}; {outcome}"
    env.record_failure(Phase.RUN)
    if env.retry_class is None:
        env.retry_class = RetryClass.RETRY_SAME
    env.supervisor = Supervisor.FINISHED
    env.finished_at = env.finished_at or _now()


def _release_orphaned_job(env, session, state, store, transport) -> None:
    """Finish an orphaned job whose workload is terminal: keep the VM when
    `keep_vm` says so (the secret was already confirmed removed to get
    here), otherwise copy its records and release it."""
    try:
        plan = store.read_plan(env.job_id)
    except ValueError as error:
        _logger.warning(
            "job status %s: plan unreadable (%s); only --leave-up decides whether "
            "the VM is kept",
            env.job_id,
            error,
        )
        plan = None
    if keep_vm(env, plan.spec if plan else None, secret_removed=True):
        record_vm_kept(env, env.job_id)
        env.supervisor = Supervisor.FINISHED
        store.write_envelope(env)
        return
    _copy_before_release(env, transport, store)
    stop_session_keep_alive(session)
    _release(env, state, "recovery teardown failed")
    _forget_session(env, state)
    env.supervisor = Supervisor.FINISHED
    if not env.offload.terminal:
        env.offload = Offload.SKIPPED
    store.write_envelope(env)


def _ensure_keep_alive(session, state) -> Optional[str]:
    """Respawn keep-alive if the daemon has died, returning a hint if so.

    Issue #54: `spawn_keep_alive` starts a genuinely detached process
    (`start_new_session=True`), but it has been observed dying anyway when
    the local `job apply` invocation that started it is killed externally
    (a wrapping tool's timeout, a closed terminal) -- confirmed via local
    history logs showing `keep_alive_started` with no matching
    `keep_alive_stopped`, which the daemon's own loop always logs before
    any graceful exit. `job apply --async` (spawning apply itself as a
    detached child) removes one specific cause of that, but the daemon
    dying is not something mighty-colab should merely hope doesn't
    recur -- self-heal it regardless of cause. Without a ping the
    assignment idles out and is gone within minutes, well before a long
    job's `wall_clock` budget elapses. `job status`/`--poll` already
    holds a live `session` object and is what a caller is expected to
    call periodically regardless, so it's the natural place to notice
    and recover before that happens.
    """
    from colab_cli.common import pid_alive

    if pid_alive(session.keep_alive_pid):
        return None
    from colab_cli.commands.session import spawn_keep_alive

    stopped = _keep_alive_stop_note(state, session.name)
    try:
        session.keep_alive_pid = spawn_keep_alive(
            session.endpoint,
            session.name,
            auth_provider=state.auth_provider,
            config_path=state.config_path,
        )
        state.store.add(session)
    except Exception as error:  # noqa: BLE001 - the status read must still finish
        return (
            f"keep-alive had died ({stopped}) and could not be respawned "
            f"({describe_error(error)}); Colab may reclaim the idle VM"
        )
    return f"keep-alive had died ({stopped}); respawned as pid {session.keep_alive_pid}"


def _keep_alive_stop_note(state, session_name: str) -> str:
    """Why the session's last keep-alive daemon stopped, from its history:
    the reason and last error it logged, or that it logged none."""
    try:
        events = state.history.get_history(session_name)
    except Exception as error:  # noqa: BLE001 - only a note
        return f"its history could not be read: {describe_error(error)}"
    last = next(
        (
            e
            for e in reversed(events)
            if e.get("event_type") in ("keep_alive_started", "keep_alive_stopped")
        ),
        None,
    )
    if last is None or last.get("event_type") != "keep_alive_stopped":
        return "it logged no stop reason"
    note = f"stopped: {last.get('reason')}"
    if last.get("last_error"):
        note += f", last error {last['last_error']}"
    return note


def status(
    job_id: Annotated[str, typer.Argument(help="Job id")],
    poll: Annotated[
        bool,
        typer.Option(
            "--poll",
            help="Keep polling until a remote verdict or dead runner, then finish leftover cleanup",
        ),
    ] = False,
    interval: Annotated[int, typer.Option("--interval", help="Poll interval (s)")] = 15,
):
    # Reads the VM, not just the local record: a status that only echoed
    # the local record would happily report a healthy session for twenty
    # minutes after the VM was actually gone (a real field bug).
    """Report a job's state, asking the VM rather than local memory."""
    from colab_cli.common import state
    from colab_cli.job.transport import JobTransport

    store = _store()
    env = store.read_envelope(job_id)
    if env is None:
        detail = _apply_log_note(store, job_id)
        _emit_command_message(
            "status",
            f"[colab] No local record of job {job_id!r}"
            + (f"; {detail}" if detail else "."),
            reason="job_not_found",
        )
        raise typer.Exit(1)

    supervisor_alive = _supervisor_alive(store, job_id)
    if env.supervisor is Supervisor.RUNNING and not supervisor_alive:
        env.supervisor = Supervisor.INTERRUPTED
        env.reason = "the supervisor process that started this job is gone"

    orphaned = not supervisor_alive and env.supervisor is not Supervisor.RUNNING
    cleanup_pending = env.cleanup not in {
        Cleanup.RELEASED,
        Cleanup.ALREADY_ABSENT,
        Cleanup.LEFT_UP,
    }
    must_scrub = orphaned or env.cleanup is Cleanup.FAILED
    if env.session and cleanup_pending:
        session = state.store.get(env.session)
        if session is None and must_scrub:
            env = _recover_off_vm_result(env, store, job_id) or env
            _force_release_unconfirmed_secret(
                env, state, store, "status",
                cause=f"no local session record {env.session!r}, so the VM could not be reached",
            )
        if session is None and not must_scrub:
            env.hints.append(
                f"the VM was not consulted: no local session record {env.session!r} "
                "on this machine"
            )
        if session is not None:
            keep_alive_hint = _ensure_keep_alive(session, state)
            if keep_alive_hint:
                env.hints.append(keep_alive_hint)
            transport = JobTransport(session, state.client, state.store)
            scrubbed, scrub_problem = (
                _scrub_transfer_secret(transport, job_id) if must_scrub else (True, None)
            )
            if not scrubbed:
                env = _recover_off_vm_result(env, store, job_id) or env
                _force_release_unconfirmed_secret(
                    env, state, store, "status", transport, cause=scrub_problem
                )
            kind = None
            if orphaned and env.workload.terminal:
                _release_orphaned_job(env, session, state, store, transport)
                _emit(env, "status")
                return
            if not env.workload.terminal:
                staleness = WatchdogStaleness()
                deadline = _status_poll_deadline(env, store, job_id) if orphaned else None
                while True:
                    if deadline is not None and time.time() > deadline[0]:
                        _cancel_orphan_after_deadline(
                            env, store, job_id, transport, deadline[1]
                        )
                        break
                    keep_alive_hint = _ensure_keep_alive(session, state)
                    if keep_alive_hint:
                        env.hints.append(keep_alive_hint)
                    # Same Contents connection already open for the result
                    # poll below -- no new signed URL, no watchdog changes.
                    # runner.log is on VM disk the whole run and nothing
                    # else ever reads it back; if the VM disappears before
                    # offload, it's gone with everything else. Best-effort:
                    # any failure here must never break the verdict poll.
                    log_problem = pull_runner_log(transport, store, job_id)
                    if log_problem is not None:
                        _logger.warning("job %s: runner.log not copied: %s", job_id, log_problem)
                    kind, payload = observe_remote(transport, job_id)
                    if kind == "result":
                        try:
                            _absorb_remote_result(env, store, job_id, payload)
                        except Exception as error:  # noqa: BLE001 - kept in the reason
                            env.workload = Workload.UNKNOWN
                            env.record_failure(Phase.RUN)
                            env.reason = (
                                f"runner result could not be absorbed ({describe_error(error)}); "
                                f"raw result: {raw_verdict(payload)}"
                            )
                            env.retry_class = RetryClass.DO_NOT_RETRY
                            if not env.offload.terminal:
                                env.offload = Offload.SKIPPED
                        if orphaned:
                            env.supervisor = Supervisor.FINISHED
                        break
                    if kind in {"session_lost", "runner_dead"} or (
                        kind == "never_started" and orphaned
                    ):
                        recovered = _recover_off_vm_result(env, store, job_id)
                        if recovered is not None:
                            env = recovered
                            env.supervisor = Supervisor.FINISHED
                            break
                    if kind == "session_lost":
                        env.workload = Workload.UNKNOWN
                        env.record_failure(Phase.RUN)
                        env.reason = "the assignment is gone from the server"
                        env.retry_class = RetryClass.RETRY_SAME
                        env.supervisor = Supervisor.FINISHED
                        break
                    if kind == "runner_dead":
                        env.workload = Workload.UNKNOWN
                        env.record_failure(Phase.RUN)
                        reading = (
                            f" (watchdog: elapsed={payload.get('elapsed')}s, "
                            f"ts={payload.get('ts')})"
                            if payload
                            else ""
                        )
                        env.reason = (
                            f"runner identity is dead and no result.json was written{reading}; "
                            "its last output is in runner.log"
                        )
                        env.retry_class = RetryClass.RETRY_SAME
                        env.supervisor = Supervisor.FINISHED
                        break
                    if kind == "never_started" and orphaned:
                        env.workload = Workload.UNKNOWN
                        env.record_failure(Phase.RUN)
                        env.reason = (
                            "supervisor is gone and no runner identity was recorded"
                        )
                        env.retry_class = RetryClass.RETRY_SAME
                        env.supervisor = Supervisor.FINISHED
                        break
                    if kind == "degraded":
                        env.supervisor = Supervisor.DEGRADED
                        env.reason = (
                            degraded_reason(transport) + "; the job is not known dead"
                        )
                    if kind == "launch_invalid":
                        identity = {
                            key: (payload or {}).get(key)
                            for key in ("pid", "starttime", "boot_id")
                        }
                        env.reason = (
                            "launch.json on the VM has no valid runner identity: "
                            f"{identity}"
                        )
                    if kind == "runner_alive" and payload:
                        stalled = staleness.observe(payload)
                        replace_hint(env.hints, WATCHDOG_STALLED_PREFIX, stalled)
                        if stalled:
                            env.reason = stalled
                    if not poll:
                        break
                    time.sleep(interval)
            if orphaned and env.workload.terminal:
                _release_orphaned_job(env, session, state, store, transport)
            else:
                store.write_envelope(env)

    released = env.cleanup in (Cleanup.RELEASED, Cleanup.ALREADY_ABSENT)
    if not (env.session and cleanup_pending) and not released:
        why = (
            "no session is recorded for this job"
            if not env.session
            else f"cleanup is already {env.cleanup.value}"
        )
        note = f"the VM was not consulted: {why}"
        if env.cleanup is Cleanup.LEFT_UP:
            note += (
                "; the VM was left running and may still bill: "
                f"`mighty-colab sessions` shows it, `job destroy {job_id}` releases it"
            )
        if note not in env.hints:
            env.hints.append(note)

    # Finalize hints once, regardless of which branch above ran (or none --
    # a terminal job with no session skips the whole block): drops any
    # "still billing"/"left running" hint that cleanup has since made
    # false, dedupes repeats, and persists the result. Redundant with an
    # already-written envelope in the branches above, which is harmless --
    # same object, same final state.
    _finalize_hints(env)
    store.write_envelope(env)
    _emit(env, "status")


def destroy(
    job_id: Annotated[str, typer.Argument(help="Job id")],
    cancel_only: Annotated[
        bool,
        typer.Option("--cancel-only", help="Signal the workload but keep the VM"),
    ] = False,
    wait: Annotated[
        int,
        typer.Option(
            "--wait",
            min=0,
            help=(
                "Seconds to wait, after signalling a running job, for the runner "
                "to stop it, upload artifacts and write result.json before the "
                "VM is released. 0 releases at once."
            ),
        ),
    ] = RUNNER_STOP_WAIT_SECONDS,
):
    """Unconditional teardown. Safe to run twice; exits 0 if already gone.

    Before release, the job's records on the VM (runner.log, install.log,
    result.json and the other runner files) are copied into the local job
    directory."""
    from colab_cli.common import state
    from colab_cli.job.transport import JobTransport

    store = _store()
    env = store.read_envelope(job_id)
    if env is None:
        _emit_command_message(
            "destroy",
            f"[colab] No local record of job {job_id!r}; nothing to destroy.",
            exit_code=0,
            reason="job_not_found",
        )
        raise typer.Exit(0)

    saved_plan = store.read_plan(job_id)
    transport = None
    wait_outcome = None
    session = state.store.get(env.session) if env.session else None
    if session is not None:
        transport = JobTransport(session, state.client, state.store)
        try:
            result, read_status = transport.read_json(
                f"/content/jobs/{job_id}/result.json"
            )
            if read_status.name == "OK" and result:
                _absorb_remote_result(env, store, job_id, result)
                env.supervisor = Supervisor.FINISHED
            elif read_status.name not in ("OK", "NOT_FOUND"):
                env.hints.append(
                    f"reading result.json before destroy returned {read_status.name}: "
                    + degraded_reason(transport)
                )
        except Exception as e:  # noqa: BLE001 - teardown still must proceed
            env.hints.append(
                f"the remote result could not be reconciled before destroy ({describe_error(e)})"
            )

    if not env.workload.terminal:
        recovered = _recover_off_vm_result(env, store, job_id)
        if recovered is not None:
            env = recovered
            env.supervisor = Supervisor.FINISHED

    if transport is None:
        secret_removed, scrub_problem = False, (
            f"no local session record {env.session!r}, so the VM could not be reached"
        )
    else:
        secret_removed, scrub_problem = _scrub_transfer_secret(transport, job_id)
    if cancel_only and not secret_removed:
        _copy_before_release(env, transport, store)
        stop_session_keep_alive(session)
        _release(env, state, "forced unassign failed")
        _forget_session(env, state)
        if not env.workload.terminal:
            if env.reason:
                env.hints.append(f"before the forced teardown: {env.reason}")
            env.workload = Workload.UNKNOWN
            env.record_failure(Phase.CLEANUP)
            env.reason = (
                "cancel-only retention overridden because transfer credential "
                f"deletion could not be confirmed ({scrub_problem})"
            )
            env.retry_class = RetryClass.DO_NOT_RETRY
        else:
            env.hints.append(
                "forced VM teardown because credential deletion was not confirmed "
                f"({scrub_problem})"
            )
        env.supervisor = Supervisor.FINISHED
        _finalize_hints(env)
        store.write_envelope(env)
        _emit(env, "destroy", exit_code=1)
        raise typer.Exit(1)
    cancel_note, cancel_confirmed = None, False
    if not env.workload.terminal and transport is not None:
        cancel_note, cancel_confirmed = request_cancel(transport, job_id, "job destroy")

    if cancel_only:
        if env.workload.terminal:
            env.reason = env.reason or "workload already terminal; VM left running"
        elif cancel_confirmed:
            env.reason = "cancel intent written; VM left running"
        else:
            env.reason = (
                f"cancel intent could not be confirmed ({cancel_note or 'no transport'}); "
                "VM left running"
            )
        _finalize_hints(env)
        store.write_envelope(env)
        failed = not env.workload.terminal and not cancel_confirmed
        _emit(env, "destroy", exit_code=1 if failed else 0)
        if failed:
            raise typer.Exit(1)
        return

    if (
        not env.workload.terminal
        and transport is not None
        and cancel_confirmed
        and wait > 0
    ):
        if _supervisor_alive(store, job_id):
            # A live `job apply` owns the release: it absorbs the runner's
            # result, copies the records and unassigns. Releasing here as
            # well would race it and overwrite its envelope.
            typer.echo(
                f"[colab] Waiting up to {wait}s for the running `job apply` to "
                "stop the job and release the VM.",
                err=True,
            )
            current = _await_supervisor_cleanup(store, job_id, wait)
            if current is not None and current.cleanup in (
                Cleanup.RELEASED,
                Cleanup.ALREADY_ABSENT,
            ):
                _emit(current, "destroy", exit_code=0)
                return
            # Left up, failed, or still running: release from the latest
            # record rather than the one read at the start of this command.
            try:
                env = store.read_envelope(job_id) or env
            except Exception as error:  # noqa: BLE001 - release must proceed
                env.hints.append(
                    f"latest envelope unreadable ({describe_error(error)}); "
                    "releasing from the record read at the start of destroy"
                )
            if current is None:
                env.hints.append(
                    f"the running `job apply` did not release the VM within {wait}s; "
                    "destroy took over the release"
                )
            else:
                env.hints.append(
                    f"the running `job apply` ended with cleanup={current.cleanup.value}; "
                    "destroy took over the release"
                )
        else:
            typer.echo(
                f"[colab] Waiting up to {wait}s for the runner to stop the job "
                "and write its result before release.",
                err=True,
            )
            kind, outcome = await_runner_result(transport, job_id, wait)
            if kind == "result":
                try:
                    _absorb_remote_result(env, store, job_id, outcome)
                    env.supervisor = Supervisor.FINISHED
                except Exception as error:  # noqa: BLE001 - release must proceed
                    env.hints.append(
                        f"runner result could not be absorbed ({describe_error(error)}); "
                        f"raw result: {raw_verdict(outcome)}"
                    )
            else:
                wait_outcome = outcome
                env.hints.append(outcome)
    _copy_before_release(env, transport, store)
    stop_session_keep_alive(session)
    _release(env, state, "unassign failed")
    _forget_session(env, state)
    if not env.workload.terminal:
        env.workload = Workload.UNKNOWN
        if not env.offload.terminal:
            env.offload = (
                Offload.NOT_REQUIRED
                if saved_plan is None or not saved_plan.spec.artifacts
                else Offload.SKIPPED
            )
        env.reason = (
            "VM destroyed after cancellation was requested; remote workload "
            "verdict unavailable"
            if cancel_confirmed
            else "VM destroyed; cancellation intent and remote workload verdict unavailable"
            + (f" ({cancel_note})" if cancel_note else "")
        ) + (f": {wait_outcome}" if wait_outcome else "")
        env.retry_class = RetryClass.DO_NOT_RETRY
    env.supervisor = Supervisor.FINISHED
    _finalize_hints(env)
    store.write_envelope(env)
    _emit(env, "destroy", exit_code=1 if env.cleanup is Cleanup.FAILED else 0)
    if env.cleanup is Cleanup.FAILED:
        raise typer.Exit(1)


# Characters of apply.log's last line quoted when a job has no envelope.
APPLY_LOG_LINE_CHARS = 300


def _apply_log_note(store, job_id: str) -> Optional[str]:
    """For a job with no envelope: `apply --async` writes its output to
    apply.log, and a refusal or crash before the envelope exists is only
    there. Points at the file and quotes its last line."""
    path = store.job_dir(job_id) / "apply.log"
    try:
        lines = [line for line in path.read_text(errors="replace").splitlines() if line.strip()]
    except OSError:
        return None
    if not lines:
        return None
    last = " ".join(lines[-1].split())
    if len(last) > APPLY_LOG_LINE_CHARS:
        omitted = len(last) - APPLY_LOG_LINE_CHARS
        last = last[:APPLY_LOG_LINE_CHARS] + f" [... {omitted} characters omitted]"
    return f"apply wrote no envelope; its output is in {path}, ending: {last}"


def _job_list_rows(store) -> List[Dict[str, Any]]:
    """One row per local job record -- the single source of truth for
    `jobs list --json`, the plain-text `jobs list` rendering, and the
    `jobs://` MCP resource. All three must show the same information;
    the JSON path previously carried only job_id/workload/done/endpoint,
    thinner than what the plain-text rendering already showed
    (workload/offload/cleanup/done) -- that gap is exactly the kind of
    "hides information" bug this function exists to make impossible.
    """
    rows = []
    for jid in store.list_jobs():
        e, problem = store.read_envelope_or_problem(jid)
        if e is None:
            rows.append(
                {
                    "job_id": jid,
                    "phase": None,
                    "workload": None,
                    "offload": None,
                    "cleanup": None,
                    "done": False,
                    "endpoint": None,
                    "reason": problem
                    or _apply_log_note(store, jid)
                    or "planned, not applied",
                }
            )
            continue
        rows.append(
            {
                "job_id": jid,
                "phase": e.phase.value,
                "workload": e.workload.value,
                "offload": e.offload.value,
                "cleanup": e.cleanup.value,
                "done": e.done,
                "endpoint": e.endpoint,
                "reason": e.reason,
            }
        )
    return rows


def list_jobs(
    running: Annotated[
        bool, typer.Option("--running", help="Only jobs not yet done")
    ] = False,
    done: Annotated[
        bool, typer.Option("--done", help="Only jobs that have finished")
    ] = False,
):
    """List local job records."""
    from colab_cli.common import state

    if running and done:
        _emit_command_message(
            "jobs list",
            "[colab] --running and --done are mutually exclusive.",
            reason="usage_error",
        )
        raise typer.Exit(1)

    store = _store()
    rows = _job_list_rows(store)
    if running:
        rows = [r for r in rows if not r["done"]]
    elif done:
        rows = [r for r in rows if r["done"]]
    if state.json_output:
        emit_json(
            build_envelope(status="ok", command="jobs list", jobs=rows),
            JobListEnvelope,
        )
        return
    if not rows:
        typer.echo("[colab] No jobs.")
        return
    for row in rows:
        if row["workload"] is None:
            typer.echo(f"  {row['job_id']}  (planned, not applied)")
        else:
            typer.echo(
                f"  {row['job_id']}  {row['workload']}/{row['offload']}/{row['cleanup']}"
                f"  done={row['done']}"
            )



def prune(
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Report what would be removed without deleting anything."),
    ] = False,
):
    # `left_up`/`failed` are deliberately never auto-pruned: `left_up` is
    # a still-billing VM whose local record is the only pointer to it,
    # and `failed` means teardown's own confirmation failed, which is
    # usually fine but the local record alone can't prove it (see
    # docs/job/store-and-cleanup.md for the live check).
    """Delete local job records that are safe to remove: unapplied plans,
    and terminal jobs with cleanup=released/already_absent.

    Everything else (still running, cleanup=left_up, cleanup=failed) is
    reported as skipped with a reason, never silently deleted.
    """
    from colab_cli.common import state

    store = _store()
    ids = store.list_jobs()
    removed: list[tuple[str, str]] = []
    skipped: list[tuple[str, str]] = []
    for jid in ids:
        e, problem = store.read_envelope_or_problem(jid)
        if problem is not None:
            skipped.append((jid, f"{problem} -- state unknown, will not prune"))
            continue
        if e is None:
            held = store.apply_lock_held(jid)
            log = store.job_dir(jid) / "apply.log"
            if held:
                skipped.append((jid, "a live `job apply` holds its lock, will not prune"))
            elif log.exists():
                # An `apply --async` that refused or crashed before writing
                # an envelope, or one still in its preflight (an empty log):
                # apply.log is the only record of why.
                skipped.append(
                    (
                        jid,
                        (_apply_log_note(store, jid) or f"{log} exists but is empty: "
                         "an `apply --async` may still be in its preflight")
                        + "; read it, then delete the job directory by hand",
                    )
                )
            else:
                removed.append((jid, "planned, not applied"))
            continue
        if e.done and e.cleanup in (Cleanup.RELEASED, Cleanup.ALREADY_ABSENT):
            removed.append((jid, f"done, cleanup={e.cleanup.value}"))
            continue
        if not e.done:
            reason = "not done -- may still be running; check `job status --poll` first"
        elif e.cleanup is Cleanup.LEFT_UP:
            reason = "cleanup=left_up -- VM deliberately left running, will not prune"
        elif e.cleanup is Cleanup.FAILED:
            reason = "cleanup=failed -- confirm with `mighty-colab sessions` before pruning by hand"
        else:
            reason = f"cleanup={e.cleanup.value}"
        skipped.append((jid, reason))

    if not dry_run:
        for jid, reason in list(removed):
            failure = store.delete_job(jid)
            if failure is not None:
                removed.remove((jid, reason))
                skipped.append((jid, f"deletion failed: {failure}"))

    if state.json_output:
        emit_json(
            build_envelope(
                status="ok",
                command="jobs prune",
                dry_run=dry_run,
                removed=[{"job_id": jid, "reason": reason} for jid, reason in removed],
                skipped=[{"job_id": jid, "reason": reason} for jid, reason in skipped],
            ),
            JobPruneEnvelope,
        )
        return

    if not removed and not skipped:
        typer.echo("[colab] No jobs.")
        return
    verb = "would remove" if dry_run else "removed"
    for jid, reason in removed:
        typer.echo(f"  {verb}: {jid}  ({reason})")
    for jid, reason in skipped:
        typer.echo(f"  skipped: {jid}  ({reason})")
    if dry_run:
        typer.echo(f"[colab] Would prune {len(removed)} job record(s), would skip {len(skipped)}.")
    else:
        typer.echo(f"[colab] Pruned {len(removed)} job record(s), skipped {len(skipped)}.")



def register(app: typer.Typer):
    job_app.command(name="plan")(plan)
    job_app.command(name="apply")(apply)
    job_app.command(name="status")(status)
    job_app.command(name="destroy")(destroy)
    app.add_typer(job_app, name="job")
    jobs_app.command(name="list")(list_jobs)
    jobs_app.command(name="prune")(prune)
    app.add_typer(jobs_app, name="jobs")
