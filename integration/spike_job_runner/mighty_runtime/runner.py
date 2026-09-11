"""Parent of the consumer. Owns the verdict.

Contract points this prototype exists to prove:
  - launch.json is created O_EXCL BEFORE the child starts, so a retried
    launch RPC is a no-op instead of a second consumer.
  - the shim runs in its OWN process group, so cancelling the workload
    cannot kill the process that has to write the verdict.
  - result.json is written atomically on every exit path.
  - a signal death is only `cancelled` when a cancel intent record
    exists; otherwise it is `failed` (OOM is not a cancellation).

Usage:
  python -m mighty_runtime.runner --job-dir DIR [--deadline SECS] entry.py [args...]
"""

import json
import os
import signal
import subprocess
import sys
import time

from mighty_runtime import SCHEMA_VERSION, ident


# Seconds between SIGTERM and SIGKILL when a budget kill fires.
GRACE_SECONDS = 5


def _safe_killpg(pgid, sig):
    """Signal a process group without ever raising.

    An already-empty group is not an error: BSD returns EPERM (not
    ESRCH) once the last member is gone, so catching ProcessLookupError
    alone lets a routine race kill the runner before it writes the
    verdict. Any OSError here means "nothing left to signal".
    """
    try:
        os.killpg(pgid, sig)
        return True
    except OSError:
        return False

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


def main(argv):
    job_dir = None
    deadline_secs = None
    rest = []
    i = 0
    while i < len(argv):
        if argv[i] == "--job-dir":
            job_dir = argv[i + 1]
            i += 2
        elif argv[i] == "--deadline":
            deadline_secs = float(argv[i + 1])
            i += 2
        else:
            rest = argv[i:]
            break
    if not job_dir or not rest:
        print("usage: runner --job-dir DIR [--deadline S] entry.py", file=sys.stderr)
        return 2

    entry, script_args = rest[0], rest[1:]
    os.makedirs(job_dir, exist_ok=True)
    launch_path = os.path.join(job_dir, "launch.json")
    result_path = os.path.join(job_dir, "result.json")

    started = time.time()
    deadline = started + deadline_secs if deadline_secs else None

    # O_EXCL: if a launch record already exists for a live runner, this
    # invocation is a duplicate (lost RPC reply, retried call) and must
    # NOT start a second consumer.
    try:
        fd = os.open(launch_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        prior = _read_json(launch_path) or {}
        if ident.alive(
            prior.get("pid", -1),
            prior.get("starttime", ""),
            prior.get("boot_id", ""),
        ):
            print(f"[runner] duplicate launch; live runner pid={prior.get('pid')}")
            return 0
        os.replace(launch_path, os.path.join(job_dir, "launch.json.stale"))
        fd = os.open(launch_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)

    me = os.getpid()
    with os.fdopen(fd, "w") as f:
        json.dump(
            {
                "schema_version": SCHEMA_VERSION,
                "pid": me,
                "pgid": os.getpgid(me),
                "starttime": ident.starttime(me),
                "boot_id": ident.boot_id(),
                "attempt": int(os.environ.get("MIGHTY_ATTEMPT", "1")),
                "deadline": deadline,
                "started_at": started,
            },
            f,
            indent=2,
        )

    # The shim gets its OWN session. If it shared ours, killpg on the
    # workload would also kill this process before it could record why.
    #
    # MIGHTY_JOB_ID tags the whole descendant tree. A setsid escapee
    # leaves the process group and is reparented to init, so neither a
    # pgid scan nor a ppid walk can see it -- but it cannot shed the
    # environment it inherited, so an environ sweep can.
    job_id = os.path.basename(os.path.normpath(job_dir))
    child_env = dict(os.environ)
    child_env[ident.JOB_ENV_VAR] = job_id
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "mighty_runtime.shim",
            "--job-dir",
            job_dir,
            entry,
            *script_args,
        ],
        start_new_session=True,
        stdout=sys.stdout,
        stderr=sys.stderr,
        env=child_env,
    )
    shim_pgid = os.getpgid(proc.pid)

    exit_code = None
    term_signal = None
    runner_error = None
    term_sent = False
    kill_sent = False
    escalate_at = None

    # INVARIANT: nothing below may prevent result.json from being
    # written. A runner that dies in its own kill path produces a
    # spurious `unknown` -- the one terminal value an agent cannot act
    # on -- caused by cleanup rather than by the workload.
    try:
        while True:
            done_pid, status = os.waitpid(proc.pid, os.WNOHANG)
            if done_pid == proc.pid:
                if os.WIFSIGNALED(status):
                    term_signal = os.WTERMSIG(status)
                elif os.WIFEXITED(status):
                    exit_code = os.WEXITSTATUS(status)
                break
            now = time.time()
            if deadline and now > deadline:
                if not term_sent:
                    _atomic_write_json(
                        os.path.join(job_dir, "cancel.json"),
                        {"cancelled_by": "wall_clock", "at": now},
                    )
                    _safe_killpg(shim_pgid, signal.SIGTERM)
                    term_sent = True
                    escalate_at = now + GRACE_SECONDS
                elif not kill_sent and now > escalate_at:
                    # Only escalate if SIGTERM did not do the job.
                    _safe_killpg(shim_pgid, signal.SIGKILL)
                    kill_sent = True
            time.sleep(0.2)
    except BaseException as e:  # noqa: BLE001 - verdict must still land
        runner_error = f"{type(e).__name__}: {e}"

    # Every probe below is best-effort: a `ps`/proc hiccup must not eat
    # the verdict, which is the same hole that already cost one cycle.
    survivors, tagged, intent, exception = [], [], None, None
    detect_ok = False
    try:
        survivors = ident.descendants(shim_pgid)
    except Exception:  # noqa: BLE001
        pass
    try:
        tagged = ident.tagged_processes(job_id)
        detect_ok = ident.can_detect_escapees()
    except Exception:  # noqa: BLE001
        pass
    try:
        intent = _read_json(os.path.join(job_dir, "cancel.json"))
    except Exception:  # noqa: BLE001
        pass
    try:
        exception = _read_json(os.path.join(job_dir, "exception.json"))
    except Exception:  # noqa: BLE001
        pass

    # A setsid escapee is invisible to the pgid scan but keeps the
    # inherited MIGHTY_JOB_ID, so the union is the real answer. On a
    # platform that cannot read another process's environ this is
    # "unknown", never "nothing survived".
    all_survivors = sorted(set(survivors) | set(tagged))

    if runner_error is not None and exit_code is None and term_signal is None:
        workload = "unknown"
    elif term_signal is not None:
        workload = "cancelled" if intent else "failed"
    elif exit_code == 0:
        workload = "succeeded"
    else:
        workload = "failed"

    _atomic_write_json(
        result_path,
        {
            "schema_version": SCHEMA_VERSION,
            "workload": workload,
            "exit_code": exit_code,
            "signal": term_signal,
            "cancel_intent": intent,
            "exception": exception,
            "surviving_descendants": all_survivors,
            "survivors_by_pgid": survivors,
            "survivors_by_job_tag": tagged,
            "escapee_detection_available": detect_ok,
            "runner_error": runner_error,
            "attempt": int(os.environ.get("MIGHTY_ATTEMPT", "1")),
            "started_at": started,
            "finished_at": time.time(),
        },
    )
    print(
        f"[runner] {workload} exit={exit_code} signal={term_signal} "
        f"survivors={survivors} err={runner_error}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
