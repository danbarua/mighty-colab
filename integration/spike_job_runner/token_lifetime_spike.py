#!/usr/bin/env python3
"""SUPERSEDED 2026-09-11 by `token_discriminator_spike.py`. Kept for the
raw data only; do NOT re-run it to answer anything.

This script observed the failure but could not explain it, and its first
write-up drew the wrong conclusion ("the VM was reset under a live
assignment"). The discriminator showed that fresh assignment metadata
restored access and that the files remained intact. Because `adopt` refreshes
both the token and proxy endpoint, it did not distinguish token expiry from
endpoint rebinding. See `docs/job/design.md`'s long-run section.

Original docstring follows.

Half of doc step 6: does Contents-API polling remain usable for a multi-hour job?
SCOPE -- READ THIS BEFORE BELIEVING A GREEN RESULT.
This run measures the Contents-poll half ONLY. It does NOT exercise
`control.result.put_url`: the spike runner has no signed-URL PUT, and
minting one needs a service-account signer this CLI deliberately does
not have (ordinary user ADC cannot sign -- see the design's non-goals).
So:
  - poll survives  -> Contents polling outlives one token. Says NOTHING
    about whether the durable push works, because none was attempted.
  - poll dies      -> the durable push is REQUIRED, and still unproven.
Either way the `control.*` half needs a caller-supplied signed PUT/GET
pair against a real bucket before doc step 6 is closed.

If polling dies and nothing else holds the verdict, an unattended
multi-hour job silently loses its result -- which is the entire use case
`job` exists for. Everything in the short spike ran inside one token's
life, so it proved nothing about this.
Env: SPIKE_MINUTES (default 75), SPIKE_SESSION.
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
SESSION = os.environ.get("SPIKE_SESSION", "spike-token-life")
MINUTES = float(os.environ.get("SPIKE_MINUTES", "75"))
REMOTE_ROOT = "/content/jobs"
JOB = "longrun"
CLI = ["uv", "run", "mighty-colab", "--auth=adc"]
LOG = os.path.join(tempfile.gettempdir(), "mighty_token_spike.log")


def say(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def cli(*args, timeout=300, check=True):
    proc = subprocess.run(
        [*CLI, *args], cwd=REPO, capture_output=True, text=True, timeout=timeout
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"cli {args[:3]} exit={proc.returncode} {proc.stderr[-800:]}")
    return proc


def cli_json(*args, **kw):
    proc = cli("--json", *args, **kw)
    for line in reversed(proc.stdout.strip().splitlines()):
        if line.strip().startswith("{"):
            return json.loads(line.strip())
    raise RuntimeError(f"no JSON: {proc.stdout[-500:]}")


def exec_snippet(code, timeout=300):
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, dir=HERE) as f:
        f.write(code)
        path = f.name
    try:
        return cli("exec", "-s", SESSION, "-f", path, "--timeout", "120", timeout=timeout).stdout
    finally:
        os.unlink(path)


def main():
    created = False
    observations = []
    first_failure_min = None
    try:
        info = cli_json("new", "-s", SESSION, timeout=600)
        created = True
        say(f"provisioned {info.get('endpoint')} accel={info.get('accelerator')}")

        bundle = os.path.join(tempfile.gettempdir(), "mighty_token_bundle.tar.gz")
        with tarfile.open(bundle, "w:gz") as tar:
            tar.add(os.path.join(HERE, "mighty_runtime"), arcname="mighty_runtime")
            tar.add(os.path.join(HERE, "payload.py"), arcname="payload.py")
        exec_snippet(f'import os; os.makedirs("{REMOTE_ROOT}/src", exist_ok=True); print("OK")')
        cli("upload", "-s", SESSION, bundle, f"{REMOTE_ROOT}/src/b.tar.gz", timeout=600)
        exec_snippet(
            f'import tarfile\n'
            f'with tarfile.open("{REMOTE_ROOT}/src/b.tar.gz") as t: t.extractall("{REMOTE_ROOT}/src")\n'
            f'print("EXTRACTED")\n'
        )

        run_seconds = int(MINUTES * 60) - 120
        job_dir = f"{REMOTE_ROOT}/{JOB}"
        code = f'''
import subprocess, sys, os
os.makedirs("{job_dir}", exist_ok=True)
p = subprocess.Popen(
    [sys.executable, "-m", "mighty_runtime.runner", "--job-dir", "{job_dir}",
     "{REMOTE_ROOT}/src/payload.py", "--mode", "quiet", "--seconds", "{run_seconds}"],
    cwd="{REMOTE_ROOT}/src", start_new_session=True,
    stdout=open("{job_dir}/runner.log", "w"), stderr=subprocess.STDOUT)
print("LAUNCHED_PID", p.pid)
'''
        out = exec_snippet(code)
        say(f"launched: {[ln for ln in out.splitlines() if 'LAUNCHED_PID' in ln]}")
        say(f"workload runs {run_seconds}s; polling every 5 min for {MINUTES} min")

        started = time.time()
        local = os.path.join(tempfile.gettempdir(), "token-spike-launch.json")
        while (time.time() - started) < MINUTES * 60:
            time.sleep(300)
            mins = (time.time() - started) / 60
            if os.path.exists(local):
                os.unlink(local)
            # launch.json exists from t=0, so a read failure here is the
            # transport dying, not the file being absent.
            proc = cli(
                "download", "-s", SESSION,
                f"{job_dir}/launch.json", local,
                check=False, timeout=180,
            )
            ok = proc.returncode == 0 and os.path.exists(local)
            observations.append((round(mins, 1), ok))
            say(f"t+{mins:.0f}min contents_read={'OK' if ok else 'FAIL'}"
                + ("" if ok else f" err={proc.stderr.strip()[-300:]}"))
            if not ok and first_failure_min is None:
                first_failure_min = mins
                say(f"*** FIRST CONTENTS FAILURE at t+{mins:.0f}min ***")
                # Disambiguate, or the whole run is uninterpretable: a
                # dead keep-alive daemon gets the VM idle-reaped, and
                # that produces a Contents error indistinguishable from
                # an access-binding failure.
                try:
                    sess = cli_json("sessions", check=False, timeout=180)
                    names = [s.get("endpoint") for s in (sess.get("sessions") or [])]
                    say(f"  attribution: assignment_still_listed={bool(names)} {names}")
                except Exception as e:  # noqa: BLE001
                    say(f"  attribution: sessions query failed: {e}")
                try:
                    st = cli_json("status", "-s", SESSION, check=False, timeout=180)
                    info = st.get("session") or {}
                    ka_pid = info.get("keep_alive_pid")
                    ka_alive = None
                    if ka_pid:
                        try:
                            os.kill(int(ka_pid), 0)
                            ka_alive = True
                        except OSError:
                            ka_alive = False
                    say(
                        f"  attribution: keep_alive_pid={ka_pid} alive={ka_alive} "
                        f"last_ping={info.get('last_keep_alive_ping')}"
                    )
                except Exception as e:  # noqa: BLE001
                    say(f"  attribution: status query failed: {e}")
                say(
                    "  observation: assignment and keep-alive state alone cannot "
                    "distinguish token expiry from endpoint rebinding"
                )

        # Read the verdict again at the end of the observation window.
        res_local = os.path.join(tempfile.gettempdir(), "token-spike-result.json")
        proc = cli("download", "-s", SESSION, f"{job_dir}/result.json", res_local,
                   check=False, timeout=180)
        if proc.returncode == 0 and os.path.exists(res_local):
            with open(res_local) as f:
                result = json.load(f)
            say(f"VERDICT READABLE after {MINUTES}min: workload={result['workload']} "
                f"exit={result['exit_code']}")
            say("RESULT: Contents verdict readable at the final sample")
        else:
            say(f"VERDICT UNREADABLE: {proc.stderr.strip()[-400:]}")
            say("RESULT: architecture NEEDS the control.result.put_url durable push")
    except Exception as e:
        say(f"ERROR {type(e).__name__}: {e}")
    finally:
        if created:
            stop = cli_json("stop", "-s", SESSION, check=False, timeout=300)
            say(f"stop: {stop.get('status')} {stop.get('reason')}")
            left = cli_json("sessions", check=False, timeout=180).get("sessions") or []
            say(f"orphans: {left if left else 'none'}")
    say(f"observations: {observations}")
    say(f"first_failure_min: {first_failure_min}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
