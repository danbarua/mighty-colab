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
import os
import time
import uuid
from pathlib import Path
from typing import Optional

import typer
from typing_extensions import Annotated

from colab_cli.common import build_envelope, emit_json
from colab_cli.envelopes import (
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
from colab_cli.job.orchestrator import Orchestrator, PhaseError, stop_session_keep_alive
from colab_cli.job.runtime_payload import ident
from colab_cli.job.spec_io import fetch_control_result
from colab_cli.job.store import ApplyInProgress, JobStore, load_plan_file, write_plan_file

_logger = logging.getLogger(__name__)
job_app = typer.Typer(
    help="Run an unattended job on a Colab VM: plan, apply, status, destroy.",
    no_args_is_help=True,
)
jobs_app = typer.Typer(
    help="Manage local job records as a collection: list, prune.",
    no_args_is_help=True,
)

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
    if env.artifacts:
        for a in env.artifacts:
            lines.append(f"  artifact:   {a.path} -> {a.status}")
    if env.retry_class:
        lines.append(f"  retry:      {env.retry_class.value}")
    if env.reason:
        lines.append(f"  reason:     {env.reason}")
    for h in env.hints:
        lines.append(f"  hint:       {h}")
    return "\n".join(lines)


# --------------------------------------------------------------------------


def _safe_error_location(parts) -> str:
    safe = []
    for part in parts:
        if isinstance(part, int):
            safe.append(str(part))
        elif (
            isinstance(part, str)
            and part.isascii()
            and part.isidentifier()
            and len(part) <= 64
        ):
            safe.append(part)
        else:
            safe.append("<field>")
    return ".".join(safe) or "<spec>"


def _emit_spec_errors(exc) -> None:
    """Render pydantic validation failures without reflecting spec values."""
    from colab_cli.common import state
    diags = []
    for err in exc.errors(
        include_input=False, include_context=False, include_url=False
    ):
        loc = _safe_error_location(err.get("loc", ()))
        diags.append(
            {
                "severity": "error",
                "code": "spec_invalid",
                "message": f"{loc}: {err.get('msg')}",
                "retry_class": "fix_code",
                "hint": None,
            }
        )
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

    from colab_cli.job.payload_bundle import collect_source_files
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
    except (OSError, ValueError) as e:
        _emit_command_message(
            "plan",
            f"[colab] Could not read spec {spec_file!r} ({type(e).__name__}).",
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
    p.source_spec_path = source_spec_path
    try:
        p.source_files = collect_source_files(spec, p.source_spec_path)
    except (FileNotFoundError, ValueError):
        p.source_files = []
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
        if not p.diagnostics:
            typer.echo("  no diagnostics; ready to apply")
        elif not errors:
            typer.echo(f"  {len(warnings)} warning(s); apply will refuse without "
                       "`ignore_warnings: true` in the spec")
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
        typer.Option("--timeout", help="Local supervisor budget (s); the VM's "
                                       "wall_clock still owns the kill"),
    ] = None,
    leave_up: Annotated[
        bool, typer.Option("--leave-up", help="Do not unassign the VM when finished")
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
        _emit_command_message(
            "apply",
            f"[colab] Could not load protected plan ({type(e).__name__}).",
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

    from colab_cli.job.payload_bundle import CONTENTS_UPLOAD_CEILING, verify_source_files

    try:
        verify_source_files(p.spec, p.source_files, p.source_spec_path)
    except ValueError as error:
        _emit_command_message(
            "apply", f"[colab] {error}", reason="plan_refused"
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


    if p.has_errors:
        message = (
            "[colab] This plan has errors and will not be applied. "
            "Fix the spec and re-plan."
        )
        if not state.json_output:
            for d in p.diagnostics:
                if d.severity == "error":
                    typer.echo(f"  ERROR {d.code}: {d.message}", err=True)
        _emit_command_message("apply", message, reason="plan_refused")
        raise typer.Exit(1)
    if p.has_warnings and not p.spec.ignore_warnings:
        message = (
            "[colab] This plan has warnings. Set ignore_warnings: true in the "
            "spec to accept them explicitly."
        )
        if not state.json_output:
            for d in p.diagnostics:
                if d.severity == "warn":
                    typer.echo(f"  WARN {d.code}: {d.message}", err=True)
        _emit_command_message("apply", message, reason="plan_refused")
        raise typer.Exit(1)

    # Expiry is revalidated here, not trusted from plan time: a plan is
    # durable and may be applied long after its signatures were minted.
    # Checked BEFORE assign, because failing after costs a VM.
    expired = revalidate_expiry(p)
    if expired:
        message = (
            "[colab] Signed URLs in this plan have expired or will expire before "
            "the job's budget elapses. Re-sign and re-plan."
        )
        if not state.json_output:
            for error in expired:
                typer.echo(f"  {error}", err=True)
        _emit_command_message("apply", message, reason="plan_refused")
        raise typer.Exit(1)

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


    budget = timeout or (p.spec.budgets.wall_clock + 600)
    deadline = time.time() + budget
    secret_handoff = False
    try:
        orch.provision()
        orch.install()
        orch.restart()
        orch.verify()
        _stage_payload(orch, p)
        orch.seal_secret_channel()
        orch.launch(f"{orch.remote_dir}/src")
        secret_handoff = True
        transport = orch.job_transport()
        orch.poll(transport, deadline=deadline)
    except PhaseError as e:
        orch.env.workload = Workload.FAILED
        orch.env.offload = (
            Offload.NOT_REQUIRED if not p.spec.artifacts else Offload.SKIPPED
        )
        orch.env.supervisor = Supervisor.FINISHED
        orch.env.reason = e.reason
        orch.env.retry_class = e.retry_class
        orch.env.hints.extend(e.hints)
        orch.env.phase = e.phase
    except KeyboardInterrupt:
        orch.env.supervisor = Supervisor.INTERRUPTED
        orch.env.reason = "interrupted locally; the VM job is unaffected"
        orch.env.retry_class = RetryClass.RETRY_SAME
        orch.env.hints.append(
            f"reattach with `mighty-colab job status {p.job_id}`"
        )
    except Exception as e:  # noqa: BLE001 - every non-debug path needs a verdict
        if state.debug:
            raise
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
        orch.env.reason = f"internal supervisor failure ({type(e).__name__})"
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
        keep = leave_up or p.spec.on_offload_fail == "leave_up" and (
            orch.env.offload is Offload.FAILED
        )
        if not secret_removed:
            keep = False
            orch.env.reason = "transfer credential deletion could not be confirmed"
            orch.env.retry_class = RetryClass.DO_NOT_RETRY
            orch.env.hints.append(
                "credential cleanup failed; forced VM teardown to remove the secret"
            )
        if orch.env.supervisor is Supervisor.INTERRUPTED and secret_removed:
            # Deliberately not torn down: the run is still going on the VM
            # and the caller can reattach. Recorded as left_up so the
            # envelope still says it is billing.
            orch.env.cleanup = Cleanup.LEFT_UP
            orch.env.hints.append(
                f"VM still running and billing: mighty-colab job destroy {p.job_id}"
            )
            store.write_envelope(orch.env)
        else:
            orch.cleanup(force_leave_up=keep)
        store.clear_supervisor_identity(p.job_id)
        claim.release()

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




def _scrub_transfer_secret(transport, job_id: str) -> bool:
    path = f"/content/jobs/{job_id}/mighty_runtime/.secrets/transfer.json"
    try:
        return transport.remove(path).name == "OK"
    except Exception:  # noqa: BLE001 - callers enforce teardown on uncertainty
        return False


def _read_off_vm_result(store: JobStore, job_id: str):
    try:
        plan = store.read_plan_for_apply(job_id)
        channel = plan.spec.control.result if plan is not None else None
        if channel is None:
            return None
        result = fetch_control_result(channel.get_url)
        workload = Workload(result.get("workload", ""))
        return result if workload.terminal else None
    except Exception:  # noqa: BLE001 - optional recovery must not block teardown
        return None


def _force_release_unconfirmed_secret(env, state, store, action: str) -> None:
    if env.endpoint:
        try:
            state.client.unassign(env.endpoint)
            env.cleanup = Cleanup.RELEASED
        except Exception as error:  # noqa: BLE001
            env.cleanup = Cleanup.FAILED
            env.hints.append(f"forced unassign failed: {type(error).__name__}")
    else:
        env.cleanup = Cleanup.ALREADY_ABSENT
    if env.session and env.cleanup is not Cleanup.FAILED:
        try:
            state.store.remove(env.session)
        except Exception:  # noqa: BLE001
            pass
    if not env.workload.terminal:
        env.workload = Workload.UNKNOWN
        env.reason = "forced teardown because transfer credential deletion could not be confirmed"
        env.retry_class = RetryClass.DO_NOT_RETRY
    else:
        env.hints.append("forced VM teardown because credential deletion was not confirmed")
    env.supervisor = Supervisor.FINISHED
    store.write_envelope(env)
    _emit(env, action, exit_code=1)
    raise typer.Exit(1)


def _observe_remote(transport, job_id: str):
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
        return "degraded", None
    watchdog, watchdog_status = transport.read_json(
        f"/content/jobs/{job_id}/watchdog.json"
    )
    if watchdog_status.name == "SESSION_LOST":
        return "session_lost", None
    if (
        watchdog_status.name == "OK"
        and watchdog is not None
        and watchdog.get("runner_alive") is False
    ):
        return "runner_dead", launch
    return "runner_alive", launch


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
    try:
        result = _read_off_vm_result(store, job_id)
        if result is None:
            return None
        recovered = env.model_copy(deep=True)
        _absorb_remote_result(recovered, store, job_id, result)
    except Exception:  # noqa: BLE001 - optional recovery must not block teardown
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


def _release_orphaned_job(env, session, state, store) -> None:
    stop_session_keep_alive(session)
    if env.endpoint:
        try:
            state.client.unassign(env.endpoint)
            env.cleanup = Cleanup.RELEASED
        except Exception as error:  # noqa: BLE001
            if "404" in str(error) or "not found" in str(error).lower():
                env.cleanup = Cleanup.ALREADY_ABSENT
            else:
                env.cleanup = Cleanup.FAILED
                env.hints.append(
                    f"recovery teardown failed ({type(error).__name__}); "
                    f"endpoint {env.endpoint} may still be billing"
                )
    else:
        env.cleanup = Cleanup.ALREADY_ABSENT
    if env.session and env.cleanup is not Cleanup.FAILED:
        try:
            state.store.remove(env.session)
        except Exception:  # noqa: BLE001
            pass
    env.supervisor = Supervisor.FINISHED
    if not env.offload.terminal:
        env.offload = Offload.SKIPPED
    store.write_envelope(env)


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
        _emit_command_message(
            "status",
            f"[colab] No local record of job {job_id!r}.",
            reason="job_not_found",
        )
        raise typer.Exit(1)

    # A PID alone is not identity: after reuse, status could mistake an unrelated
    # process for the supervisor and leave a credential-bearing VM alive.
    identity = store.supervisor_identity(job_id)
    supervisor_alive = bool(
        identity
        and ident.alive(
            identity["pid"], identity["starttime"], identity["boot_id"]
        )
    )
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
            _force_release_unconfirmed_secret(env, state, store, "status")
        if session is not None:
            transport = JobTransport(session, state.client, state.store)
            if must_scrub and not _scrub_transfer_secret(transport, job_id):
                env = _recover_off_vm_result(env, store, job_id) or env
                _force_release_unconfirmed_secret(env, state, store, "status")
            kind = None
            if orphaned and env.workload.terminal:
                _release_orphaned_job(env, session, state, store)
                _emit(env, "status")
                return
            if not env.workload.terminal:
                while True:
                    kind, payload = _observe_remote(transport, job_id)
                    if kind == "result":
                        _absorb_remote_result(env, store, job_id, payload)
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
                        env.reason = "the assignment is gone from the server"
                        env.supervisor = Supervisor.FINISHED
                        break
                    if kind == "runner_dead":
                        env.workload = Workload.UNKNOWN
                        env.reason = (
                            "runner identity is dead and no result.json was written"
                        )
                        env.retry_class = RetryClass.RETRY_SAME
                        env.supervisor = Supervisor.FINISHED
                        break
                    if kind == "never_started" and orphaned:
                        env.workload = Workload.UNKNOWN
                        env.reason = (
                            "supervisor is gone and no runner identity was recorded"
                        )
                        env.retry_class = RetryClass.RETRY_SAME
                        env.supervisor = Supervisor.FINISHED
                        break
                    if kind == "degraded":
                        env.supervisor = Supervisor.DEGRADED
                        env.reason = (
                            "transport failing; the assignment is still listed, "
                            "so the job is not known dead"
                        )
                    if not poll:
                        break
                    time.sleep(interval)
            if orphaned and env.workload.terminal:
                _release_orphaned_job(env, session, state, store)
            else:
                store.write_envelope(env)

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
):
    """Unconditional teardown. Safe to run twice; exits 0 if already gone."""
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
    intent_status = None
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
        except Exception as e:  # noqa: BLE001 - teardown still must proceed
            typer.echo(
                f"[colab] Could not reconcile remote result ({type(e).__name__}).",
                err=True,
            )

    if not env.workload.terminal:
        recovered = _recover_off_vm_result(env, store, job_id)
        if recovered is not None:
            env = recovered
            env.supervisor = Supervisor.FINISHED

    secret_removed = transport is not None and _scrub_transfer_secret(
        transport, job_id
    )
    if cancel_only and not secret_removed:
        try:
            stop_session_keep_alive(session)
            state.client.unassign(env.endpoint)
            env.cleanup = Cleanup.RELEASED
            if env.session:
                state.store.remove(env.session)
        except Exception as error:  # noqa: BLE001
            env.cleanup = Cleanup.FAILED
            env.hints.append(f"forced unassign failed: {type(error).__name__}")
        if not env.workload.terminal:
            env.workload = Workload.UNKNOWN
            env.reason = (
                "cancel-only retention overridden because transfer credential "
                "deletion could not be confirmed"
            )
            env.retry_class = RetryClass.DO_NOT_RETRY
        else:
            env.hints.append(
                "forced VM teardown because credential deletion was not confirmed"
            )
        env.supervisor = Supervisor.FINISHED
        _finalize_hints(env)
        store.write_envelope(env)
        _emit(env, "destroy", exit_code=1)
        raise typer.Exit(1)
    if not env.workload.terminal and transport is not None:
        try:
            intent_status = transport.write_json(
                f"/content/jobs/{job_id}/cancel.json",
                {
                    "intent": "cancelled",
                    "by": "job destroy",
                    "at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                },
            )
        except Exception as e:  # noqa: BLE001 - teardown still must proceed
            typer.echo(
                f"[colab] Could not write cancel intent ({type(e).__name__}).",
                err=True,
            )

    if cancel_only:
        if env.workload.terminal:
            env.reason = env.reason or "workload already terminal; VM left running"
        elif intent_status is not None and intent_status.name == "OK":
            env.reason = "cancel intent written; VM left running"
        else:
            env.reason = "cancel intent could not be confirmed; VM left running"
        _finalize_hints(env)
        store.write_envelope(env)
        failed = not env.workload.terminal and (
            intent_status is None or intent_status.name != "OK"
        )
        _emit(env, "destroy", exit_code=1 if failed else 0)
        if failed:
            raise typer.Exit(1)
        return

    stop_session_keep_alive(session)
    if env.endpoint:
        try:
            state.client.unassign(env.endpoint)
            env.cleanup = Cleanup.RELEASED
        except Exception as e:  # noqa: BLE001
            if "404" in str(e) or "not found" in str(e).lower():
                env.cleanup = Cleanup.ALREADY_ABSENT
            else:
                env.cleanup = Cleanup.FAILED
                env.hints.append(f"unassign failed: {type(e).__name__}")
    else:
        env.cleanup = Cleanup.ALREADY_ABSENT

    if env.session and env.cleanup is not Cleanup.FAILED:
        try:
            state.store.remove(env.session)
        except Exception:  # noqa: BLE001
            pass
    if not env.workload.terminal:
        env.workload = Workload.UNKNOWN
        if not env.offload.terminal:
            env.offload = (
                Offload.NOT_REQUIRED
                if saved_plan is None or not saved_plan.spec.artifacts
                else Offload.SKIPPED
            )
        wrote_intent = intent_status is not None and intent_status.name == "OK"
        env.reason = (
            "VM destroyed after cancellation was requested; remote workload "
            "verdict unavailable"
            if wrote_intent
            else "VM destroyed; cancellation intent and remote workload verdict unavailable"
        )
        env.retry_class = RetryClass.DO_NOT_RETRY
    env.supervisor = Supervisor.FINISHED
    _finalize_hints(env)
    store.write_envelope(env)
    _emit(env, "destroy", exit_code=1 if env.cleanup is Cleanup.FAILED else 0)
    if env.cleanup is Cleanup.FAILED:
        raise typer.Exit(1)


def list_jobs():
    """List local job records."""
    from colab_cli.common import state
    store = _store()
    ids = store.list_jobs()
    if state.json_output:
        rows = []
        for jid in ids:
            e = store.read_envelope(jid)
            rows.append(
                {
                    "job_id": jid,
                    "workload": e.workload.value if e else "pending",
                    "done": e.done if e else False,
                    "endpoint": e.endpoint if e else None,
                }
            )
        emit_json(
            build_envelope(status="ok", command="jobs list", jobs=rows),
            JobListEnvelope,
        )
        return
    if not ids:
        typer.echo("[colab] No jobs.")
        return
    for jid in ids:
        e = store.read_envelope(jid)
        if e is None:
            typer.echo(f"  {jid}  (planned, not applied)")
        else:
            typer.echo(
                f"  {jid}  {e.workload.value}/{e.offload.value}/{e.cleanup.value}"
                f"  done={e.done}"
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
        e = store.read_envelope(jid)
        if e is None:
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
        for jid, _ in removed:
            store.delete_job(jid)

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
