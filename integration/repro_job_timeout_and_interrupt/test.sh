#!/bin/bash
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

# A local supervisor that stops early must not leave a VM billing with
# nobody responsible for it, and a stopped job keeps its verdict. Four CPU jobs:
#   1. `apply --timeout` passes while the workload still runs: apply
#      cancels the runner, keeps its result, and releases the VM;
#   2. SIGINT (Ctrl-C) during install, before the runner is launched:
#      apply releases the VM;
#   3. SIGTERM after launch, as an agent harness sends when a tool call runs
#      too long: the run continues, and a detached `job status --poll` that
#      apply starts on its way out collects the result and releases the VM;
#   4. wall_clock passes: the watchdog kills the workload and escalates, but
#      never signals the runner, which writes the verdict.
# Signals go to the apply process itself (the pid in supervisor.json), not
# to the uv wrapper.

set -euo pipefail
# A non-interactive shell starts background jobs with SIGINT ignored, and
# Python then never installs its KeyboardInterrupt handler. Job control
# gives each background job its own process group with SIGINT at default,
# as a terminal's Ctrl-C would see it.
set -m

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)
TMP_DIR=$(cd "$(mktemp -d)" && pwd -P)
SESSION_FILE="$TMP_DIR/sessions.json"
JOB_ID=""
APPLY_PID=""

mc() {
    uv run mighty-colab --auth=adc --config "$SESSION_FILE" "$@"
}

# Assignment checks parse `--json sessions` instead of matching its text.
endpoint_listed() {
    SESSIONS="$(mc --json sessions 2>/dev/null)" ENDPOINT="$1" uv run python -c \
        'import json, os; listed = {s.get("endpoint") for s in json.loads(os.environ["SESSIONS"])["sessions"]}; raise SystemExit(0 if os.environ["ENDPOINT"] in listed else 1)'
}

no_sessions() {
    SESSIONS="$(mc --json sessions 2>/dev/null)" uv run python -c \
        'import json, os; raise SystemExit(0 if not json.loads(os.environ["SESSIONS"])["sessions"] else 1)'
}

cleanup() {
    if [ -n "$APPLY_PID" ] && kill -0 "$APPLY_PID" 2>/dev/null; then
        kill "$APPLY_PID" 2>/dev/null || true
        wait "$APPLY_PID" 2>/dev/null || true
    fi
    if [ -n "$JOB_ID" ]; then
        mc --json job destroy "$JOB_ID" --wait 0 >/dev/null 2>&1 || true
    fi
    rm -rf "$TMP_DIR"
}
trap cleanup EXIT

cd "$REPO_ROOT"
cat >"$TMP_DIR/sleeper.py" <<'PY'
import sys
import time

seconds = int(sys.argv[1])
for i in range(seconds // 5):
    print(f"tick {i}", flush=True)
    time.sleep(5)
print("sleeper done")
PY

# plan NAME SECONDS [DEP]: writes a spec for a CPU job that sleeps SECONDS,
# optionally with one dependency, and sets $JOB_ID.
plan() {
    local name="$1" seconds="$2" dep="${3:-}"
    {
        echo "name: $name-$(date -u +%Y%m%dT%H%M%SZ)-$$"
        echo "accelerator: {prefer: [], accept_cpu: true}"
        echo "code: {kind: file, root: $TMP_DIR, entry: sleeper.py, args: [\"$seconds\"]}"
        [ -n "$dep" ] && echo "deps: [\"$dep\"]"
        echo "budgets: {wall_clock: ${WALL_CLOCK:-1200}}"
    } >"$TMP_DIR/$name.yaml"
    JOB_ID=$(mc --json job plan "$TMP_DIR/$name.yaml" --no-probe | uv run python -c \
        "import json, sys; print(json.load(sys.stdin)['job_id'])")
}

job_field() {
    JOB_DIR="$TMP_DIR/jobs/$JOB_ID" FIELD="$1" uv run python -c \
        "import json, os; print(json.load(open(os.path.join(os.environ['JOB_DIR'], 'envelope.json'))).get(os.environ['FIELD']) or '')"
}

wait_for() {
    local what="$1" check="$2"
    for _ in $(seq 1 150); do
        if eval "$check"; then return 0; fi
        if ! kill -0 "$APPLY_PID" 2>/dev/null; then
            echo "apply exited while waiting for $what" >&2
            return 1
        fi
        sleep 2
    done
    echo "timed out waiting for $what" >&2
    return 1
}

interrupt_apply() {
    local signal_name="$1" pid
    pid=$(JOB_DIR="$TMP_DIR/jobs/$JOB_ID" uv run python -c \
        "import json, os; print(json.load(open(os.path.join(os.environ['JOB_DIR'], 'supervisor.json')))['pid'])")
    kill -"$signal_name" "$pid"
    # A stuck apply is a failure, not something to wait out.
    for _ in $(seq 1 30); do
        kill -0 "$APPLY_PID" 2>/dev/null || break
        sleep 2
    done
    if kill -0 "$APPLY_PID" 2>/dev/null; then
        echo "apply did not exit within 60s of SIG$signal_name" >&2
        kill -9 "$pid" 2>/dev/null || true
        exit 1
    fi
    set +e
    wait "$APPLY_PID"
    set -e
    APPLY_PID=""
}

assert_released() {
    local endpoint
    endpoint=$(job_field endpoint)
    if endpoint_listed "$endpoint"; then
        mc sessions
        echo "$1 left $endpoint listed" >&2
        exit 1
    fi
    JOB_ID=""
}

check() {
    JOB_DIR="$TMP_DIR/jobs/$JOB_ID" uv run python -c "
import json, os
env = json.load(open(os.path.join(os.environ['JOB_DIR'], 'envelope.json')))
$1"
}

# 1. --timeout passes during the run.
plan timeout 600
mc --json job apply --job-id "$JOB_ID" --timeout 150 >"$TMP_DIR/timeout.json" || true
check '
# The runner survives the cancel (the watchdog never signals it) and
# writes its own verdict.
assert env["workload"] == "cancelled", env
assert env["failed_phase"] == "run", env
assert env["reason"].endswith(
    "--timeout of 150s passed before a verdict; cancelled by job apply "
    "--timeout; the workload was stopped by SIGTERM (15)"
), env
assert env["cleanup"] == "released", env
print("timeout:", env["workload"], "|", env["reason"][:200])
'
assert_released timeout

# 2. SIGINT during install, before launch.
plan install-interrupt 60 "torch==2.6.0"
mc --json job apply --job-id "$JOB_ID" >"$TMP_DIR/install.json" 2>/dev/null &
APPLY_PID=$!
wait_for "install" '[ -f "$TMP_DIR/jobs/$JOB_ID/envelope.json" ] && [ "$(job_field phase)" = install ]'
sleep 10
interrupt_apply INT
check '
assert env["workload"] == "cancelled", env
assert "Ctrl-C (SIGINT)" in env["reason"], env
assert env["cleanup"] == "released", env
assert "before the runner was launched" in env["reason"], env
assert env["failed_phase"] is None, env
print("install interrupt:", env["reason"])
'
assert_released "install interrupt"

# 3. SIGTERM after launch: a detached `job status --poll` takes over and
#    releases the VM when the job ends, with nobody coming back.
plan run-interrupt 60
mc --json job apply --job-id "$JOB_ID" >"$TMP_DIR/run.json" 2>/dev/null &
APPLY_PID=$!
wait_for "the run" '[ -f "$TMP_DIR/jobs/$JOB_ID/runner.log" ] && grep -q "tick 1" "$TMP_DIR/jobs/$JOB_ID/runner.log"'
interrupt_apply TERM
check '
assert env["cleanup"] == "pending", env
assert "SIGTERM" in env["reason"], env
assert "detached `job status --poll`" in env["reason"], env
print("run interrupt:", env["reason"])
'
ENDPOINT=$(job_field endpoint)
endpoint_listed "$ENDPOINT" || { echo "the handed-off run's VM should still be assigned" >&2; exit 1; }
for _ in $(seq 1 150); do
    [ "$(job_field cleanup)" = released ] && break
    sleep 2
done
check '
assert env["cleanup"] == "released", f"the detached poll did not release the VM within 300s: {env}"
assert env["workload"] == "succeeded", env
assert os.path.exists(os.path.join(os.environ["JOB_DIR"], "status-poll.log")), "no status-poll.log"
print("detached poll:", env["workload"], env["cleanup"])
'
assert_released "detached poll"

# 4. wall_clock passes: the watchdog terminates the workload and escalates
#    to SIGKILL, but never touches the runner, which writes the verdict.
WALL_CLOCK=60 plan wall-clock 600
mc --json job apply --job-id "$JOB_ID" >"$TMP_DIR/wall-clock.json" || true
check '
assert env["workload"] == "cancelled", f"no runner verdict after the wall_clock kill: {env}"
assert env["reason"] == "wall_clock budget of 60s reached; the workload was stopped by SIGTERM (15)", env
assert env["retry_class"] == "fix_code", env
assert env["failed_phase"] == "run", env
assert env["cleanup"] == "released", env
print("wall_clock:", env["workload"], "signal", env["signal"], "|", env["reason"])
'
assert_released wall_clock

echo "[SUCCESS] --timeout cancelled and released; Ctrl-C before launch released; SIGTERM after launch handed off and the detached poll released"
