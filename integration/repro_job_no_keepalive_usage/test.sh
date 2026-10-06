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

# `usage --json` and `job apply --no-keepalive`, live. One CPU job (~3
# minutes): `usage --json` reports a balance; a 60 s job applied with
# `--async --no-keepalive` runs with no keep-alive daemon, `sessions
# --json` reports its keep-alive as disabled, and its envelope records
# keep_alive_disabled and the compute units at provision and at release.

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)
TMP_DIR=$(cd "$(mktemp -d)" && pwd -P)
SESSION_FILE="$TMP_DIR/sessions.json"
JOB_ID=""

mc() {
    uv run mighty-colab --auth=adc --config "$SESSION_FILE" "$@"
}

last_json() {
    uv run python -c 'import json, sys; print(json.dumps(json.loads(sys.stdin.read().strip().splitlines()[-1])))'
}

cleanup() {
    if [ -n "$JOB_ID" ]; then
        mc --json job destroy "$JOB_ID" --wait 0 >/dev/null 2>&1 || true
    fi
    rm -rf "$TMP_DIR"
}
trap cleanup EXIT

cd "$REPO_ROOT"
USAGE=$(mc --json usage 2>/dev/null | last_json)
USAGE="$USAGE" uv run python -c '
import json, os
u = json.loads(os.environ["USAGE"])
assert u["command"] == "usage" and u["status"] == "ok", u
assert isinstance(u["current_balance"], float) and u["assignments_count"] >= 0, u
balance, rate, count = u["current_balance"], u["consumption_rate_hourly"], u["assignments_count"]
print(f"usage: balance {balance:.2f}, rate {rate}/h, {count} assignment(s)")
'

cat >"$TMP_DIR/sleeper.py" <<'PY'
import time
for i in range(12):
    print(f"tick {i}", flush=True)
    time.sleep(5)
PY
cat >"$TMP_DIR/job.yaml" <<YAML
name: no-keepalive
accelerator: {prefer: [], accept_cpu: true}
code: {kind: file, root: $TMP_DIR, entry: sleeper.py}
budgets: {wall_clock: 300}
YAML
JOB_ID=$(mc --json job plan "$TMP_DIR/job.yaml" --no-probe | last_json | uv run python -c \
    "import json, sys; print(json.load(sys.stdin)['job_id'])")
APPLY_PID=$(mc --json job apply --job-id "$JOB_ID" --async --no-keepalive | last_json | uv run python -c \
    "import json, sys; print(json.load(sys.stdin)['pid'])")
echo "$(date +%T) apply --async --no-keepalive pid $APPLY_PID for $JOB_ID"

for _ in $(seq 1 150); do
    [ -f "$TMP_DIR/jobs/$JOB_ID/runner.log" ] && grep -q "tick 0" "$TMP_DIR/jobs/$JOB_ID/runner.log" && break
    sleep 2
done
grep -q "tick 0" "$TMP_DIR/jobs/$JOB_ID/runner.log" || { echo "the run never started" >&2; exit 1; }
ENDPOINT=$(JOB_DIR="$TMP_DIR/jobs/$JOB_ID" uv run python -c \
    "import json, os; print(json.load(open(os.path.join(os.environ['JOB_DIR'], 'envelope.json')))['endpoint'])")
if pgrep -f "keep-alive $ENDPOINT" >/dev/null; then
    echo "a keep-alive daemon is running for $ENDPOINT" >&2
    exit 1
fi
SESSIONS=$(mc --json sessions 2>/dev/null | last_json)
SESSIONS="$SESSIONS" ENDPOINT="$ENDPOINT" uv run python -c '
import json, os
rows = [s for s in json.loads(os.environ["SESSIONS"])["sessions"] if s["endpoint"] == os.environ["ENDPOINT"]]
assert rows and rows[0]["keep_alive_health"] == "disabled" and "keep_alive_pid" not in rows[0], rows
print("sessions: keep_alive_health", rows[0]["keep_alive_health"])
'
echo "$(date +%T) running with no keep-alive daemon"

for _ in $(seq 1 120); do
    kill -0 "$APPLY_PID" 2>/dev/null || break
    sleep 5
done
if kill -0 "$APPLY_PID" 2>/dev/null; then
    echo "apply still running after 10 minutes" >&2
    exit 1
fi

JOB_DIR="$TMP_DIR/jobs/$JOB_ID" uv run python - <<'PY'
import json, os
env = json.load(open(os.path.join(os.environ["JOB_DIR"], "envelope.json")))
assert env["workload"] == "succeeded" and env["cleanup"] == "released", env
assert env["keep_alive_disabled"] is True, env
provision, release = env["compute_units_at_provision"], env["compute_units_at_release"]
assert provision["assignments"] >= 1 and release["balance"] <= provision["balance"], env
assert not any(h.startswith("compute units not read") for h in env["hints"]), env["hints"]
print("envelope: keep_alive_disabled", env["keep_alive_disabled"])
print("compute units at provision:", provision)
print("compute units at release:  ", release)
PY
if mc --json sessions 2>/dev/null | last_json | ENDPOINT="$ENDPOINT" uv run python -c \
    'import json, os, sys; raise SystemExit(0 if os.environ["ENDPOINT"] in {s["endpoint"] for s in json.load(sys.stdin)["sessions"]} else 1)'; then
    echo "$ENDPOINT is still listed" >&2
    exit 1
fi
JOB_ID=""
echo "[SUCCESS] usage read the balance; the --no-keepalive job ran without a daemon and recorded its compute units"
