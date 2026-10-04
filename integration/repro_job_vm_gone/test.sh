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

# The VM disappears under a running job (released out of band, as a
# preemption or idle reclaim would). `job apply` must notice the lost
# assignment, finish with workload=unknown and a terminal offload, and
# record cleanup as already_absent: its own unassign gets a 404, which only
# counts as absent once the assignment listing confirms the endpoint is
# gone. A second release of the same endpoint must not report a failure.

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)
TMP_DIR=$(cd "$(mktemp -d)" && pwd -P)
SESSION_FILE="$TMP_DIR/sessions.json"
SPEC_FILE="$TMP_DIR/spec.yaml"
APPLY_JSON="$TMP_DIR/apply.json"
APPLY_ERR="$TMP_DIR/apply.err"
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

release_out_of_band() {
    SESSION_FILE="$SESSION_FILE" ENDPOINT="$1" uv run python - <<'PY'
import os

from colab_cli.auth import AuthProvider
from colab_cli.common import state
from colab_cli.job.orchestrator import release_assignment

state.auth_provider = AuthProvider.ADC
state.config_path = os.environ["SESSION_FILE"]
cleanup, detail = release_assignment(state.client, os.environ["ENDPOINT"])
print(cleanup.value if detail is None else f"{cleanup.value}: {detail}")
PY
}

cd "$REPO_ROOT"
cat >"$TMP_DIR/train.py" <<'PY'
import time

for i in range(60):
    print(f"tick {i}")
    time.sleep(10)
PY
cat >"$SPEC_FILE" <<SPEC
name: vm-gone-$(date -u +%Y%m%dT%H%M%SZ)-$$
accelerator:
  prefer: []
  accept_cpu: true
code:
  kind: file
  root: $TMP_DIR
  entry: train.py
budgets:
  wall_clock: 900
SPEC

JOB_ID=$(mc --json job plan "$SPEC_FILE" | uv run python -c \
    "import json, sys; print(json.load(sys.stdin)['job_id'])")
JOB_DIR="$TMP_DIR/jobs/$JOB_ID"

mc --json job apply --job-id "$JOB_ID" --timeout 900 >"$APPLY_JSON" 2>"$APPLY_ERR" &
APPLY_PID=$!

ENDPOINT=""
for _ in $(seq 1 180); do
    if [ -f "$JOB_DIR/runner.log" ] && grep -q "tick 1" "$JOB_DIR/runner.log"; then
        ENDPOINT=$(JOB_DIR="$JOB_DIR" uv run python -c \
            "import json, os; print(json.load(open(os.path.join(os.environ['JOB_DIR'], 'envelope.json')))['endpoint'])")
        break
    fi
    if ! kill -0 "$APPLY_PID" 2>/dev/null; then
        cat "$APPLY_JSON" "$APPLY_ERR"
        echo "job apply exited before the workload was running" >&2
        exit 1
    fi
    sleep 2
done
if [ -z "$ENDPOINT" ]; then
    echo "the workload never reported progress" >&2
    exit 1
fi

FIRST=$(release_out_of_band "$ENDPOINT")
echo "out-of-band release of $ENDPOINT: $FIRST"
[ "$FIRST" = "released" ] || { echo "out-of-band release failed: $FIRST" >&2; exit 1; }

STARTED=$(date +%s)
set +e
wait "$APPLY_PID"
APPLY_RC=$?
set -e
APPLY_PID=""
ELAPSED=$(( $(date +%s) - STARTED ))

APPLY_JSON="$APPLY_JSON" APPLY_RC="$APPLY_RC" ELAPSED="$ELAPSED" uv run python - <<'PY'
import json
import os
from pathlib import Path

envelope = json.loads(Path(os.environ["APPLY_JSON"]).read_text())
job = envelope["job"]
assert os.environ["APPLY_RC"] == "1", envelope
assert int(os.environ["ELAPSED"]) < 300, f"apply took {os.environ['ELAPSED']}s to notice: {envelope}"
assert job["workload"] == "unknown", job
assert job["reason"] == "the assignment is gone from the server", job
assert job["retry_class"] == "retry_same", job
assert job["offload"] == "not_required", job
assert job["cleanup"] == "already_absent", job
assert job["supervisor"] == "finished", job
assert envelope["done"] is True, envelope
print(f"apply noticed the lost assignment after {os.environ['ELAPSED']}s; cleanup={job['cleanup']}")
PY

SECOND=$(release_out_of_band "$ENDPOINT")
echo "second release of $ENDPOINT: $SECOND"
[ "$SECOND" = "already_absent" ] || { echo "a second release should be already_absent, got: $SECOND" >&2; exit 1; }
JOB_ID=""

if endpoint_listed "$ENDPOINT"; then
    mc sessions
    echo "the released endpoint $ENDPOINT is still listed" >&2
    exit 1
fi

echo "[SUCCESS] apply recorded a VM released underneath it as already_absent, confirmed by the listing"
