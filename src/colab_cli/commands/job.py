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
from colab_cli.job.orchestrator import Orchestrator, PhaseError
from colab_cli.job.store import JobStore

job_app = typer.Typer(
    help="Run an unattended job on a Colab VM: plan, apply, status, destroy.",
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


def _emit(env: JobEnvelope, command: str) -> None:
    """One envelope shape for every `job` subcommand.

    `status` is the CLI-transaction field ("did this invocation work"),
    which is *not* the same question as whether the job succeeded -- a
    perfectly successful `job status` call reporting a failed run exits 0.
    Collapsing the two is how a caller ends up retrying the CLI instead of
    fixing their code.
    """
    from colab_cli.common import state
    if state.json_output:
        emit_json(
            build_envelope(
                status="ok",
                command=f"job {command}",
                exit_code=0,
                job=json.loads(env.model_dump_json()),
                done=env.done,
                ok=env.ok,
            ),
            JobEnvelopeWrapper,
        )
    else:
        typer.echo(_human(env))


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


def _emit_spec_errors(exc) -> None:
    """Render pydantic validation failures in the plan-diagnostic shape."""
    from colab_cli.common import state
    diags = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ())) or "<spec>"
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
    """Validate a spec and report what `apply` would do. Allocates nothing.

    `plan` never calls `assign`. That is the whole point of having it: an
    agent can iterate on a broken spec for free, and the first thing that
    costs money is the thing the caller explicitly asked for.
    """
    from colab_cli.common import state
    from pydantic import ValidationError

    from colab_cli.job.planner import build_plan
    from colab_cli.job.spec_io import load_spec

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
        typer.echo(f"[colab] Could not read spec {spec_file!r}: {e}", err=True)
        raise typer.Exit(1) from None
    job_id = _new_job_id(spec.name)
    p = build_plan(spec, job_id, probe=not no_probe)

    store = _store()
    store.write_spec(job_id, spec)
    store.write_plan(p)
    if out:
        Path(out).write_text(p.model_dump_json(indent=2))

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

    Refuses a spec whose hash does not match the plan's. A plan is a
    durable artifact that may be applied hours after it was written, and
    "the thing I reviewed" must be the thing that runs.
    """
    from colab_cli.common import state
    from colab_cli.job.models import Plan
    from colab_cli.job.planner import revalidate_expiry
    from colab_cli.job.spec_io import spec_hash
    from colab_cli.job.transport import JobTransport

    store = _store()
    if plan_file:
        p = Plan.model_validate_json(Path(plan_file).read_text())
    elif job_id_opt:
        p = store.read_plan(job_id_opt)
        if p is None:
            typer.echo(f"[colab] No plan found for job {job_id_opt!r}.", err=True)
            raise typer.Exit(1)
    else:
        typer.echo("[colab] Pass a plan file or --job-id.", err=True)
        raise typer.Exit(1)

    # The thing that was reviewed must be the thing that runs. A plan is a
    # durable file: it can be edited, or hand-written, between `plan` and
    # `apply`. Re-derive the hash rather than trusting the one recorded
    # inside the same file -- a self-certifying document certifies nothing.
    #
    # The hash canonicalises URL query strings out, so re-signing the same
    # object does NOT invalidate a plan, while pointing at a different
    # object does. That is the distinction worth enforcing: it is also what
    # stops a hand-edited plan from bypassing the plan-time gates (unknown
    # accelerator, path escape, non-HTTPS URL) that `apply` itself does not
    # re-run.
    actual = spec_hash(p.spec)
    if actual != p.spec_hash:
        typer.echo(
            "[colab] This plan's spec does not match its recorded hash "
            f"(plan says {p.spec_hash[:12]}, spec hashes to {actual[:12]}). "
            "The spec was changed after planning. Re-run `job plan`.",
            err=True,
        )
        raise typer.Exit(1)

    if p.has_errors:
        typer.echo(
            "[colab] This plan has errors and will not be applied. "
            "Fix the spec and re-plan.",
            err=True,
        )
        for d in p.diagnostics:
            if d.severity == "error":
                typer.echo(f"  ERROR {d.code}: {d.message}", err=True)
        raise typer.Exit(1)
    if p.has_warnings and not p.spec.ignore_warnings:
        typer.echo(
            "[colab] This plan has warnings. Set `ignore_warnings: true` in the "
            "spec to accept them explicitly.",
            err=True,
        )
        for d in p.diagnostics:
            if d.severity == "warn":
                typer.echo(f"  WARN {d.code}: {d.message}", err=True)
        raise typer.Exit(1)

    # Expiry is revalidated here, not trusted from plan time: a plan is
    # durable and may be applied long after its signatures were minted.
    # Checked BEFORE `assign`, because failing after costs a VM.
    expired = revalidate_expiry(p)
    if expired:
        typer.echo(
            "[colab] Signed URLs in this plan have expired or will expire before "
            "the job's budget elapses. Re-sign and re-plan.",
            err=True,
        )
        for e in expired:
            typer.echo(f"  {e}", err=True)
        raise typer.Exit(1)

    from colab_cli.runtime import ColabRuntime

    orch = Orchestrator(
        plan=p,
        store=store,
        client=state.client,
        runtime_factory=lambda url, token: ColabRuntime(url, token),
        transport_factory=lambda s: JobTransport(s, state.client, state.store),
        session_store=state.store,
        emit=lambda m: typer.echo(m),
    )
    store.write_supervisor_pid(p.job_id, os.getpid())

    budget = timeout or (p.spec.budgets.wall_clock + 600)
    deadline = time.time() + budget

    try:
        orch.provision()
        orch.install()
        orch.restart()
        orch.verify()
        _stage_payload(orch, p)
        orch.launch(f"{orch.remote_dir}/src")
        transport = orch.transport_factory(orch.session_state)
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
    finally:
        # Teardown is how you leave, not a phase you reach: an early
        # failure must still release the VM, or the cost of a typo is an
        # A100 left assigned.
        keep = leave_up or p.spec.on_offload_fail == "leave_up" and (
            orch.env.offload is Offload.FAILED
        )
        if orch.env.supervisor is Supervisor.INTERRUPTED:
            # Deliberately not torn down: the run is still going on the VM
            # and the caller can reattach. Recorded as `left_up` so the
            # envelope still says it is billing.
            orch.env.cleanup = Cleanup.LEFT_UP
            orch.env.hints.append(
                f"VM still running and billing: `mighty-colab job destroy {p.job_id}`"
            )
            store.write_envelope(orch.env)
        else:
            orch.cleanup(force_leave_up=keep)
        store.clear_supervisor_pid(p.job_id)

    _emit(orch.env, "apply")
    if not orch.env.ok:
        raise typer.Exit(1)


def _stage_payload(orch: Orchestrator, p) -> None:
    """Upload the runtime package, the user's code, and the two manifests.

    The manifests are written here rather than on the VM so that signed
    URLs never pass through `execute_code` -- kernel input is echoed into
    the session log, and a signature in a log is a credential in a log.
    """
    from colab_cli.job.payload_bundle import stage_payload

    orch._set_phase(Phase.STAGE)
    stage_payload(
        spec=p.spec,
        job_id=p.job_id,
        session=orch.session_state,
        remote_dir=orch.remote_dir,
    )


def status(
    job_id: Annotated[str, typer.Argument(help="Job id")],
    poll: Annotated[
        bool, typer.Option("--poll", help="Keep polling until the job is done")
    ] = False,
    interval: Annotated[int, typer.Option("--interval", help="Poll interval (s)")] = 15,
):
    """Report a job's state, asking the VM rather than local memory.

    The distinction is load-bearing and was a real field bug: a `status`
    that reads only the local record will happily report a healthy session
    for twenty minutes after the VM has gone.
    """
    from colab_cli.common import state
    from colab_cli.job.transport import JobTransport

    store = _store()
    env = store.read_envelope(job_id)
    if env is None:
        typer.echo(f"[colab] No local record of job {job_id!r}.", err=True)
        raise typer.Exit(1)

    # A supervisor that is no longer running cannot be `running`. Without
    # this the envelope claims someone is driving long after the laptop
    # slept, and `done` never becomes true.
    pid = store.supervisor_pid(job_id)
    if env.supervisor is Supervisor.RUNNING and (pid is None or not _pid_alive(pid)):
        env.supervisor = Supervisor.INTERRUPTED
        env.reason = "the supervisor process that started this job is gone"

    if env.session and not env.workload.terminal:
        s = state.store.get(env.session)
        if s is not None:
            transport = JobTransport(s, state.client, state.store)
            while True:
                result, st = transport.read_json(
                    f"/content/jobs/{job_id}/result.json"
                )
                if st.name == "OK" and result:
                    env.workload = Workload(result.get("workload", "unknown"))
                    env.exit_code = result.get("exit_code")
                    env.signal = result.get("signal")
                    env.exception = result.get("exception")
                    env.supervisor = Supervisor.FINISHED
                    break
                if st.name == "SESSION_LOST":
                    env.workload = Workload.UNKNOWN
                    env.reason = "the assignment is gone from the server"
                    env.supervisor = Supervisor.FINISHED
                    break
                if st.name == "DEGRADED":
                    env.supervisor = Supervisor.DEGRADED
                    env.reason = (
                        "transport failing; the assignment is still listed, "
                        "so the job is not known dead"
                    )
                if not poll:
                    break
                time.sleep(interval)
            store.write_envelope(env)

    _emit(env, "status")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def destroy(
    job_id: Annotated[str, typer.Argument(help="Job id")],
    cancel_only: Annotated[
        bool,
        typer.Option("--cancel-only", help="Signal the workload but keep the VM"),
    ] = False,
):
    """Unconditional teardown. Safe to run twice.

    Exits 0 when the thing is already gone, because the whole value of an
    unconditional teardown is that a caller can run it without first
    working out whether it is needed.
    """
    from colab_cli.common import state
    from colab_cli.job.transport import JobTransport

    store = _store()
    env = store.read_envelope(job_id)
    if env is None:
        typer.echo(f"[colab] No local record of job {job_id!r}; nothing to destroy.")
        raise typer.Exit(0)

    if env.session:
        s = state.store.get(env.session)
        if s is not None:
            try:
                transport = JobTransport(s, state.client, state.store)
                transport.write_json(
                    f"/content/jobs/{job_id}/cancel.json",
                    {
                        "intent": "cancelled",
                        "by": "job destroy",
                        "at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    },
                )
            except Exception as e:  # noqa: BLE001 - best effort; unassign follows
                typer.echo(f"[colab] Could not write cancel intent: {e}", err=True)

    if cancel_only:
        env.reason = "cancel intent written; VM left running"
        store.write_envelope(env)
        _emit(env, "destroy")
        return

    if env.endpoint:
        try:
            state.client.unassign(env.endpoint)
            env.cleanup = Cleanup.RELEASED
        except Exception as e:  # noqa: BLE001
            # Already-absent is success for a desired-state verb; only a
            # real failure is worth a nonzero exit.
            if "404" in str(e) or "not found" in str(e).lower():
                env.cleanup = Cleanup.ALREADY_ABSENT
            else:
                env.cleanup = Cleanup.FAILED
                env.hints.append(f"unassign failed: {e}")
    else:
        env.cleanup = Cleanup.ALREADY_ABSENT

    if env.session:
        try:
            state.store.remove(env.session)
        except Exception:  # noqa: BLE001
            pass
    if not env.workload.terminal:
        env.workload = Workload.CANCELLED
    env.supervisor = Supervisor.FINISHED
    store.write_envelope(env)
    _emit(env, "destroy")
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
            build_envelope(status="ok", command="job list", jobs=rows),
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


def register(app: typer.Typer):
    job_app.command(name="plan")(plan)
    job_app.command(name="apply")(apply)
    job_app.command(name="status")(status)
    job_app.command(name="destroy")(destroy)
    job_app.command(name="list")(list_jobs)
    app.add_typer(job_app, name="job")
