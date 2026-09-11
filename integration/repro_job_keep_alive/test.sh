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

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)
TMP_DIR=$(mktemp -d)
SESSION_FILE="$TMP_DIR/sessions.json"
SPEC_FILE="$TMP_DIR/spec.yaml"
TRAIN_FILE="$TMP_DIR/train.py"
APPLY_LOG="$TMP_DIR/apply.log"
JOB_ID=""
SESSION_NAME=""
KEEP_PID=""

mc() {
    uv run mighty-colab --auth=adc --config "$SESSION_FILE" "$@"
}

cleanup() {
    if [ -n "$JOB_ID" ]; then
        mc job destroy "$JOB_ID" >/dev/null 2>&1 || true
    elif [ -n "$SESSION_NAME" ]; then
        mc stop -s "$SESSION_NAME" >/dev/null 2>&1 || true
    fi
    rm -rf "$TMP_DIR"
}
trap cleanup EXIT

cat >"$TRAIN_FILE" <<'PY'
import time

print("job keep-alive probe started")
time.sleep(8)
print("job keep-alive probe done")
PY

RUN_NAME="job-keep-alive-$(date -u +%Y%m%dT%H%M%SZ)-$$"
cat >"$SPEC_FILE" <<EOF
name: $RUN_NAME
accelerator:
  prefer: []
  accept_cpu: true
code:
  kind: file
  root: $TMP_DIR
  entry: train.py
budgets:
  wall_clock: 180
EOF

cd "$REPO_ROOT"
PLAN_JSON=$(mc --json job plan "$SPEC_FILE")
JOB_ID=$(PLAN_JSON="$PLAN_JSON" uv run python - <<'PY'
import json
import os

print(json.loads(os.environ["PLAN_JSON"])["job_id"])
PY
)
SESSION_NAME="job-$JOB_ID"

mc --json job apply --job-id "$JOB_ID" --leave-up >"$APPLY_LOG" 2>&1

SESSION_FILE="$SESSION_FILE" SESSION_NAME="$SESSION_NAME" uv run python - <<'PY' >"$TMP_DIR/alive.txt"
import json
import os
from pathlib import Path

sessions = json.loads(Path(os.environ["SESSION_FILE"]).read_text())
session = sessions[os.environ["SESSION_NAME"]]
pid = session.get("keep_alive_pid")
assert pid, session
assert session.get("last_keep_alive_ping"), session
print(pid)
print(session["last_keep_alive_ping"])
PY

KEEP_PID=$(sed -n '1p' "$TMP_DIR/alive.txt")
PING_BEFORE=$(sed -n '2p' "$TMP_DIR/alive.txt")
if ! ps -p "$KEEP_PID" >/dev/null; then
    echo "keep-alive pid $KEEP_PID is not running after leave-up" >&2
    exit 1
fi
ps -fp "$KEEP_PID" | grep -q "keep-alive"

echo "[*] keep-alive pid $KEEP_PID; waiting for a daemon tick past pre-flight"
sleep 70

SESSION_FILE="$SESSION_FILE" SESSION_NAME="$SESSION_NAME" PING_BEFORE="$PING_BEFORE" KEEP_PID="$KEEP_PID" \
    uv run python - <<'PY'
import json
import os
from pathlib import Path

sessions = json.loads(Path(os.environ["SESSION_FILE"]).read_text())
session = sessions[os.environ["SESSION_NAME"]]
assert session.get("keep_alive_pid") == int(os.environ["KEEP_PID"]), session
ping = session.get("last_keep_alive_ping")
assert ping, session
assert ping >= os.environ["PING_BEFORE"], (os.environ["PING_BEFORE"], ping)
print(ping)
PY

if ! ps -p "$KEEP_PID" >/dev/null; then
    echo "keep-alive pid $KEEP_PID died during the idle wait" >&2
    exit 1
fi

DESTROY_JSON=$(mc --json job destroy "$JOB_ID")
DESTROY_JSON="$DESTROY_JSON" KEEP_PID="$KEEP_PID" uv run python - <<'PY'
import json
import os
import time

payload = json.loads(os.environ["DESTROY_JSON"])
assert payload["job"]["cleanup"] in {"released", "already_absent"}, payload
pid = int(os.environ["KEEP_PID"])
deadline = time.time() + 5
while time.time() < deadline:
    try:
        os.kill(pid, 0)
    except OSError:
        break
    time.sleep(0.1)
else:
    raise SystemExit(f"keep-alive pid {pid} still running after destroy")
PY
JOB_ID=""
KEEP_PID=""

if ! mc sessions | grep -q "No active sessions"; then
    mc sessions
    echo "job keep-alive integration left an active session" >&2
    exit 1
fi

echo "[SUCCESS] job apply owned keep-alive through idle leave-up and destroy"
