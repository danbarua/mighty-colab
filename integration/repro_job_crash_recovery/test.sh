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
JOB_ID=""
SESSION_NAME=""
APPLY_PID=""

mc() {
    uv run mighty-colab --auth=adc --config "$SESSION_FILE" "$@"
}

cleanup() {
    if [ -n "$APPLY_PID" ]; then
        kill "$APPLY_PID" >/dev/null 2>&1 || true
        wait "$APPLY_PID" >/dev/null 2>&1 || true
    fi
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

print("crash-recovery probe started")
time.sleep(45)
print("crash-recovery probe done")
PY

RUN_NAME="crash-recovery-$(date -u +%Y%m%dT%H%M%SZ)-$$"
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
JOB_DIR="$TMP_DIR/jobs/$JOB_ID"

mc --json job apply --job-id "$JOB_ID" >/dev/null 2>&1 &
APPLY_PID=$!

ENVELOPE=""
for _ in $(seq 1 90); do
    if [ -f "$JOB_DIR/envelope.json" ]; then
        PHASE=$(JOB_DIR="$JOB_DIR" uv run python - <<'PY'
import json
import os
from pathlib import Path

env = json.loads(Path(os.environ["JOB_DIR"], "envelope.json").read_text())
print(env.get("phase") or "")
PY
)
        if [ "$PHASE" = "run" ]; then
            ENVELOPE=1
            break
        fi
    fi
    sleep 1
done

if [ -z "$ENVELOPE" ]; then
    echo "apply never reached phase=run" >&2
    exit 1
fi

SUP_PID=$(JOB_DIR="$JOB_DIR" uv run python - <<'PY'
import json
import os
from pathlib import Path

ident = json.loads(Path(os.environ["JOB_DIR"], "supervisor.json").read_text())
print(ident["pid"])
PY
)
kill "$SUP_PID" 2>/dev/null || true
pkill -TERM -P "$APPLY_PID" 2>/dev/null || true
kill "$APPLY_PID" 2>/dev/null || true
set +e
wait "$APPLY_PID"
set -e
APPLY_PID=""
for _ in $(seq 1 20); do
    if ! kill -0 "$SUP_PID" 2>/dev/null; then
        break
    fi
    sleep 0.2
done
if kill -0 "$SUP_PID" 2>/dev/null; then
    kill -KILL "$SUP_PID" 2>/dev/null || true
fi

STATUS_JSON=$(mc --json job status "$JOB_ID" --poll --interval 5)
STATUS_JSON="$STATUS_JSON" uv run python - <<'PY'
import json
import os

payload = json.loads(os.environ["STATUS_JSON"])
job = payload["job"]
assert job["workload"] == "succeeded", payload
assert job["exit_code"] == 0, payload
assert job["cleanup"] in {"released", "already_absent"}, payload
assert job["supervisor"] == "finished", payload
assert payload.get("done") is True, payload
PY
JOB_ID=""

if ! mc sessions | grep -q "No active sessions"; then
    mc sessions
    echo "crash recovery left an active session" >&2
    exit 1
fi

echo "[SUCCESS] killed apply during run; status --poll recovered a terminal released job"
