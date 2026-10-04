"""Sibling watchdog for a detached job attempt.

The watchdog is deliberately independent from the consumer and runner
process groups.  It reports resource state and enforces only the absolute
wall-clock deadline; inactivity is diagnostic and never a kill condition.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time

from . import GRACE_SECONDS, ident

INTERVAL_SECONDS = 30


def _atomic_write_json(path, payload):
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _safe_killpg(pgid, sig):
    """Signal a group without allowing an empty-group race to kill us."""
    try:
        os.killpg(pgid, sig)
        return True
    except OSError:
        return False


def _signal_escapees(job_dir, sig, runner_pid) -> None:
    """Signal the job's tagged processes, never this watchdog or the runner.

    The runner carries MIGHTY_JOB_ID too, but after a cancel or the
    deadline it is the process that reaps the workload, uploads artifacts
    and writes result.json; killing it loses all three.
    """
    job_id = os.path.basename(os.path.normpath(job_dir))
    exclude = {os.getpid()}
    if runner_pid > 0:
        exclude.add(runner_pid)
    try:
        ident.signal_tagged(job_id, sig, exclude=exclude)
    except Exception as error:  # noqa: BLE001 - watchdog must keep polling
        _log(f"signalling tagged processes with {sig} failed: {type(error).__name__}: {error}")


def _log(message):
    """One line to runner.log, which the watchdog shares with the runner."""
    print(f"[watchdog] {message}", file=sys.stderr, flush=True)



def _gpu_query():
    """Return (nvidia-smi query output, None), or (None, why) when there is
    no output: not installed, timed out, or a non-zero exit with its
    stderr, which is where a driver fault shows up."""
    try:
        proc = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,memory.free,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except FileNotFoundError:
        return None, "nvidia-smi not found"
    except (OSError, subprocess.SubprocessError) as error:
        return None, f"nvidia-smi failed: {type(error).__name__}: {error}"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()[:300]
        return None, f"nvidia-smi exited {proc.returncode}: {detail}"
    value = proc.stdout.strip()
    return (value, None) if value else (None, "nvidia-smi printed nothing")


def _disk_free(job_dir):
    """(free bytes, the path measured): the job directory, else "/"."""
    for path in (job_dir, "/"):
        try:
            return shutil.disk_usage(path).free, path
        except OSError:
            continue
    return None, None


def _inactivity(job_dir, now):
    """Report stale job files without making the state terminal."""
    newest = None
    try:
        for root, dirs, files in os.walk(job_dir):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for name in files:
                path = os.path.join(root, name)
                try:
                    mtime = os.stat(path).st_mtime
                except OSError:
                    continue
                newest = mtime if newest is None else max(newest, mtime)
    except OSError as error:
        _log(f"inactivity scan failed: {type(error).__name__}: {error}")
        return None
    if newest is None:
        return None
    return max(0.0, now - newest) >= INTERVAL_SECONDS


def _runner_identity(job_dir):
    """(pid, starttime, boot_id, deadline, started_at, error). `error` says
    why launch.json gave no usable pid; liveness is then unknown, not
    dead."""
    path = os.path.join(job_dir, "launch.json")
    error = None
    try:
        with open(path) as f:
            launch = json.load(f)
    except (OSError, ValueError) as exc:
        launch, error = {}, f"launch.json unreadable: {type(exc).__name__}: {exc}"
    if not isinstance(launch, dict):
        launch, error = {}, "launch.json is not an object"
    try:
        pid = int(launch.get("pid", -1))
    except (TypeError, ValueError):
        pid = -1
    if pid <= 0 and error is None:
        error = f"launch.json has no usable pid ({launch.get('pid')!r})"
    try:
        started = float(launch.get("started_at"))
    except (TypeError, ValueError):
        started = None
    return (
        pid,
        launch.get("starttime", ""),
        launch.get("boot_id", ""),
        launch.get("deadline"),
        started,
        error,
    )


def _record(job_dir, runner_alive, deadline, now, started, identity_error=None):
    # `elapsed`/`remaining` are derived here rather than by the supervisor:
    # the local clock may be minutes off the VM's, and "how long has this
    # been running" must be answered by the machine that is running it.
    gpu, gpu_error = _gpu_query()
    disk_free, disk_path = _disk_free(job_dir)
    _atomic_write_json(
        os.path.join(job_dir, "watchdog.json"),
        {
            "ts": now,
            "elapsed": round(now - started),
            "remaining": (round(deadline - now) if deadline is not None else None),
            "disk_free_bytes": disk_free,
            "disk_path": disk_path,
            "gpu": gpu,
            "gpu_error": gpu_error,
            # None when launch.json gave no usable identity: unknown, which
            # the supervisor must not read as a dead runner.
            "runner_alive": runner_alive,
            "runner_identity_error": identity_error,
            "deadline": deadline,
            "inactivity": _inactivity(job_dir, now),
        },
    )


def _cancel(job_dir, shim_pgid, now, runner_pid):
    # The runner may have already issued the same intent. Keep one durable
    # record and make cancellation one-shot.
    cancel_path = os.path.join(job_dir, "cancel.json")
    if not os.path.exists(cancel_path):
        _atomic_write_json(
            cancel_path,
            {"intent": "cancelled", "by": "wall_clock", "at": now},
        )
    _safe_killpg(shim_pgid, signal.SIGTERM)
    _signal_escapees(job_dir, signal.SIGTERM, runner_pid)


def main(argv):
    job_dir = None
    shim_pgid = None
    interval = INTERVAL_SECONDS
    i = 0
    while i < len(argv):
        if argv[i] == "--job-dir" and i + 1 < len(argv):
            job_dir = argv[i + 1]
            i += 2
        elif argv[i] == "--shim-pgid" and i + 1 < len(argv):
            shim_pgid = int(argv[i + 1])
            i += 2
        elif argv[i] == "--interval" and i + 1 < len(argv):
            interval = max(0.01, float(argv[i + 1]))
            i += 2
        else:
            print(
                "usage: watchdog --job-dir DIR --shim-pgid PGID [--interval S]",
                file=sys.stderr,
            )
            return 2
    if not job_dir or shim_pgid is None:
        print(
            "usage: watchdog --job-dir DIR --shim-pgid PGID [--interval S]",
            file=sys.stderr,
        )
        return 2

    cancel_sent = False
    kill_sent = False
    escalate_at = None
    # Fallback only. The authoritative start is the runner's own
    # `started_at` from `launch.json`: the watchdog boots *after* the
    # runner, so its own clock under-reports elapsed time, and it would be
    # a second slightly-wrong clock alongside the one the liveness check
    # already reads from that same record.
    watchdog_started = time.time()
    while True:
        now = time.time()
        (
            pid,
            expected_start,
            expected_boot,
            deadline,
            launched_at,
            identity_error,
        ) = _runner_identity(job_dir)
        runner_alive = (
            None
            if identity_error is not None
            else ident.alive(pid, expected_start, expected_boot)
        )
        try:
            deadline_value = float(deadline) if deadline is not None else None
        except (TypeError, ValueError):
            deadline_value = None
        try:
            _record(
                job_dir,
                runner_alive,
                deadline_value,
                now,
                launched_at if launched_at is not None else watchdog_started,
                identity_error,
            )
        except Exception as error:  # noqa: BLE001 - the deadline kill below must still run
            _log(f"watchdog.json not written: {type(error).__name__}: {error}")

        # A result means the runner has completed its durable work. Stop the
        # sibling rather than leave a detached process behind.
        if os.path.exists(os.path.join(job_dir, "result.json")):
            return 0

        cancel_requested = os.path.exists(os.path.join(job_dir, "cancel.json"))
        deadline_reached = deadline_value is not None and now >= deadline_value
        if not cancel_sent and (cancel_requested or deadline_reached):
            if cancel_requested:
                _safe_killpg(shim_pgid, signal.SIGTERM)
                _signal_escapees(job_dir, signal.SIGTERM, pid)
            else:
                _cancel(job_dir, shim_pgid, now, pid)
            cancel_sent = True
            escalate_at = now + GRACE_SECONDS
        elif not kill_sent and escalate_at is not None and now >= escalate_at:
            _safe_killpg(shim_pgid, signal.SIGKILL)
            _signal_escapees(job_dir, signal.SIGKILL, pid)
            kill_sent = True


        # Poll more frequently than the report cadence only when a deadline is
        # close. This keeps the normal 30-second report contract while making
        # budget enforcement responsive.
        sleep_for = interval
        if deadline_value is not None and not kill_sent:
            until_deadline = max(0.05, deadline_value - time.time())
            sleep_for = min(sleep_for, until_deadline)
        if escalate_at is not None and not kill_sent:
            until_escalation = max(0.05, escalate_at - time.time())
            sleep_for = min(sleep_for, until_escalation)
        time.sleep(sleep_for)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
