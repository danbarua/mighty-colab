#!/usr/bin/env python3
"""Controlled A/B for the ~61min job loss found by token_lifetime_spike.py.

That run showed `launch.json`/`result.json` both 404 at t=61min while
`stop` returned `reason=None` -- the assignment was still LISTED, but
`/content` was gone. Two different failure shapes look identical from a
single silent session:
  (a) VM-side activity keeps it alive (bonsai-2026's prior experience);
  (b) everything dies around t=60min regardless of activity.

Split them in ONE billing window: two sessions, identical quiet payload,
one with a watchdog writing watchdog.json every 30s, one writing
NOTHING. Each poll also records whether the assignment is still listed
in `sessions`, so "VM recycled under a surviving assignment" (distinct
outcome) is not conflated with "assignment itself was reaped".

  uv run python integration/spike_job_runner/token_lifetime_ab_spike.py
Env: SPIKE_MINUTES (default 75).
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
MINUTES = float(os.environ.get("SPIKE_MINUTES", "75"))
REMOTE_ROOT = "/content/jobs"
CLI = ["uv", "run", "mighty-colab", "--auth=adc"]
LOG = os.path.join(tempfile.gettempdir(), "mighty_token_ab_spike.log")

ARMS = [
    {"session": "spike-ab-watchdog", "job": "wd", "watchdog": True},
    {"session": "spike-ab-silent", "job": "silent", "watchdog": False},
]


def say(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def cli(*args, session=None, timeout=300, check=True):
    cmd = list(CLI)
    if session is not None:
        cmd += ["-s", session]
    cmd += list(args)
    proc = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, timeout=timeout)
    if check and proc.returncode != 0:
        raise RuntimeError(f"cli {args[:2]} exit={proc.returncode} {proc.stderr[-800:]}")
    return proc


def cli_json(*args, session=None, **kw):
    proc = cli("--json", *args, session=session, **kw)
    for line in reversed(proc.stdout.strip().splitlines()):
        if line.strip().startswith("{"):
            return json.loads(line.strip())
    raise RuntimeError(f"no JSON: {proc.stdout[-500:]}")


def exec_snippet(session, code, timeout=300):
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, dir=HERE) as f:
        f.write(code)
        path = f.name
    try:
        return cli("exec", "-f", path, "--timeout", "120", session=session, timeout=timeout).stdout
    finally:
        os.unlink(path)


def provision(arm):
    info = cli_json("new", session=arm["session"], timeout=600)
    arm["endpoint"] = info.get("endpoint")
    say(f"[{arm['job']}] provisioned {arm['endpoint']} accel={info.get('accelerator')}")

    bundle = os.path.join(tempfile.gettempdir(), f"mighty_ab_bundle_{arm['job']}.tar.gz")
    with tarfile.open(bundle, "w:gz") as tar:
        tar.add(os.path.join(HERE, "mighty_runtime"), arcname="mighty_runtime")
        tar.add(os.path.join(HERE, "payload.py"), arcname="payload.py")
    exec_snippet(arm["session"], f'import os; os.makedirs("{REMOTE_ROOT}/src", exist_ok=True); print("OK")')
    cli("upload", bundle, f"{REMOTE_ROOT}/src/b.tar.gz", session=arm["session"], timeout=600)
    exec_snippet(
        arm["session"],
        f'import tarfile\n'
        f'with tarfile.open("{REMOTE_ROOT}/src/b.tar.gz") as t: t.extractall("{REMOTE_ROOT}/src")\n'
        f'print("EXTRACTED")\n',
    )


def launch(arm, run_seconds):
    job_dir = f"{REMOTE_ROOT}/{arm['job']}"
    watchdog_snippet = ""
    if arm["watchdog"]:
        # A separate detached process writing watchdog.json every 30s --
        # not the runner itself, so a hung runner wouldn't fake liveness.
        watchdog_snippet = f'''
wd = subprocess.Popen(
    [sys.executable, "-c",
     "import json,os,time\\n"
     "p = {job_dir + '/watchdog.json'!r}\\n"
     "while True:\\n"
     "    with open(p + '.tmp', 'w') as f: json.dump({{'t': time.time()}}, f)\\n"
     "    os.replace(p + '.tmp', p)\\n"
     "    time.sleep(30)\\n"],
    start_new_session=True,
    stdout=open("{job_dir}/watchdog.log", "w"), stderr=subprocess.STDOUT,
)
print("WATCHDOG_PID", wd.pid)
'''
    code = f'''
import subprocess, sys, os
os.makedirs("{job_dir}", exist_ok=True)
{watchdog_snippet}
p = subprocess.Popen(
    [sys.executable, "-m", "mighty_runtime.runner", "--job-dir", "{job_dir}",
     "{REMOTE_ROOT}/src/payload.py", "--mode", "quiet", "--seconds", "{run_seconds}"],
    cwd="{REMOTE_ROOT}/src", start_new_session=True,
    stdout=open("{job_dir}/runner.log", "w"), stderr=subprocess.STDOUT)
print("LAUNCHED_PID", p.pid)
'''
    out = exec_snippet(arm["session"], code)
    say(f"[{arm['job']}] {out.strip()}")


def poll_arm(arm, mins):
    """One observation: is the assignment listed, and is launch.json readable."""
    sess = cli_json("sessions", check=False, timeout=180)
    listed = arm["endpoint"] in [s.get("endpoint") for s in (sess.get("sessions") or [])]

    local = os.path.join(tempfile.gettempdir(), f"ab-{arm['job']}-launch.json")
    if os.path.exists(local):
        os.unlink(local)
    proc = cli(
        "download", f"{REMOTE_ROOT}/{arm['job']}/launch.json", local,
        session=arm["session"], check=False, timeout=180,
    )
    files_ok = proc.returncode == 0 and os.path.exists(local)

    say(
        f"[{arm['job']}] t+{mins:.0f}min assignment_listed={listed} "
        f"files_readable={'OK' if files_ok else 'FAIL'}"
        + ("" if files_ok else f" err={proc.stderr.strip()[-200:]}")
    )
    return {"mins": round(mins, 1), "listed": listed, "files_ok": files_ok}


def teardown(arm, failures):
    stop = cli_json("stop", session=arm["session"], check=False, timeout=300)
    say(f"[{arm['job']}] stop: status={stop.get('status')} reason={stop.get('reason')}")
    sess = cli_json("sessions", check=False, timeout=180)
    listed = [s for s in (sess.get("sessions") or []) if s.get("endpoint") == arm.get("endpoint")]
    if listed:
        subprocess.run(
            ["uv", "run", "python", "-c",
             f"from colab_cli.common import state; state.client.unassign({arm['endpoint']!r})"],
            cwd=REPO, capture_output=True, text=True, timeout=180,
        )
        sess = cli_json("sessions", check=False, timeout=180)
        listed = [s for s in (sess.get("sessions") or []) if s.get("endpoint") == arm.get("endpoint")]
    if listed:
        failures.append(f"[{arm['job']}] ORPHAN: {arm['endpoint']} still assigned")
        say(f"[{arm['job']}] [FAIL] still assigned: {arm['endpoint']}")
    else:
        say(f"[{arm['job']}] released")


def main():
    failures = []
    observations = {arm["job"]: [] for arm in ARMS}
    provisioned = []
    try:
        for arm in ARMS:
            provision(arm)
            provisioned.append(arm)

        run_seconds = int(MINUTES * 60) - 120
        for arm in ARMS:
            launch(arm, run_seconds)
        say(f"both arms launched; polling every 5min for {MINUTES}min")

        started = time.time()
        while (time.time() - started) < MINUTES * 60:
            time.sleep(300)
            mins = (time.time() - started) / 60
            for arm in ARMS:
                observations[arm["job"]].append(poll_arm(arm, mins))

        for arm in ARMS:
            res_local = os.path.join(tempfile.gettempdir(), f"ab-{arm['job']}-result.json")
            proc = cli(
                "download", f"{REMOTE_ROOT}/{arm['job']}/result.json", res_local,
                session=arm["session"], check=False, timeout=180,
            )
            if proc.returncode == 0 and os.path.exists(res_local):
                with open(res_local) as f:
                    result = json.load(f)
                say(f"[{arm['job']}] VERDICT READABLE: {result['workload']}")
            else:
                say(f"[{arm['job']}] VERDICT UNREADABLE")
    except Exception as e:
        failures.append(f"driver error: {type(e).__name__}: {e}")
        say(f"ERROR {e}")
    finally:
        for arm in provisioned:
            teardown(arm, failures)

    say("=== summary ===")
    for job, obs in observations.items():
        say(f"{job}: {obs}")
    watchdog_wins = None
    wd_obs = observations.get("wd", [])
    silent_obs = observations.get("silent", [])
    if wd_obs and silent_obs:
        wd_last_ok = max([o["mins"] for o in wd_obs if o["files_ok"]], default=0)
        silent_last_ok = max([o["mins"] for o in silent_obs if o["files_ok"]], default=0)
        watchdog_wins = wd_last_ok > silent_last_ok
        say(f"watchdog arm survived to {wd_last_ok}min, silent arm to {silent_last_ok}min")
        say(f"CONCLUSION: activity keeps it alive = {watchdog_wins}")
    if failures:
        say(f"{len(failures)} FAILURE(S): {failures}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
