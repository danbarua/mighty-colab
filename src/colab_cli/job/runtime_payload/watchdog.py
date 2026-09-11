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

from . import ident

INTERVAL_SECONDS = 30
GRACE_SECONDS = 5


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


def _signal_escapees(job_dir, sig) -> None:
    job_id = os.path.basename(os.path.normpath(job_dir))
    try:
        ident.signal_tagged(job_id, sig, exclude={os.getpid()})
    except Exception:  # noqa: BLE001 - watchdog must keep polling
        pass



def _gpu_query():
    """Return the nvidia-smi query output, or None when unavailable."""
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
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    value = proc.stdout.strip()
    return value or None


def _disk_free_bytes(job_dir):
    try:
        return shutil.disk_usage(job_dir).free
    except OSError:
        try:
            return shutil.disk_usage("/").free
        except OSError:
            return None


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
    except OSError:
        return None
    if newest is None:
        return None
    return max(0.0, now - newest) >= INTERVAL_SECONDS


def _runner_identity(job_dir):
    launch = _read_json(os.path.join(job_dir, "launch.json")) or {}
    try:
        pid = int(launch.get("pid", -1))
    except (TypeError, ValueError):
        pid = -1
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
    )


def _record(job_dir, runner_alive, deadline, now, started):
    # `elapsed`/`remaining` are derived here rather than by the supervisor:
    # the local clock may be minutes off the VM's, and "how long has this
    # been running" must be answered by the machine that is running it.
    _atomic_write_json(
        os.path.join(job_dir, "watchdog.json"),
        {
            "ts": now,
            "elapsed": round(now - started),
            "remaining": (round(deadline - now) if deadline is not None else None),
            "disk_free_bytes": _disk_free_bytes(job_dir),
            "gpu": _gpu_query(),
            "runner_alive": runner_alive,
            "deadline": deadline,
            "inactivity": _inactivity(job_dir, now),
        },
    )


def _cancel(job_dir, shim_pgid, now):
    # The runner may have already issued the same intent. Keep one durable
    # record and make cancellation one-shot.
    cancel_path = os.path.join(job_dir, "cancel.json")
    if not os.path.exists(cancel_path):
        _atomic_write_json(
            cancel_path,
            {"cancelled_by": "wall_clock", "at": now},
        )
    _safe_killpg(shim_pgid, signal.SIGTERM)
    _signal_escapees(job_dir, signal.SIGTERM)


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
        pid, expected_start, expected_boot, deadline, launched_at = _runner_identity(
            job_dir
        )
        runner_alive = ident.alive(pid, expected_start, expected_boot)
        try:
            deadline_value = float(deadline) if deadline is not None else None
        except (TypeError, ValueError):
            deadline_value = None
        _record(
            job_dir,
            runner_alive,
            deadline_value,
            now,
            launched_at if launched_at is not None else watchdog_started,
        )

        # A result means the runner has completed its durable work. Stop the
        # sibling rather than leave a detached process behind.
        if os.path.exists(os.path.join(job_dir, "result.json")):
            return 0

        cancel_requested = os.path.exists(os.path.join(job_dir, "cancel.json"))
        deadline_reached = deadline_value is not None and now >= deadline_value
        if not cancel_sent and (cancel_requested or deadline_reached):
            if cancel_requested:
                _safe_killpg(shim_pgid, signal.SIGTERM)
                _signal_escapees(job_dir, signal.SIGTERM)
            else:
                _cancel(job_dir, shim_pgid, now)
            cancel_sent = True
            escalate_at = now + GRACE_SECONDS
        elif not kill_sent and escalate_at is not None and now >= escalate_at:
            _safe_killpg(shim_pgid, signal.SIGKILL)
            _signal_escapees(job_dir, signal.SIGKILL)
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
