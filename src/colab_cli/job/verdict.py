"""What a runner result means for the caller's next action.

Pure functions from the fields of the runner's result.json to the
envelope's `reason` and `retry_class`; `Orchestrator.absorb_result`
applies them. The policy:

* Transfers (a data GET while staging, an artifact PUT at offload) are
  classified by HTTP status when there was a response: 401 and 403 ->
  refresh_urls; 404 -> fix_code for a GET, refresh_urls for a PUT; 408,
  429 and 5xx -> retry_same; any other 4xx (413 included) -> fix_code.
  Without a response, by the runner's category: network -> retry_same;
  checksum, size and local disk errors -> fix_code; a non-public address
  -> fix_human; the runner's own manifest or credential channel ->
  do_not_retry; anything unrecognised -> retry_same.
* Several failed uploads take the strongest class, in the order
  do_not_retry, fix_human, fix_code, refresh_urls, retry_same: a spec
  change means a new plan with new URLs, and new URLs cover a retry.
* A failed or stopped workload decides the class over any upload failure;
  the reason names both.
* A wall_clock kill -> fix_code (the run outgrew its budget). A signal
  with no cancel request (the OOM killer's SIGKILL, a stray SIGTERM) ->
  fix_code. A cancel by a person or tool carries no class.
* A runner that could not reach a verdict (an exception in its own loop,
  no escapee detection) -> do_not_retry: the fault is in the supervisor.
* A failed Colab assign for one accelerator: 400, 401 and 403 ->
  fix_human; 408 and 429 -> retry_same; 5xx and any other status ->
  retry_different; no response -> retry_same; anything else ->
  do_not_retry. When every candidate fails, the job takes the strongest.
"""

from __future__ import annotations

import logging
from typing import Iterable, List, Literal, Optional, Sequence, Tuple

from colab_cli.job.models import (
    ArtifactItem,
    ArtifactResult,
    InputResult,
    Offload,
    RetryClass,
    TransferError,
)
from colab_cli.job.runtime_payload import GRACE_SECONDS

_logger = logging.getLogger(__name__)

Method = Literal["GET", "PUT"]
Outcome = Tuple[Optional[str], Optional[RetryClass]]

# Longest exception message quoted in a reason; the full text is in
# `exception` and runner.log.
EXCEPTION_MESSAGE_CHARS = 500

_CATEGORY_RETRY = {
    "network": RetryClass.RETRY_SAME,
    "checksum": RetryClass.FIX_CODE,
    "size": RetryClass.FIX_CODE,
    "local": RetryClass.FIX_CODE,
    "blocked": RetryClass.FIX_HUMAN,
    "setup": RetryClass.DO_NOT_RETRY,
}

_STRENGTH = (
    RetryClass.DO_NOT_RETRY,
    RetryClass.FIX_HUMAN,
    RetryClass.FIX_CODE,
    RetryClass.RETRY_DIFFERENT,
    RetryClass.REFRESH_URLS,
    RetryClass.RETRY_SAME,
)


def transfer_retry_class(error: TransferError, method: Method) -> RetryClass:
    if error.http_status is not None:
        status = error.http_status
        if status in (401, 403):
            return RetryClass.REFRESH_URLS
        if status == 404:
            return RetryClass.FIX_CODE if method == "GET" else RetryClass.REFRESH_URLS
        if status in (408, 429) or 500 <= status < 600:
            return RetryClass.RETRY_SAME
        if 400 <= status < 500:
            return RetryClass.FIX_CODE
        return RetryClass.RETRY_SAME
    if error.category is None:
        _logger.warning(
            "transfer error has no category (exception=%s reason=%s); "
            "classified as retry_same",
            error.exception,
            error.reason,
        )
    return _CATEGORY_RETRY.get(error.category, RetryClass.RETRY_SAME)


def assign_retry_class(http_status: Optional[int], network: bool) -> RetryClass:
    """A failed Colab assign for one accelerator. 400 is Colab's answer for
    an accelerator the account has no quota or entitlement for; 401 and
    403 are the account's credentials or scope. A 5xx is taken as capacity,
    where another accelerator is usually granted sooner. A failure with no
    HTTP response and no network error is a fault in mighty-colab's client."""

    if http_status is not None:
        if http_status in (400, 401, 403):
            return RetryClass.FIX_HUMAN
        if http_status in (408, 429):
            return RetryClass.RETRY_SAME
        return RetryClass.RETRY_DIFFERENT
    return RetryClass.RETRY_SAME if network else RetryClass.DO_NOT_RETRY


def strongest(classes: Iterable[Optional[RetryClass]]) -> Optional[RetryClass]:
    present = {c for c in classes if c is not None}
    return next((c for c in _STRENGTH if c in present), None)


def cancel_source(intent) -> Optional[str]:
    """Who asked for the cancel, from cancel.json's `by`."""
    if isinstance(intent, dict):
        by = intent.get("by")
        return by if isinstance(by, str) and by else None
    return None


def _signal_label(result: dict) -> str:
    name = result.get("signal_name")
    number = result.get("signal")
    return f"{name} ({number})" if name else f"signal {number}"


def _how_it_stopped(result: dict) -> str:
    if result.get("signal_name") == "SIGKILL":
        return (
            f"the workload did not exit within {GRACE_SECONDS}s of SIGTERM "
            "and was killed by SIGKILL"
        )
    if result.get("signal") is not None:
        return f"the workload was stopped by {_signal_label(result)}"
    if result.get("exit_code") is not None:
        return f"the workload exited {result['exit_code']} after SIGTERM"
    return "the workload's exit was not observed"


def _oom_note(result: dict) -> Optional[str]:
    kills = result.get("oom_kills")
    if not isinstance(kills, int):
        return None
    if kills == 0:
        return (
            "no kernel OOM kill during the run"
            if result.get("signal_name") == "SIGKILL"
            else None
        )
    note = f"the kernel's out-of-memory killer ran {kills} time(s) during the run"
    log = result.get("oom_log") or []
    return f"{note}: {log[-1]}" if log else note


def _with(reason: str, *notes: Optional[str]) -> str:
    return "; ".join([reason, *(n for n in notes if n)])


def workload_outcome(result: dict, wall_clock: int) -> Outcome:
    """Reason and retry class for the workload's own verdict."""
    workload = result.get("workload", "unknown")
    if workload == "succeeded":
        return None, None
    if workload == "cancelled":
        intent = result.get("cancel_intent")
        by = cancel_source(intent)
        stopped = _how_it_stopped(result)
        if by == "wall_clock":
            return (
                f"wall_clock budget of {wall_clock}s reached; {stopped}",
                RetryClass.FIX_CODE,
            )
        if by is not None:
            return f"cancelled by {by}; {stopped}", None
        error = intent.get("error") if isinstance(intent, dict) else None
        source = (
            f"cancelled (cancel.json unreadable: {error})"
            if error
            else "cancelled (cancel.json names no requester)"
        )
        return f"{source}; {stopped}", None
    if workload == "unknown":
        runner_error = result.get("runner_error")
        reason = (
            f"the runner could not reach a verdict: {runner_error}"
            if runner_error
            else "the runner reported no verdict"
        )
        return _with(reason, _oom_note(result)), RetryClass.DO_NOT_RETRY
    # failed
    exit_code = result.get("exit_code")
    tagged = result.get("survivors_by_job_tag") or []
    exception = result.get("exception")
    if result.get("signal") is not None:
        reason = (
            f"the workload was killed by {_signal_label(result)} "
            "with no cancel request"
        )
    elif exit_code == 0 and tagged:
        reason = (
            f"the workload exited 0 but {len(tagged)} tagged process(es) "
            f"survived containment: {', '.join(str(p) for p in tagged)}"
        )
    elif isinstance(exception, dict) and exception.get("type"):
        message = str(exception.get("message") or "")
        if len(message) > EXCEPTION_MESSAGE_CHARS:
            message = (
                message[:EXCEPTION_MESSAGE_CHARS]
                + f" [... {len(message) - EXCEPTION_MESSAGE_CHARS} characters "
                "omitted; the full message is in `exception`]"
            )
        reason = f"the workload exited {exit_code}: {exception['type']}: {message}"
    else:
        reason = (
            f"the workload exited {exit_code} without a recorded exception; "
            "its output is in runner.log"
        )
    return _with(reason, _oom_note(result)), RetryClass.FIX_CODE


def stage_outcome(inputs: Sequence[InputResult], exception) -> Outcome:
    """Reason and retry class for a staging failure; the consumer never ran."""
    failed = next((i for i in inputs if i.status == "failed" and i.error), None)
    if failed is not None:
        return (
            f"staging failed at {failed.dest} ({failed.url_id}): "
            f"{failed.error.summary}. The consumer never started.",
            transfer_retry_class(failed.error, "GET"),
        )
    detail = "no detail recorded"
    if isinstance(exception, dict) and exception.get("message"):
        detail = exception["message"]
    return (
        f"staging failed before any input was fetched: {detail}. "
        "The consumer never started.",
        RetryClass.DO_NOT_RETRY,
    )


def offload_outcome(
    declared: Sequence[ArtifactItem],
    artifacts: Sequence[ArtifactResult],
    remote_offload: Optional[str],
    offload_error: Optional[str],
) -> Tuple[Offload, Optional[str], Optional[RetryClass]]:
    """Offload status, reason and retry class for a run that reached
    offload. `declared` is not empty."""
    status = {a.path: a.status for a in artifacts}
    missing = sorted(
        d.path for d in declared if d.required and status.get(d.path, "missing") == "missing"
    )
    failed = sorted((a for a in artifacts if a.status == "failed"), key=lambda a: a.path)
    parts: List[str] = []
    classes: List[RetryClass] = []
    if offload_error:
        parts.append(f"artifact offload could not start: {offload_error}")
        classes.append(RetryClass.DO_NOT_RETRY)
    if missing:
        # Declared artifact paths are caller-chosen relative paths, not
        # signed URLs: safe to name, and naming them saves a round trip.
        parts.append(f"required artifact(s) not produced: {', '.join(missing)}")
        classes.append(RetryClass.FIX_CODE)
    if failed:
        parts.append(
            "artifact offload failed: "
            + "; ".join(f"{a.path} ({a.error.summary})" if a.error else a.path for a in failed)
        )
        for a in failed:
            if a.error is None:
                _logger.warning(
                    "failed artifact %s has no error record; classified as retry_same",
                    a.path,
                )
                classes.append(RetryClass.RETRY_SAME)
            else:
                classes.append(transfer_retry_class(a.error, "PUT"))
    if not parts and remote_offload == "failed":
        parts.append("artifact offload failed")
        classes.append(RetryClass.RETRY_SAME)
    if not parts:
        return Offload.OK, None, None
    return Offload.FAILED, "; ".join(parts), strongest(classes)
