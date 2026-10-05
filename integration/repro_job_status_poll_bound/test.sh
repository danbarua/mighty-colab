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

# `job status --poll` on an orphaned job cannot wait forever. One CPU job
# (~20 minutes): `apply --async` is SIGKILLed after launch, then the
# runner, the consumer and the watchdog are SIGKILLed on the VM, so no
# result.json is ever written and watchdog.json stops changing while still
# saying runner_alive: true. `job status --poll` must report the watchdog
# as stalled after 5 minutes, cancel at launch + wall_clock + 600 s, wait
# for a result that cannot come, and release the VM.

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)
TMP_DIR=$(cd "$(mktemp -d)" && pwd -P)
SESSION_FILE="$TMP_DIR/sessions.json"
JOB_ID=""
POLL_PID=""

mc() {
    uv run mighty-colab --auth=adc --config "$SESSION_FILE" "$@"
}

endpoint_listed() {
    SESSIONS="$(mc --json sessions 2>/dev/null)" ENDPOINT="$1" uv run python -c \
        'import json, os; listed = {s.get("endpoint") for s in json.loads(os.environ["SESSIONS"])["sessions"]}; raise SystemExit(0 if os.environ["ENDPOINT"] in listed else 1)'
}

cleanup() {
    if [ -n "$POLL_PID" ] && kill -0 "$POLL_PID" 2>/dev/null; then
        kill "$POLL_PID" 2>/dev/null || true
    fi
    if [ -n "$JOB_ID" ]; then
        mc --json job destroy "$JOB_ID" --wait 0 >/dev/null 2>&1 || true
    fi
    rm -rf "$TMP_DIR"
}
trap cleanup EXIT

cd "$REPO_ROOT"
cat >"$TMP_DIR/sleeper.py" <<'PY'
import time
for i in range(180):
    print(f"tick {i}", flush=True)
    time.sleep(5)
PY
cat >"$TMP_DIR/job.yaml" <<YAML
name: poll-bound-$(date -u +%Y%m%dT%H%M%SZ)-$$
accelerator: {prefer: [], accept_cpu: true}
code: {kind: file, root: $TMP_DIR, entry: sleeper.py}
budgets: {wall_clock: 180}
YAML
JOB_ID=$(mc --json job plan "$TMP_DIR/job.yaml" --no-probe | uv run python -c \
    "import json, sys; print(json.load(sys.stdin)['job_id'])")
APPLY_PID=$(mc --json job apply --job-id "$JOB_ID" --async | uv run python -c \
    "import json, sys; print(json.load(sys.stdin)['pid'])")
echo "$(date +%T) apply --async pid $APPLY_PID for $JOB_ID"

for _ in $(seq 1 120); do
    [ -f "$TMP_DIR/jobs/$JOB_ID/runner.log" ] && grep -q "tick 0" "$TMP_DIR/jobs/$JOB_ID/runner.log" && break
    sleep 2
done
grep -q "tick 0" "$TMP_DIR/jobs/$JOB_ID/runner.log" || { echo "the run never started" >&2; exit 1; }
kill -9 "$APPLY_PID"
echo "$(date +%T) apply SIGKILLed"

cat >"$TMP_DIR/kill_runtime.py" <<'PY'
import subprocess
out = subprocess.run(["pkill", "-9", "-f", "mighty_runtime"], capture_output=True, text=True)
left = subprocess.run(["pgrep", "-f", "mighty_runtime"], capture_output=True, text=True)
print("PKILL", out.returncode, "LEFT", left.stdout.split())
PY
mc exec -s "job-$JOB_ID" -f "$TMP_DIR/kill_runtime.py" 2>&1 | grep -E "PKILL" || { echo "could not kill the runtime on the VM" >&2; exit 1; }
echo "$(date +%T) runner, consumer and watchdog SIGKILLed on the VM"

mc --json job status "$JOB_ID" --poll --interval 15 >"$TMP_DIR/status.json" 2>"$TMP_DIR/status.err" &
POLL_PID=$!
# launch + 180 + 600 s, then up to 300 s of waiting for a result: bounded at 25 min.
for _ in $(seq 1 300); do
    kill -0 "$POLL_PID" 2>/dev/null || break
    sleep 5
done
if kill -0 "$POLL_PID" 2>/dev/null; then
    echo "status --poll still running after 25 minutes" >&2
    exit 1
fi
POLL_PID=""
echo "$(date +%T) status --poll ended"

JOB_DIR="$TMP_DIR/jobs/$JOB_ID" uv run python - <<'PY'
import json, os
env = json.load(open(os.path.join(os.environ["JOB_DIR"], "envelope.json")))
assert env["cleanup"] == "released", env
assert env["workload"] == "unknown", env
assert env["failed_phase"] == "run" and env["retry_class"] == "retry_same", env
assert env["reason"].startswith(
    "no verdict within 780s of launch (wall_clock 180s + 600s); the job's supervisor "
    "is gone, so job status --poll cancelled it"
), env
stalled = [h for h in env["hints"] if h.startswith("watchdog stalled: watchdog.json has not changed for")]
assert stalled, env["hints"]
print("reason:", env["reason"])
print("stalled:", stalled[0])
PY
ENDPOINT=$(JOB_DIR="$TMP_DIR/jobs/$JOB_ID" uv run python -c \
    "import json, os; print(json.load(open(os.path.join(os.environ['JOB_DIR'], 'envelope.json')))['endpoint'] or '')")
if [ -n "$ENDPOINT" ] && endpoint_listed "$ENDPOINT"; then
    echo "$ENDPOINT is still listed" >&2
    exit 1
fi
JOB_ID=""
echo "[SUCCESS] status --poll reported the stalled watchdog, cancelled at the deadline and released the VM"
