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


def starttime(pid: int) -> str:
    """A value that changes when a PID is reused. "" if the pid is gone."""
    if pid <= 0:
        return ""
    if _LINUX:
        try:
            with open(f"/proc/{pid}/stat") as f:
                data = f.read()
        except OSError:
            return ""
        # comm (field 2) can contain spaces and parens: split after the LAST ')'
        try:
            after_comm = data[data.rindex(")") + 2 :]
            return after_comm.split()[19]
        except (ValueError, IndexError):
            return ""
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


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
                if os.getpgid(pid) == pgid:
                    out.append(pid)
            except OSError:
                continue
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


def tagged_processes(job_id: str) -> list:
    """PIDs whose environment carries this job's id, excluding ourselves.

    This is the only sweep that finds a `setsid` escapee: it left the
    process group (so `descendants` is blind) and was reparented to init
    (so a ppid walk is blind), but a child cannot shed the environment
    it inherited. Linux-only -- reading another process's environ needs
    /proc, which is what Colab actually runs on.

    Returns [] on non-Linux, where this cannot be answered; callers MUST
    treat that as "unknown", never as "nothing survived".
    """
    if not _LINUX or not job_id:
        return []
    needle = f"{JOB_ENV_VAR}={job_id}\0".encode()
    me = os.getpid()
    out = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid == me:
            continue
        try:
            with open(f"/proc/{pid}/environ", "rb") as f:
                if needle in f.read():
                    out.append(pid)
        except OSError:
            continue
    return out


def can_detect_escapees() -> bool:
    """Whether tagged_processes() can actually answer on this platform."""
    return _LINUX
