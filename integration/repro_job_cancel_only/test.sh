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
PROBE_DIR="$REPO_ROOT/integration/repro_job_kernel_restart"
TMP_DIR=$(mktemp -d)
SESSION_FILE="$TMP_DIR/sessions.json"
SPEC_FILE="$TMP_DIR/spec.yaml"
APPLY_LOG="$TMP_DIR/apply.log"
PROGRESS="$TMP_DIR/progress.json"
JOB_ID=""
SESSION_NAME=""
ENDPOINT=""
APPLY_PID=""

mc() {
    uv run mighty-colab --auth=adc --config "$SESSION_FILE" "$@"
}

cleanup() {
    if [ -n "$APPLY_PID" ] && kill -0 "$APPLY_PID" 2>/dev/null; then
        kill "$APPLY_PID" 2>/dev/null || true
        wait "$APPLY_PID" 2>/dev/null || true
    fi
    if [ -n "$JOB_ID" ]; then
        mc --json job destroy "$JOB_ID" >/dev/null 2>&1 || true
    fi
    rm -rf "$TMP_DIR"
}
trap cleanup EXIT

RUN_NAME="cancel-only-live-$(date -u +%Y%m%dT%H%M%SZ)-$$"
cat >"$SPEC_FILE" <<EOF
name: $RUN_NAME
accelerator:
  prefer: []
  accept_cpu: true
code:
  kind: file
  root: $PROBE_DIR
  entry: probe.py
  args: ["--seconds", "300"]
budgets:
  wall_clock: 600
on_offload_fail: destroy
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

mc --json job apply --job-id "$JOB_ID" --leave-up >"$APPLY_LOG" 2>&1 &
APPLY_PID=$!

for _ in $(seq 1 90); do
    if [ -f "$SESSION_FILE" ]; then
        ENDPOINT=$(SESSION_FILE="$SESSION_FILE" SESSION_NAME="$SESSION_NAME" uv run python - <<'PY'
import json
import os
from pathlib import Path

records = json.loads(Path(os.environ["SESSION_FILE"]).read_text())
print((records.get(os.environ["SESSION_NAME"]) or {}).get("endpoint") or "")
PY
)
    fi
    if [ -n "$ENDPOINT" ] && mc download -s "$SESSION_NAME" \
        /content/out/restart_probe.json "$PROGRESS" >/dev/null 2>&1; then
        break
    fi
    if ! kill -0 "$APPLY_PID" 2>/dev/null; then
        cat "$APPLY_LOG"
        echo "job apply exited before the running workload was observable" >&2
        exit 1
    fi
    sleep 1
done

if [ -z "$ENDPOINT" ] || [ ! -f "$PROGRESS" ]; then
    cat "$APPLY_LOG"
    echo "running workload or endpoint was not observable" >&2
    exit 1
fi

CANCEL_JSON=$(mc --json job destroy "$JOB_ID" --cancel-only)
CANCEL_JSON="$CANCEL_JSON" uv run python - <<'PY'
import json
import os

result = json.loads(os.environ["CANCEL_JSON"])
assert result["status"] == "ok", result
assert result["job"]["reason"] == "cancel intent written; VM left running", result
assert result["job"]["cleanup"] != "released", result
PY

SESSION_LIST=$(mc sessions)
if ! echo "$SESSION_LIST" | grep -q "$ENDPOINT"; then
    echo "$SESSION_LIST"
    echo "cancel-only unexpectedly released the assignment" >&2
    exit 1
fi

set +e
wait "$APPLY_PID"
APPLY_RC=$?
set -e
APPLY_PID=""
if [ "$APPLY_RC" -eq 0 ]; then
    cat "$APPLY_LOG"
    echo "cancelled apply unexpectedly exited successfully" >&2
    exit 1
fi

APPLY_LOG="$APPLY_LOG" uv run python - <<'PY'
import json
import os
from pathlib import Path

envelopes = []
for line in Path(os.environ["APPLY_LOG"]).read_text().splitlines():
    try:
        candidate = json.loads(line)
    except json.JSONDecodeError:
        continue
    if candidate.get("command") == "job apply":
        envelopes.append(candidate)
assert len(envelopes) == 1, envelopes
envelope = envelopes[0]
assert envelope["done"] is True, envelope
assert envelope["ok"] is False, envelope
assert envelope["job"]["workload"] == "cancelled", envelope
assert envelope["job"]["cleanup"] == "left_up", envelope
PY

DESTROY_JSON=$(mc --json job destroy "$JOB_ID")
DESTROY_JSON="$DESTROY_JSON" uv run python - <<'PY'
import json
import os

result = json.loads(os.environ["DESTROY_JSON"])
assert result["job"]["workload"] == "cancelled", result
assert result["job"]["cleanup"] == "released", result
PY

SESSION_LIST=$(mc sessions)
if echo "$SESSION_LIST" | grep -q "$ENDPOINT"; then
    echo "$SESSION_LIST"
    echo "destroyed endpoint still appears in active sessions" >&2
    exit 1
fi
JOB_ID=""

echo "[SUCCESS] cancel-only stopped the workload and preserved the assignment"
