#!/usr/bin/env python3
"""Known-answer check for the spike runtime, run locally (no VM, no cost).

Each row is a payload mode plus the verdict the runner MUST produce. If
the local run cannot get these right, running it on a billed A100 only
adds latency to the same bug.

Stdlib only -- run it with the system interpreter, NOT `uv run`:

    python3 integration/spike_job_runner/local_check.py

The project venv pulls pyarrow (35MB, via Google's jupyter-kernel-client
-> jupyter-mimetypes) which nothing here imports and which has already
cost one 4-minute build timeout. Only `live_spike.py` needs the venv,
because it shells out to the `mighty-colab` CLI.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile


HERE_LINUX = sys.platform.startswith("linux") and os.path.isdir("/proc")
_LINUX = HERE_LINUX
ESCAPEE_LOG = "/tmp/mighty_spike_escapee.log"
HERE = os.path.dirname(os.path.abspath(__file__))

# mode, extra args, expected workload, expected exit, expected signal,
# expect exception.json?, expect survivors?
CASES = [
    ("ok", [], "succeeded", 0, None, False, False),
    ("sysexit0", [], "succeeded", 0, None, False, False),
    ("exitcode", ["--code", "3"], "failed", 3, None, True, False),
    ("crash", [], "failed", 1, None, True, False),
    ("quiet", [], "succeeded", 0, None, False, False),
    ("osexit", [], "failed", 7, None, False, False),
    ("sigkill", [], "failed", None, 9, False, False),
    ("sibling", [], "succeeded", 0, None, False, False),
    # doublefork: the escapee calls setsid(), so a process-group scan is
    # BY CONSTRUCTION blind to it, and it is reparented to init so a
    # ppid walk is too. The job-tag environ sweep DOES find it -- but
    # only on Linux, where another process's environ is readable. On
    # macOS that question is unanswerable, which is "unknown", not
    # "nothing survived".
    ("doublefork", [], "succeeded", 0, None, False, _LINUX),
]


def run_case(mode, extra, seconds="1"):
    job_dir = tempfile.mkdtemp(prefix=f"mighty-spike-{mode}-")
    cmd = [
        sys.executable,
        "-m",
        "mighty_runtime.runner",
        "--job-dir",
        job_dir,
        os.path.join(HERE, "payload.py"),
        "--mode",
        mode,
        "--seconds",
        seconds,
        *extra,
    ]
    # Redirect to FILES, not pipes. A double-forked escapee inherits the
    # runner's stdout fd and holds it open long after the runner has
    # exited and written its verdict, so a pipe read blocks forever.
    # That is a real property of the escape case, not a harness quirk --
    # it is exactly why the verdict lives in result.json and never in
    # "the stream closed".
    log_path = os.path.join(job_dir, "runner.log")
    with open(log_path, "w") as log:
        proc = subprocess.run(
            cmd, cwd=HERE, stdout=log, stderr=subprocess.STDOUT, timeout=180
        )
    with open(log_path) as f:
        output = f.read()
    result_path = os.path.join(job_dir, "result.json")
    result = None
    if os.path.exists(result_path):
        with open(result_path) as f:
            result = json.load(f)
    return result, proc, output, job_dir


def main():
    failures = []
    # A stale escapee log from an aborted run makes the next run read a
    # dead PID and report a false "escapee did not survive". Note the
    # escapee's cmdline is `payload.py --mode doublefork` -- the log
    # path appears nowhere in it, so `pkill -f mighty_spike_escapee`
    # never matches. Just unlink: killing a PID read from a stale file
    # can SIGKILL whatever unrelated process has since reused that
    # number, and the live escapee is already killed on the normal path.
    if os.path.exists(ESCAPEE_LOG):
        os.unlink(ESCAPEE_LOG)
    for mode, extra, want_workload, want_exit, want_sig, want_exc, want_surv in CASES:
        result, proc, output, job_dir = run_case(mode, extra)
        if result is None:
            failures.append(f"{mode}: no result.json (output={output[-400:]})")
            continue

        problems = []
        if result["workload"] != want_workload:
            problems.append(f"workload={result['workload']} want={want_workload}")
        if result["exit_code"] != want_exit:
            problems.append(f"exit={result['exit_code']} want={want_exit}")
        if result["signal"] != want_sig:
            problems.append(f"signal={result['signal']} want={want_sig}")
        has_exc = result.get("exception") is not None
        if has_exc != want_exc:
            problems.append(f"exception={has_exc} want={want_exc}")
        has_surv = bool(result.get("surviving_descendants"))
        if has_surv != want_surv:
            problems.append(
                f"survivors={result.get('surviving_descendants')} want_any={want_surv}"
            )
        if result.get("cancel_intent") is not None:
            problems.append("unexpected cancel_intent")

        status = "FAIL" if problems else "ok"
        print(f"[{status}] {mode}: {'; '.join(problems) if problems else result['workload']}")
        if problems:
            failures.append(f"{mode}: {'; '.join(problems)}")
        shutil.rmtree(job_dir, ignore_errors=True)

    # The escape itself: after doublefork reported `succeeded` with an
    # empty group scan, is the grandchild actually still running? If it
    # is, "clean terminal state" was a lie and the process group is
    # provably not a containment boundary -- the [open] item in the
    # design that cgroup/subreaper has to close.
    escaped = os.path.exists(ESCAPEE_LOG)
    escapee_pid = None
    if escaped:
        with open(ESCAPEE_LOG) as f:
            escapee_pid = int(f.read().split("pid=")[1].split()[0])
        try:
            os.kill(escapee_pid, 0)
            still_running = True
        except OSError:
            still_running = False
        if still_running:
            where = (
                "found by the job-tag environ sweep"
                if _LINUX
                else "UNDETECTABLE here (no /proc); Linux would catch it by job tag"
            )
            print(
                f"[FINDING] escapee pid={escapee_pid} outlived a 'succeeded' verdict; "
                f"invisible to the process-group scan, {where}"
            )
            os.kill(escapee_pid, 9)
        else:
            failures.append("doublefork escapee did not survive; case proved nothing")
        os.unlink(ESCAPEE_LOG)
    else:
        failures.append("doublefork produced no escapee; case proved nothing")

    # Duplicate-launch guard: a second runner against a live job dir must
    # not start a second consumer.
    job_dir = tempfile.mkdtemp(prefix="mighty-spike-dup-")
    base = [
        sys.executable,
        "-m",
        "mighty_runtime.runner",
        "--job-dir",
        job_dir,
        os.path.join(HERE, "payload.py"),
        "--mode",
        "ok",
        "--seconds",
        "6",
    ]
    first = subprocess.Popen(base, cwd=HERE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    import time

    time.sleep(2)
    second = subprocess.run(base, cwd=HERE, capture_output=True, text=True, timeout=60)
    first.wait(timeout=60)
    if "duplicate launch" not in second.stdout:
        failures.append(f"duplicate-launch guard did not fire: {second.stdout!r}")
        print(f"[FAIL] duplicate: {second.stdout!r}")
    else:
        print("[ok] duplicate: second runner refused")
    shutil.rmtree(job_dir, ignore_errors=True)

    # wall_clock: deadline must kill AND be classified cancelled, not failed.
    job_dir = tempfile.mkdtemp(prefix="mighty-spike-wall-")
    subprocess.run(
        [
            sys.executable,
            "-m",
            "mighty_runtime.runner",
            "--job-dir",
            job_dir,
            "--deadline",
            "3",
            os.path.join(HERE, "payload.py"),
            "--mode",
            "quiet",
            "--seconds",
            "120",
        ],
        cwd=HERE,
        capture_output=True,
        text=True,
        timeout=180,
    )
    with open(os.path.join(job_dir, "result.json")) as f:
        wr = json.load(f)
    if wr["workload"] != "cancelled" or not wr.get("cancel_intent"):
        failures.append(f"wall_clock: {wr['workload']} intent={wr.get('cancel_intent')}")
        print(f"[FAIL] wall_clock: {wr['workload']} intent={wr.get('cancel_intent')}")
    else:
        print(f"[ok] wall_clock: cancelled by {wr['cancel_intent']['cancelled_by']}")
    shutil.rmtree(job_dir, ignore_errors=True)

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S):")
        for f_ in failures:
            print(f"  - {f_}")
        return 1
    print("all local known-answer checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
