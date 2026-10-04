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

# A job that runs past the runtime-proxy token's ~60-minute expiry. An
# expired token answers 404 for files that exist, and the transport
# refreshes it on a 404 at most once a minute, so a poll near the boundary
# can see NOT_FOUND for watchdog.json and launch.json. `job apply` must keep
# polling through it: the workload succeeds, nothing is declared never
# started or dead, and the VM is released only after the workload ends.
#
# Slow: about 75 minutes and 75 CPU-minutes by default.
# REPRO_TOKEN_BOUNDARY_SECONDS shortens the workload for a smoke run, which
# does not cross the boundary.

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)
TMP_DIR=$(cd "$(mktemp -d)" && pwd -P)
SESSION_FILE="$TMP_DIR/sessions.json"
SPEC_FILE="$TMP_DIR/spec.yaml"
APPLY_JSON="$TMP_DIR/apply.json"
RUN_SECONDS="${REPRO_TOKEN_BOUNDARY_SECONDS:-4200}"
WALL_CLOCK=$(( RUN_SECONDS + 1200 ))
JOB_ID=""

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
    if [ -n "$JOB_ID" ]; then
        mc --json job destroy "$JOB_ID" --wait 0 >/dev/null 2>&1 || true
    fi
    rm -rf "$TMP_DIR"
}
trap cleanup EXIT

cd "$REPO_ROOT"
cat >"$TMP_DIR/train.py" <<PY
import time

started = time.time()
while time.time() - started < $RUN_SECONDS:
    print(f"elapsed {time.time() - started:.0f}s")
    time.sleep(60)
print("token boundary workload done")
PY
cat >"$SPEC_FILE" <<SPEC
name: token-boundary-$(date -u +%Y%m%dT%H%M%SZ)-$$
accelerator:
  prefer: []
  accept_cpu: true
code:
  kind: file
  root: $TMP_DIR
  entry: train.py
budgets:
  wall_clock: $WALL_CLOCK
SPEC

JOB_ID=$(mc --json job plan "$SPEC_FILE" | uv run python -c \
    "import json, sys; print(json.load(sys.stdin)['job_id'])")

STARTED=$(date +%s)
set +e
mc --json job apply --job-id "$JOB_ID" --timeout $(( WALL_CLOCK + 600 )) >"$APPLY_JSON"
APPLY_RC=$?
set -e
ELAPSED=$(( $(date +%s) - STARTED ))

APPLY_JSON="$APPLY_JSON" APPLY_RC="$APPLY_RC" ELAPSED="$ELAPSED" RUN_SECONDS="$RUN_SECONDS" \
JOB_DIR="$TMP_DIR/jobs/$JOB_ID" uv run python - <<'PY'
import json
import os
from pathlib import Path

envelope = json.loads(Path(os.environ["APPLY_JSON"]).read_text())
job = envelope["job"]
assert os.environ["APPLY_RC"] == "0", envelope
assert int(os.environ["ELAPSED"]) >= int(os.environ["RUN_SECONDS"]), (
    f"apply finished after {os.environ['ELAPSED']}s, before the workload could: {envelope}"
)
assert job["workload"] == "succeeded", job
assert job["exit_code"] == 0, job
assert job["reason"] is None, job
assert job["cleanup"] == "released", job
assert envelope["ok"] is True and envelope["done"] is True, envelope
log = Path(os.environ["JOB_DIR"], "runner.log").read_text()
assert "token boundary workload done" in log, log[-2000:]
print(f"apply succeeded after {os.environ['ELAPSED']}s for a {os.environ['RUN_SECONDS']}s workload")
PY
ENDPOINT=$(APPLY_JSON="$APPLY_JSON" uv run python -c \
    "import json, os; print(json.load(open(os.environ['APPLY_JSON']))['job']['endpoint'])")
JOB_ID=""

if endpoint_listed "$ENDPOINT"; then
    mc sessions
    echo "the released endpoint $ENDPOINT is still listed" >&2
    exit 1
fi

echo "[SUCCESS] a ${RUN_SECONDS}s job ran to completion under apply and was released afterwards"
