"""PID identity that survives PID reuse.

`kill(pid, 0)` is not liveness for a multi-hour job: PIDs wrap. Identity
is (pid, starttime, boot_id) -- a reused PID gets a different start
time, and boot_id changes if the VM was replaced underneath us.

Linux (/proc) is the real target; Colab VMs are Linux. The `ps` fallback
exists so this prototype can be exercised on a developer machine instead
of being debugged for the first time on billed hardware.
"""

import os
import subprocess
import sys

_LINUX = sys.platform.startswith("linux") and os.path.isdir("/proc")


def boot_id() -> str:
    if _LINUX:
        try:
            with open("/proc/sys/kernel/random/boot_id") as f:
                return f.read().strip()
        except OSError:
            return "unknown"
    try:
        out = subprocess.run(
            ["sysctl", "-n", "kern.boottime"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _parse_stat(data: str) -> tuple:
    """`(state, starttime)` from the text of /proc/<pid>/stat."""
    # comm (field 2) can contain spaces and parens: split after the LAST ')'
    fields = data[data.rindex(")") + 2 :].split()
    return fields[0], fields[19]


def _is_gone(state: str) -> bool:
    """Z: exited, waiting for its parent to reap it. X: dead. A process
    the launch kernel started is never reaped while the job runs, so a
    killed runner stays Z, with its original start time, until the VM
    goes away."""
    return state[:1] in ("Z", "X", "x")


def _linux_state(pid: int):
    try:
        with open(f"/proc/{pid}/stat") as f:
            return _parse_stat(f.read())
    except (OSError, ValueError, IndexError):
        return None


def starttime(pid: int) -> str:
    """A value that changes when a PID is reused. "" if the pid is gone,
    including a zombie that has exited but not been reaped."""
    if pid <= 0:
        return ""
    if _LINUX:
        parsed = _linux_state(pid)
        if parsed is None or _is_gone(parsed[0]):
            return ""
        return parsed[1]
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "stat=", "-o", "lstart="],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    state, _, started = out.stdout.strip().partition(" ")
    if not state or _is_gone(state):
        return ""
    return started.strip()


def alive(pid: int, expect_starttime: str, expect_boot_id: str) -> bool:
    """True only if this exact process -- not a PID reusing its number."""
    if pid <= 0:
        return False
    if boot_id() != expect_boot_id:
        return False
    actual = starttime(pid)
    return bool(actual) and actual == expect_starttime


def descendants(pgid: int) -> list:
    """PIDs still alive in `pgid`, excluding ourselves.

    A double-forked/setsid'd process leaves the group, so this is a lower
    bound -- which is exactly the escape the spike is built to measure.
    """
    me = os.getpid()
    out = []
    if _LINUX:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            pid = int(entry)
            if pid == me:
                continue
            try:
                if os.getpgid(pid) != pgid:
                    continue
            except OSError:
                continue
            parsed = _linux_state(pid)
            if parsed is not None and not _is_gone(parsed[0]):
                out.append(pid)
        return out
    try:
        res = subprocess.run(
            ["ps", "-A", "-o", "pid=,pgid="],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return out
    for line in res.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            pid, pg = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        if pid != me and pg == pgid:
            out.append(pid)
    return out


JOB_ENV_VAR = "MIGHTY_JOB_ID"


def tagged_processes(job_id: str, exclude=()) -> list:
    """PIDs whose environment carries this job's id, excluding ourselves.

    This is the only sweep that finds a `setsid` escapee: it left the
    process group (so `descendants` is blind) and was reparented to init
    (so a ppid walk is blind), but a child cannot shed the environment
    it inherited. Linux-only -- reading another process's environ needs
    /proc, which is what Colab actually runs on.

    `exclude` is for the supervisor's *own* tagged processes -- notably the
    watchdog, which inherits `MIGHTY_JOB_ID` by design and is still alive
    when the verdict is written. Without it every single job reports a
    surviving descendant, and an alarm that fires every time is one
    operators learn to ignore -- which costs exactly the real escapee this
    sweep exists to catch.

    Returns [] on non-Linux, where this cannot be answered; callers MUST
    treat that as "unknown", never as "nothing survived".
    """
    if not _LINUX or not job_id:
        return []
    needle = f"{JOB_ENV_VAR}={job_id}\0".encode()
    skip = {os.getpid(), *exclude}
    out = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid in skip:
            continue
        try:
            with open(f"/proc/{pid}/environ", "rb") as f:
                if needle in f.read():
                    out.append(pid)
        except OSError:
            continue
    return out


def tagged_identities(job_id: str, exclude=()) -> list:
    """Tagged PIDs with starttime and boot_id captured in one pass."""
    boot = boot_id()
    out = []
    for pid in tagged_processes(job_id, exclude):
        started = starttime(pid)
        if started:
            out.append((pid, started, boot))
    return out


def signal_identities(identities, sig) -> list:
    """Signal only processes whose pid+starttime+boot_id still match.

    A reused PID has a different starttime, so it is skipped.
    """
    signaled = []
    for pid, started, boot in identities:
        if not alive(pid, started, boot):
            continue
        try:
            os.kill(pid, sig)
        except OSError:
            continue
        signaled.append(pid)
    return signaled


def signal_tagged(job_id: str, sig, exclude=()) -> list:
    """Scan tagged processes and signal those whose identity still matches."""
    return signal_identities(tagged_identities(job_id, exclude), sig)



def can_detect_escapees() -> bool:
    """Whether tagged_processes() can actually answer on this platform."""
    return _LINUX
