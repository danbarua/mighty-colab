# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Does a job's VM survive without the keep-alive daemon?

Three CPU jobs run the same idle workload (a sleep loop) for SOAK_SECONDS:

  A  --no-keepalive; apply SIGKILLed after launch, so nobody reads the VM
  B  --no-keepalive; `apply --async` left running, reading the VM every 15 s
  C  keep-alive on; apply SIGKILLed after launch, the daemon left running

SIGKILL, not SIGTERM: apply hands a job to a detached `job status --poll`
on SIGTERM or SIGHUP, which would make A and C supervised.

One observer lists the account's assignments every OBSERVE_SECONDS with
its own empty session file, so it never touches the runtime proxy. The
listing goes to Colab's assignment API, and if Colab counts that as
activity it does so for all three jobs alike. For C it also checks the
daemon pid and the age of its last recorded ping: a VM lost after the
daemon died says nothing about keep-alive.

When the workload is due to have ended, `job status --poll` collects A's
and C's verdicts and releases their VMs; B's apply does that itself. Every
VM still listed when the script exits is destroyed.

This measures; it does not pass or fail. It exits non-zero only when the
harness itself cannot run a variant or cannot confirm the VMs are gone.
"""

import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SOAK_SECONDS = int(os.environ.get("SOAK_SECONDS", "10800"))
OBSERVE_SECONDS = int(os.environ.get("OBSERVE_SECONDS", "300"))
LAUNCH_TIMEOUT = 1200
RESULT_TIMEOUT = 1800
ROOT = Path(os.environ["SOAK_ROOT"]).resolve()

VARIANTS = {
    "A": {"keep_alive": False, "supervised": False,
          "label": "--no-keepalive, unattended"},
    "B": {"keep_alive": False, "supervised": True,
          "label": "--no-keepalive, apply --async polling"},
    "C": {"keep_alive": True, "supervised": False,
          "label": "keep-alive on, unattended"},
}


def now() -> float:
    return time.time()


def stamp(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or now(), timezone.utc).strftime("%H:%M:%S")


def log(message: str) -> None:
    line = f"{stamp()} {message}"
    print(line, flush=True)
    with open(ROOT / "soak.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def mc(config: Path, *args: str, timeout: int = 600) -> subprocess.CompletedProcess:
    cmd = ["uv", "run", "mighty-colab", "--auth=adc", "--config", str(config), *args]
    return subprocess.run(
        cmd, cwd=REPO_ROOT, capture_output=True, text=True, timeout=timeout
    )


def envelope_line(stdout: str) -> dict:
    for line in reversed(stdout.splitlines()):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and "command" in value:
            return value
    raise RuntimeError(f"no --json envelope in: {stdout[-500:]!r}")


def listed_endpoints(observer: Path) -> set[str] | None:
    """The account's assignments, or None when the listing failed."""
    try:
        result = mc(observer, "--json", "sessions", timeout=120)
        sessions = envelope_line(result.stdout).get("sessions", [])
    except Exception as error:  # noqa: BLE001 - recorded; the next observation retries
        log(f"observer: listing failed: {type(error).__name__}: {error}")
        return None
    return {s.get("endpoint") for s in sessions}


def log_compute_units(observer: Path) -> None:
    """The account's compute-unit balance, to see when Colab updates it."""
    try:
        result = mc(observer, "--json", "usage", timeout=120)
        ccu = envelope_line(result.stdout)
        log(f"observer: compute units balance={ccu.get('current_balance')} "
            f"rate={ccu.get('consumption_rate_hourly')}/h "
            f"assignments={ccu.get('assignments_count')}")
    except Exception as error:  # noqa: BLE001 - recorded; the next observation retries
        log(f"observer: usage failed: {type(error).__name__}: {error}")


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class Job:
    def __init__(self, name: str, spec: dict):
        self.name = name
        self.spec = spec
        self.dir = ROOT / name
        self.dir.mkdir(parents=True, exist_ok=True)
        self.config = self.dir / "sessions.json"
        self.job_id: str | None = None
        self.apply_pid: int | None = None
        self.endpoint: str | None = None
        self.launched_at: float | None = None
        self.last_listed: float | None = None
        self.lost_at: float | None = None
        self.daemon_pid: int | None = None
        self.daemon_died_at: float | None = None
        self.finished = False
        self.outcome: dict | None = None
        self.deferred_reason: str | None = None
        self.collect_deadline: float | None = None
        self.collector: subprocess.Popen | None = None

    @property
    def job_dir(self) -> Path:
        return self.dir / "jobs" / str(self.job_id)

    def envelope(self) -> dict | None:
        try:
            return json.loads((self.job_dir / "envelope.json").read_text())
        except (OSError, ValueError):
            return None

    def session(self) -> dict | None:
        try:
            sessions = json.loads(self.config.read_text())
        except (OSError, ValueError):
            return None
        return next(
            (s for s in sessions.values() if s.get("endpoint") == self.endpoint), None
        )

    def plan_and_apply(self) -> None:
        (self.dir / "sleeper.py").write_text(
            "import time\n"
            f"end = time.time() + {SOAK_SECONDS}\n"
            "i = 0\n"
            "while time.time() < end:\n"
            "    print(f'tick {i}', flush=True)\n"
            "    i += 1\n"
            "    time.sleep(min(60, max(0, end - time.time())))\n"
            "print('done', flush=True)\n"
        )
        (self.dir / "job.yaml").write_text(
            f"name: soak-{self.name.lower()}\n"
            "accelerator: {prefer: [], accept_cpu: true}\n"
            f"code: {{kind: file, root: {self.dir}, entry: sleeper.py}}\n"
            f"budgets: {{wall_clock: {SOAK_SECONDS + 600}}}\n"
        )
        plan = mc(self.config, "--json", "job", "plan", str(self.dir / "job.yaml"), "--no-probe")
        self.job_id = envelope_line(plan.stdout)["job_id"]
        args = ["--json", "job", "apply", "--job-id", self.job_id, "--async"]
        if not self.spec["keep_alive"]:
            args.append("--no-keepalive")
        apply = mc(self.config, *args)
        self.apply_pid = int(envelope_line(apply.stdout)["pid"])
        log(f"{self.name}: planned {self.job_id}; apply --async pid {self.apply_pid}")

    def wait_for_launch(self) -> str:
        """'launched', or 'deferred' for an assignment limit; raises on any
        other failure."""
        deadline = now() + LAUNCH_TIMEOUT
        runner_log = self.job_dir / "runner.log"
        while now() < deadline:
            env = self.envelope()
            if env and env.get("failed_phase") == "provision":
                statuses = [a.get("http_status") for a in env.get("provision_attempts", [])]
                if 412 in statuses or "412" in (env.get("reason") or ""):
                    self.deferred_reason = env.get("reason")
                    return "deferred"
                raise RuntimeError(f"{self.name}: provision failed: {env.get('reason')}")
            if env and env.get("failed_phase"):
                raise RuntimeError(
                    f"{self.name}: failed in {env['failed_phase']}: {env.get('reason')}"
                )
            if runner_log.exists() and "tick 0" in runner_log.read_text(errors="replace"):
                self.endpoint = env.get("endpoint") if env else None
                # The runner started on this VM, so it was listed then.
                self.launched_at = self.last_listed = now()
                return "launched"
            if not pid_alive(self.apply_pid):
                tail = ""
                apply_log = self.job_dir / "apply.log"
                if apply_log.exists():
                    tail = apply_log.read_text(errors="replace")[-1500:]
                raise RuntimeError(
                    f"{self.name}: apply exited before launch; envelope reason "
                    f"{(env or {}).get('reason')!r}; apply.log ends: {tail!r}"
                )
            time.sleep(5)
        raise RuntimeError(f"{self.name}: no launch within {LAUNCH_TIMEOUT}s")

    def leave_unattended(self) -> None:
        if self.spec["supervised"]:
            return
        os.kill(self.apply_pid, signal.SIGKILL)
        log(f"{self.name}: apply pid {self.apply_pid} SIGKILLed")
        time.sleep(2)
        handoff = subprocess.run(
            ["pgrep", "-f", f"job status {self.job_id}"], capture_output=True, text=True
        )
        if handoff.stdout.strip():
            raise RuntimeError(
                f"{self.name}: a status poll is running ({handoff.stdout.split()}), "
                "so the job is supervised"
            )
        if self.spec["keep_alive"]:
            session = self.session() or {}
            self.daemon_pid = session.get("keep_alive_pid")
            log(f"{self.name}: keep-alive daemon pid {self.daemon_pid}")

    def observe(self, listed: set[str] | None) -> None:
        # Once collection starts the job's own supervisor releases the VM,
        # so a missing listing then is not a loss.
        if self.finished or self.endpoint is None or self.collect_deadline is not None:
            return
        elapsed = now() - self.launched_at
        if listed is not None:
            if self.endpoint in listed:
                self.last_listed = now()
            elif self.lost_at is None:
                env = self.envelope() or {}
                if self.spec["supervised"] and env.get("cleanup") in {"released", "already_absent"}:
                    return
                self.lost_at = now()
                log(f"{self.name}: {self.endpoint} NOT LISTED at +{elapsed / 60:.0f} min "
                    f"(last listed +{(self.last_listed - self.launched_at) / 60:.0f} min)")
        note = ""
        if self.spec["keep_alive"]:
            alive = pid_alive(self.daemon_pid)
            if not alive and self.daemon_died_at is None:
                self.daemon_died_at = now()
                log(f"{self.name}: keep-alive daemon {self.daemon_pid} is gone "
                    f"at +{elapsed / 60:.0f} min")
            ping = (self.session() or {}).get("last_keep_alive_ping")
            if ping:
                age = now() - datetime.fromisoformat(ping).timestamp()
                note = f" daemon={'alive' if alive else 'gone'} last_ping_age={age:.0f}s"
        if self.spec["supervised"]:
            note += f" apply={'alive' if pid_alive(self.apply_pid) else 'exited'}"
        state = "listed" if listed and self.endpoint in listed else (
            "listing failed" if listed is None else "not listed")
        log(f"{self.name}: +{elapsed / 60:.0f} min {state}{note}")

    def due(self) -> bool:
        return self.launched_at is not None and now() >= self.launched_at + SOAK_SECONDS

    def start_collect(self) -> None:
        """Ends the job without blocking the observer: B's apply finishes
        it; A and C are finished by `job status --poll`, which absorbs the
        result and releases the VM."""
        self.collect_deadline = now() + RESULT_TIMEOUT
        if self.spec["supervised"]:
            return
        cmd = ["uv", "run", "mighty-colab", "--auth=adc", "--config", str(self.config),
               "--json", "job", "status", str(self.job_id), "--poll", "--interval", "15"]
        with open(self.dir / "status-poll.out", "w", encoding="utf-8") as out:
            self.collector = subprocess.Popen(
                cmd, cwd=REPO_ROOT, stdout=out, stderr=subprocess.STDOUT
            )
        log(f"{self.name}: job status --poll started (pid {self.collector.pid})")

    def check_collect(self) -> None:
        if self.spec["supervised"]:
            running = pid_alive(self.apply_pid)
        else:
            running = self.collector.poll() is None
        if running and now() < self.collect_deadline:
            return
        if running:
            log(f"{self.name}: still running {RESULT_TIMEOUT}s after the workload ended")
            if self.collector is not None:
                self.collector.kill()
        self.outcome = self.envelope()
        self.finished = True
        env = self.outcome or {}
        log(f"{self.name}: workload={env.get('workload')} cleanup={env.get('cleanup')} "
            f"failed_phase={env.get('failed_phase')} reason={env.get('reason')}")

    def destroy(self) -> None:
        if self.job_id is None:
            return
        if self.apply_pid and pid_alive(self.apply_pid):
            os.kill(self.apply_pid, signal.SIGKILL)
        try:
            mc(self.config, "--json", "job", "destroy", self.job_id, "--wait", "0", timeout=300)
        except Exception as error:  # noqa: BLE001 - reported; the listing check decides
            log(f"{self.name}: destroy failed: {type(error).__name__}: {error}")

    def summary(self) -> str:
        if self.deferred_reason and self.launched_at is None:
            return f"{self.name} ({self.spec['label']}): not run: {self.deferred_reason}"
        if self.launched_at is None:
            return f"{self.name} ({self.spec['label']}): not launched"
        if self.lost_at is not None:
            survived = (
                f"VM NOT LISTED at +{(self.lost_at - self.launched_at) / 60:.0f} min "
                f"(last listed +{(self.last_listed - self.launched_at) / 60:.0f} min)"
            )
        else:
            survived = f"VM listed until the workload ended (+{SOAK_SECONDS / 60:.0f} min)"
        daemon = ""
        if self.spec["keep_alive"]:
            daemon = (
                f"; daemon gone at +{(self.daemon_died_at - self.launched_at) / 60:.0f} min"
                if self.daemon_died_at else "; daemon alive throughout"
            )
        env = self.outcome or {}
        return (
            f"{self.name} ({self.spec['label']}): {survived}{daemon}; "
            f"workload={env.get('workload')} cleanup={env.get('cleanup')} "
            f"reason={env.get('reason')}"
        )


def run_batch(jobs: list[Job], observer: Path) -> list[Job]:
    """Runs the jobs together; returns those deferred by an assignment limit."""
    deferred = []
    running = []
    for job in jobs:
        job.plan_and_apply()
        outcome = job.wait_for_launch()
        if outcome == "deferred":
            log(f"{job.name}: deferred by the assignment limit: {job.deferred_reason}")
            deferred.append(job)
            continue
        log(f"{job.name}: launched on {job.endpoint}")
        job.leave_unattended()
        running.append(job)
    last_observed = 0.0
    while any(not j.finished for j in running):
        if now() - last_observed >= OBSERVE_SECONDS:
            last_observed = now()
            listed = listed_endpoints(observer)
            log_compute_units(observer)
            for job in running:
                job.observe(listed)
        for job in running:
            if job.finished:
                continue
            if job.collect_deadline is not None:
                job.check_collect()
                continue
            ended = job.due() or (
                not job.spec["supervised"] and job.lost_at is not None
            ) or (job.spec["supervised"] and not pid_alive(job.apply_pid))
            if ended:
                job.start_collect()
        if any(not j.finished for j in running):
            time.sleep(min(30, OBSERVE_SECONDS))
    return [Job(j.name, j.spec) for j in deferred]


def main() -> int:
    ROOT.mkdir(parents=True, exist_ok=True)
    observer = ROOT / "observer" / "sessions.json"
    observer.parent.mkdir(parents=True, exist_ok=True)
    names = os.environ.get("SOAK_VARIANTS", "A B C").split()
    pending = [Job(n, VARIANTS[n]) for n in names]
    all_jobs: list[Job] = list(pending)

    def stop(signum, _frame):
        raise SystemExit(f"stopped by signal {signum}")

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGHUP, stop)
    log(f"soak: variants {names}, {SOAK_SECONDS}s workload, observe every "
        f"{OBSERVE_SECONDS}s, root {ROOT}")
    harness_failed = False
    try:
        while pending:
            pending = run_batch(pending, observer)
            all_jobs.extend(pending)
    except BaseException as error:
        harness_failed = True
        log(f"soak: stopped: {type(error).__name__}: {error}")
    finally:
        for job in all_jobs:
            if job.collector is not None and job.collector.poll() is None:
                job.collector.kill()
        for job in all_jobs:
            if job.job_id and (job.outcome or {}).get("cleanup") not in {"released", "already_absent"}:
                job.destroy()
        listed = listed_endpoints(observer)
        still = [j.endpoint for j in all_jobs if j.endpoint and (listed is None or j.endpoint in listed)]
        for job in all_jobs:
            if job.config.exists():
                job.config.unlink()
        log("soak: results")
        for job in all_jobs:
            if job.job_id or job.deferred_reason:
                log("  " + job.summary())
        if still:
            log(f"soak: STILL LISTED (or the listing failed): {still}")
            harness_failed = True
        else:
            log("soak: no soak VM is listed")
    return 1 if harness_failed else 0


if __name__ == "__main__":
    sys.exit(main())
