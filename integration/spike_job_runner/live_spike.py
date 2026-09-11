#!/usr/bin/env python3
"""Live spike: does the detached runner + Contents-API polling loop
actually produce a reliable verdict on a real Colab VM?

This answers the empirical questions in docs/08_job.md's Spike section
that no local run can: whether a kernel RPC can start a process that
outlives it, whether the kernel really goes IDLE, and whether the
verdict can be read back without ever calling execute_code again.

Cheap by construction: one CPU session, short payloads, unconditional
teardown. Run:  uv run python integration/spike_job_runner/live_spike.py
"""

import json
import os
import subprocess
import sys
import tarfile
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
SESSION = os.environ.get("SPIKE_SESSION", "spike-job-runner")
REMOTE_ROOT = "/content/jobs"
CLI = ["uv", "run", "mighty-colab", "--auth=adc"]

findings = []
failures = []


def cli(*args, timeout=300, check=True):
    proc = subprocess.run(
        [*CLI, *args], cwd=REPO, capture_output=True, text=True, timeout=timeout
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"cli {args[:3]} exit={proc.returncode}\n"
            f"stdout={proc.stdout[-1500:]}\nstderr={proc.stderr[-1500:]}"
        )
    return proc


def cli_json(*args, **kw):
    proc = cli("--json", *args, **kw)
    for line in reversed(proc.stdout.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise RuntimeError(f"no JSON in output: {proc.stdout[-800:]}")


def exec_snippet(code, timeout=300):
    """Run code in the kernel via a real file (exec -f), returning stdout."""
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, dir=HERE) as f:
        f.write(code)
        path = f.name
    try:
        proc = cli("exec", "-s", SESSION, "-f", path, "--timeout", "120", timeout=timeout)
        return proc.stdout
    finally:
        os.unlink(path)


def make_bundle():
    bundle = os.path.join(tempfile.gettempdir(), "mighty_spike_bundle.tar.gz")
    with tarfile.open(bundle, "w:gz") as tar:
        tar.add(os.path.join(HERE, "mighty_runtime"), arcname="mighty_runtime")
        tar.add(os.path.join(HERE, "payload.py"), arcname="payload.py")
        tar.add(os.path.join(HERE, "sibling_helper.py"), arcname="sibling_helper.py")
    return bundle


def poll_result(job, attempts=60, interval=2):
    """Read result.json through the Contents API ONLY -- never execute_code.

    That restriction is the whole point: if the verdict needs a live
    kernel call, the design's 'kernel goes IDLE' claim is worthless.
    """
    remote = f"{REMOTE_ROOT}/{job}/result.json"
    local = os.path.join(tempfile.gettempdir(), f"spike-{job}-result.json")
    for _ in range(attempts):
        if os.path.exists(local):
            os.unlink(local)
        proc = cli("download", "-s", SESSION, remote, local, check=False, timeout=120)
        if proc.returncode == 0 and os.path.exists(local):
            try:
                with open(local) as f:
                    return json.load(f)
            except ValueError:
                pass
        time.sleep(interval)
    return None


def launch(job, mode, seconds, deadline=None, extra=""):
    job_dir = f"{REMOTE_ROOT}/{job}"
    deadline_arg = f'"--deadline", "{deadline}", ' if deadline else ""
    code = f'''
import subprocess, sys, os, time
os.makedirs("{job_dir}", exist_ok=True)
p = subprocess.Popen(
    [sys.executable, "-m", "mighty_runtime.runner",
     "--job-dir", "{job_dir}", {deadline_arg}
     "{REMOTE_ROOT}/src/payload.py", "--mode", "{mode}", "--seconds", "{seconds}"{extra}],
    cwd="{REMOTE_ROOT}/src",
    start_new_session=True,
    stdout=open("{job_dir}/runner.log", "w"),
    stderr=subprocess.STDOUT,
)
print("LAUNCHED_PID", p.pid)
'''
    t0 = time.time()
    out = exec_snippet(code)
    elapsed = time.time() - t0
    pid = None
    for line in out.splitlines():
        if line.startswith("LAUNCHED_PID"):
            pid = int(line.split()[1])
    return pid, elapsed


def check(job, result, want_workload, want_exit=None, want_sig=None, want_exc=None):
    if result is None:
        failures.append(f"{job}: no result.json via Contents API")
        print(f"[FAIL] {job}: no result.json")
        return
    problems = []
    if result["workload"] != want_workload:
        problems.append(f"workload={result['workload']} want={want_workload}")
    if want_exit is not None and result["exit_code"] != want_exit:
        problems.append(f"exit={result['exit_code']} want={want_exit}")
    if want_sig is not None and result["signal"] != want_sig:
        problems.append(f"signal={result['signal']} want={want_sig}")
    if want_exc is not None:
        has = result.get("exception") is not None
        if has != want_exc:
            problems.append(f"exception={has} want={want_exc}")
    if result.get("runner_error"):
        problems.append(f"runner_error={result['runner_error']}")
    if problems:
        failures.append(f"{job}: {'; '.join(problems)}")
        print(f"[FAIL] {job}: {'; '.join(problems)}")
    else:
        print(f"[ok]   {job}: {result['workload']} exit={result['exit_code']} signal={result['signal']}")


def main():
    print(f"=== live spike, session={SESSION}")
    my_endpoint = None
    try:
        info = cli_json("new", "-s", SESSION, timeout=600)
        my_endpoint = info.get("endpoint")
        print(f"provisioned endpoint={my_endpoint} accel={info.get('accelerator')}")

        bundle = make_bundle()
        cli("exec", "-s", SESSION, "-f", os.devnull, "--timeout", "60", check=False)
        exec_snippet(f'import os; os.makedirs("{REMOTE_ROOT}/src", exist_ok=True); print("MKDIR_OK")')
        cli("upload", "-s", SESSION, bundle, f"{REMOTE_ROOT}/src/bundle.tar.gz", timeout=600)
        out = exec_snippet(
            f'import tarfile, os\n'
            f'with tarfile.open("{REMOTE_ROOT}/src/bundle.tar.gz") as t:\n'
            f'    t.extractall("{REMOTE_ROOT}/src")\n'
            f'print("EXTRACTED", sorted(os.listdir("{REMOTE_ROOT}/src")))\n'
        )
        print(out.strip().splitlines()[-1])

        # 1. Launch must return fast and leave the kernel IDLE.
        pid, elapsed = launch("ok", "ok", 20)
        print(f"launch RPC returned in {elapsed:.1f}s, runner pid={pid}")
        if elapsed > 15:
            findings.append(f"launch RPC took {elapsed:.1f}s (expected ~1-3s)")

        # 2. Kernel must be answerable while the workload runs.
        status = cli("status", "-s", SESSION, check=False, timeout=120)
        print(f"kernel status during run: {'BUSY' if 'BUSY' in status.stdout else 'IDLE/other'}")
        if "BUSY" in status.stdout:
            findings.append("kernel reported BUSY while workload ran (should be IDLE)")

        check("ok", poll_result("ok"), "succeeded", 0, None, False)

        # 3. Exit shapes, on real Linux /proc.
        for job, mode, want, wexit, wsig, wexc in [
            ("crash", "crash", "failed", 1, None, True),
            ("osexit", "osexit", "failed", 7, None, False),
            ("sigkill", "sigkill", "failed", None, 9, False),
            ("sysexit0", "sysexit0", "succeeded", 0, None, False),
            ("sibling", "sibling", "succeeded", 0, None, False),
        ]:
            launch(job, mode, 2)
            check(job, poll_result(job), want, wexit, wsig, wexc)

        # 4. wall_clock kill must be `cancelled`, with intent recorded.
        launch("wall", "quiet", 120, deadline=5)
        wall = poll_result("wall")
        check("wall", wall, "cancelled")
        if wall and not wall.get("cancel_intent"):
            failures.append("wall: no cancel_intent recorded")

        # 5. Escapee: does a setsid grandchild survive a clean verdict?
        launch("escape", "doublefork", 2)
        esc = poll_result("escape")
        check("escape", esc, "succeeded", 0, None, False)
        alive = exec_snippet(
            'import os\n'
            'p = "/tmp/mighty_spike_escapee.log"\n'
            'if os.path.exists(p):\n'
            '    pid = int(open(p).read().split("pid=")[1].split()[0])\n'
            '    try:\n'
            '        os.kill(pid, 0); print("ESCAPEE_ALIVE", pid)\n'
            '    except OSError: print("ESCAPEE_DEAD")\n'
            'else: print("NO_ESCAPEE")\n'
        )
        if "ESCAPEE_ALIVE" in alive:
            findings.append(
                "setsid descendant outlived a 'succeeded' verdict on the VM "
                "and was invisible to the process-group scan (cgroup/subreaper needed)"
            )
            print("[FINDING] escapee survived on the VM")
        elif "NO_ESCAPEE" in alive:
            failures.append("escape case produced no escapee on the VM")

        # 6. Duplicate launch must not start a second consumer.
        launch("dup", "ok", 12)
        time.sleep(3)
        pid2, _ = launch("dup", "ok", 12)
        dup_log = exec_snippet(
            f'print(open("{REMOTE_ROOT}/dup/runner.log").read()[-400:])'
        )
        if "duplicate launch" in dup_log:
            print("[ok]   dup: second runner refused")
        else:
            failures.append("dup: duplicate-launch guard did not fire on the VM")
            print("[FAIL] dup: guard did not fire")
        poll_result("dup")

    except Exception as e:
        failures.append(f"driver error: {type(e).__name__}: {e}")
        print(f"[ERROR] {e}")
    finally:
        # NEVER gate teardown on `created`: if `new` timed out after the
        # server already made the assignment, `created` is False and the
        # VM is billing anyway. AGENTS.md #22 -- end with `sessions`
        # clean, whatever happened above.
        #
        # But ONLY ever unassign the endpoint THIS driver provisioned.
        # Other assignments on the account may be someone else's work --
        # a concurrent long-running spike, another agent, a human's
        # notebook -- and a blind sweep would destroy them.
        print("--- teardown")
        stop = cli_json("stop", "-s", SESSION, check=False, timeout=300)
        print(f"stop: status={stop.get('status')} reason={stop.get('reason')}")

        def _ours(entry):
            # my_endpoint is None exactly when `new` timed out -- which
            # is the case this teardown exists for -- so the session
            # NAME has to be part of the match, not just the endpoint.
            return (
                (my_endpoint is not None and entry.get("endpoint") == my_endpoint)
                or entry.get("name") == SESSION
            )

        def _sweep():
            entries = cli_json("sessions", check=False, timeout=180).get("sessions") or []
            return [e for e in entries if _ours(e)], [e for e in entries if not _ours(e)]

        mine, others = _sweep()
        if mine:
            print(f"[WARN] our assignment(s) still listed; unassigning: "
                  f"{[e.get('endpoint') for e in mine]}")
            for entry in mine:
                endpoint = entry.get("endpoint")
                if not endpoint:
                    continue
                subprocess.run(
                    [
                        "uv", "run", "python", "-c",
                        "from colab_cli.common import state;"
                        f"state.client.unassign({endpoint!r})",
                    ],
                    cwd=REPO, capture_output=True, text=True, timeout=180,
                )
            mine, others = _sweep()
        if mine:
            failures.append(f"ORPHAN: {[e.get('endpoint') for e in mine]} still assigned")
            print(f"[FAIL] ours still assigned: {[e.get('endpoint') for e in mine]}")
        else:
            print("our assignment released")
        if others:
            print(
                "[note] NOT ours, left running on purpose: "
                f"{[(e.get('name'), e.get('endpoint')) for e in others]}"
            )

    print()
    for f_ in findings:
        print(f"[FINDING] {f_}")
    if failures:
        print(f"\n{len(failures)} FAILURE(S):")
        for f_ in failures:
            print(f"  - {f_}")
        return 1
    print("\nlive spike passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
