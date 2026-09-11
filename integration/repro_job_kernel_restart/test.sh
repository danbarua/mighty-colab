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
APPLY_LOG="$TMP_DIR/apply.log"
BEFORE="$TMP_DIR/before.json"
AFTER="$TMP_DIR/after.json"
FINAL="$TMP_DIR/final.json"
JOB_ID=""
SESSION_NAME=""
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

RUN_NAME="kernel-restart-live-$(date -u +%Y%m%dT%H%M%SZ)-$$"
cat >"$SPEC_FILE" <<EOF
name: $RUN_NAME
accelerator:
  prefer: []
  accept_cpu: true
code:
  kind: file
  root: $SCRIPT_DIR
  entry: probe.py
  args: ["--seconds", "90"]
budgets:
  wall_clock: 180
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

KERNEL_ID=""
for _ in $(seq 1 90); do
    if [ -f "$SESSION_FILE" ]; then
        KERNEL_ID=$(SESSION_FILE="$SESSION_FILE" SESSION_NAME="$SESSION_NAME" uv run python - <<'PY'
import json
import os
from pathlib import Path

records = json.loads(Path(os.environ["SESSION_FILE"]).read_text())
print((records.get(os.environ["SESSION_NAME"]) or {}).get("kernel_id") or "")
PY
)
    fi
    if [ -n "$KERNEL_ID" ] && mc download -s "$SESSION_NAME" \
        /content/out/restart_probe.json "$BEFORE" >/dev/null 2>&1; then
        break
    fi
    if ! kill -0 "$APPLY_PID" 2>/dev/null; then
        cat "$APPLY_LOG"
        echo "job apply exited before the restart baseline was readable" >&2
        exit 1
    fi
    sleep 1
done

if [ -z "$KERNEL_ID" ] || [ ! -f "$BEFORE" ]; then
    cat "$APPLY_LOG"
    echo "launch kernel identity or probe baseline was not persisted" >&2
    exit 1
fi

echo "[*] Restarting launch kernel $KERNEL_ID"
mc restart-kernel -s "$SESSION_NAME"
sleep 6
mc download -s "$SESSION_NAME" /content/out/restart_probe.json "$AFTER" >/dev/null

BEFORE="$BEFORE" AFTER="$AFTER" uv run python - <<'PY'
import json
import os
from pathlib import Path

before = json.loads(Path(os.environ["BEFORE"]).read_text())
after = json.loads(Path(os.environ["AFTER"]).read_text())
identity = ("pid", "ppid", "sid", "started_at")
assert all(before[key] == after[key] for key in identity), (before, after)
assert after["tick"] > before["tick"], (before, after)
assert not after["completed"], after
PY

set +e
wait "$APPLY_PID"
APPLY_RC=$?
set -e
APPLY_PID=""
if [ "$APPLY_RC" -ne 0 ]; then
    cat "$APPLY_LOG"
    exit "$APPLY_RC"
fi

mc download -s "$SESSION_NAME" /content/out/restart_probe.json "$FINAL" >/dev/null
BEFORE="$BEFORE" AFTER="$AFTER" FINAL="$FINAL" APPLY_LOG="$APPLY_LOG" \
    uv run python - <<'PY'
import json
import os
from pathlib import Path

before = json.loads(Path(os.environ["BEFORE"]).read_text())
after = json.loads(Path(os.environ["AFTER"]).read_text())
final = json.loads(Path(os.environ["FINAL"]).read_text())
identity = ("pid", "ppid", "sid", "started_at")
assert all(before[key] == after[key] == final[key] for key in identity)
assert before["tick"] < after["tick"] < final["tick"]
assert final["completed"] is True

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
assert envelope["done"] is True
assert envelope["ok"] is True
assert envelope["job"]["workload"] == "succeeded"
assert envelope["job"]["exit_code"] == 0
print(json.dumps({
    "kernel_restart": "survived",
    "same_process_identity": True,
    "workload": envelope["job"]["workload"],
    "exit_code": envelope["job"]["exit_code"],
}, sort_keys=True))
PY

DESTROY_JSON=$(mc --json job destroy "$JOB_ID")
ENDPOINT=$(DESTROY_JSON="$DESTROY_JSON" uv run python - <<'PY'
import json
import os

result = json.loads(os.environ["DESTROY_JSON"])
assert result["job"]["cleanup"] == "released", result
print(result["job"]["endpoint"])
PY
)
SESSION_LIST=$(mc sessions)
if echo "$SESSION_LIST" | grep -q "$ENDPOINT"; then
    echo "$SESSION_LIST"
    echo "destroyed endpoint still appears in active sessions" >&2
    exit 1
fi
JOB_ID=""

echo "[SUCCESS] Detached job survived its launch kernel restart"
