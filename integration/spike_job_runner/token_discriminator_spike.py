#!/usr/bin/env python3
"""Tests whether fresh assignment metadata restores lost Contents access.

Background: `token_lifetime_spike.py` lost Contents access at t+61min with
404s on files written at t=0. The first write-up incorrectly concluded that
the VM had been recycled under a live assignment. Issue #3 identifies
runtime-proxy token expiry as one mechanism that causes periodic 401/404s.

The discriminating action here is:

    at first failure, run `adopt <ENDPOINT> --keep-alive`, retry the read
      files come back  -> access binding refreshed; files remained intact
      files still gone  -> filesystem loss remains possible

`adopt` both mints a token and re-resolves the proxy endpoint. A successful
retry therefore does not distinguish token expiry from endpoint rebinding.
The script reports only what that intervention proves.

  uv run python integration/spike_job_runner/token_discriminator_spike.py
Env: SPIKE_MINUTES (default 80, needs to exceed ~60), SPIKE_SESSION.
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
SESSION = os.environ.get("SPIKE_SESSION", "spike-token-discriminator")
MINUTES = float(os.environ.get("SPIKE_MINUTES", "80"))
REMOTE_ROOT = "/content/jobs"
JOB = "discriminate"
CLI = ["uv", "run", "mighty-colab", "--auth=adc"]
LOG = os.path.join(tempfile.gettempdir(), "mighty_token_discriminator.log")


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
        raise RuntimeError(f"cli {args[:2]} exit={proc.returncode} {proc.stderr[-600:]}")
    return proc


def cli_json(*args, **kw):
    proc = cli("--json", *args, **kw)
    for line in reversed(proc.stdout.strip().splitlines()):
        if line.strip().startswith("{"):
            return json.loads(line.strip())
    raise RuntimeError(f"no JSON: {proc.stdout[-400:]}")


def exec_snippet(code, timeout=300):
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, dir=HERE) as f:
        f.write(code)
        path = f.name
    try:
        return cli("exec", "-s", SESSION, "-f", path, "--timeout", "120", timeout=timeout).stdout
    finally:
        os.unlink(path)


def try_read(label):
    """Read launch.json (written at t=0) via Contents. Never refreshes."""
    local = os.path.join(tempfile.gettempdir(), f"disc-{label}.json")
    if os.path.exists(local):
        os.unlink(local)
    proc = cli(
        "download", "-s", SESSION, f"{REMOTE_ROOT}/{JOB}/launch.json", local,
        check=False, timeout=180,
    )
    ok = proc.returncode == 0 and os.path.exists(local)
    err = "" if ok else (proc.stderr or proc.stdout).strip()[-250:]
    return ok, err


def main():
    endpoint = None
    verdict = "INCONCLUSIVE"
    try:
        info = cli_json("new", "-s", SESSION, timeout=600)
        endpoint = info.get("endpoint")
        say(f"provisioned {endpoint} accel={info.get('accelerator')}")

        bundle = os.path.join(tempfile.gettempdir(), "mighty_disc_bundle.tar.gz")
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

        run_seconds = int(MINUTES * 60) + 600  # outlive the observation window
        job_dir = f"{REMOTE_ROOT}/{JOB}"
        out = exec_snippet(f'''
import subprocess, sys, os
os.makedirs("{job_dir}", exist_ok=True)
p = subprocess.Popen(
    [sys.executable, "-m", "mighty_runtime.runner", "--job-dir", "{job_dir}",
     "{REMOTE_ROOT}/src/payload.py", "--mode", "quiet", "--seconds", "{run_seconds}"],
    cwd="{REMOTE_ROOT}/src", start_new_session=True,
    stdout=open("{job_dir}/runner.log", "w"), stderr=subprocess.STDOUT)
print("LAUNCHED_PID", p.pid)
''')
        say(f"launched: {[ln for ln in out.splitlines() if 'LAUNCHED_PID' in ln]}")

        ok, err = try_read("t0")
        say(f"t+0min baseline read: {'OK' if ok else 'FAIL ' + err}")
        if not ok:
            raise RuntimeError("baseline read failed; experiment invalid")

        started = time.time()
        first_fail_min = None
        while (time.time() - started) < MINUTES * 60:
            time.sleep(300)
            mins = (time.time() - started) / 60
            ok, err = try_read(f"t{int(mins)}")
            say(f"t+{mins:.0f}min read={'OK' if ok else 'FAIL'}{'' if ok else ' err=' + err}")
            if ok:
                continue

            # ---- THE DISCRIMINATING MOMENT ----
            first_fail_min = mins
            say(f"*** FIRST FAILURE at t+{mins:.0f}min -- discriminating now ***")

            listed = [
                s.get("endpoint")
                for s in (cli_json("sessions", check=False, timeout=180).get("sessions") or [])
            ]
            say(f"  assignment still listed: {endpoint in listed}")

            say(f"  running: adopt {endpoint} --keep-alive")
            ad = cli("adopt", endpoint, "-n", SESSION, "--keep-alive", check=False, timeout=300)
            say(f"  adopt rc={ad.returncode} {(ad.stdout or ad.stderr).strip()[-200:]}")

            ok2, err2 = try_read("after-adopt")
            say(f"  retry after adopt: {'OK' if ok2 else 'FAIL ' + err2}")

            if ok2:
                verdict = "ACCESS_BINDING_REFRESH"
                say("  VERDICT: fresh assignment metadata restored Contents access.")
                say("  => files remained intact; token refresh and endpoint rebinding are confounded.")
            else:
                verdict = "FILESYSTEM_LOSS"
                say("  VERDICT: files still unreadable after a successful re-adopt.")
                say("  => genuine runtime/filesystem loss; activity hypothesis back on the table.")
            break

        if first_fail_min is None:
            verdict = "NO_FAILURE"
            say(f"no Contents failure within the {MINUTES}min observation window")
    except Exception as e:
        say(f"ERROR {type(e).__name__}: {e}")
    finally:
        stop = cli_json("stop", "-s", SESSION, check=False, timeout=300)
        say(f"stop: {stop.get('status')} {stop.get('reason')}")
        left = [
            s.get("endpoint")
            for s in (cli_json("sessions", check=False, timeout=180).get("sessions") or [])
        ]
        if endpoint and endpoint in left:
            subprocess.run(
                ["uv", "run", "python", "-c",
                 f"from colab_cli.common import state; state.client.unassign({endpoint!r})"],
                cwd=REPO, capture_output=True, text=True, timeout=180,
            )
            left = [
                s.get("endpoint")
                for s in (cli_json("sessions", check=False, timeout=180).get("sessions") or [])
            ]
        say(f"ours released: {endpoint not in left}; others untouched: {[e for e in left if e != endpoint]}")
    say(f"VERDICT={verdict}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
