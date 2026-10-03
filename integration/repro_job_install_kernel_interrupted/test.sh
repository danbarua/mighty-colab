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

# The launch kernel is shut down while dependencies install. Jupyter
# interrupts a busy kernel before shutting it down, so the install cell
# ends with a KeyboardInterrupt error output. `job apply` must report that
# as "kernel interrupted during install" with retry_same, keep install.log,
# and release the VM.
#
# This covers an interrupted cell, not a dropped websocket: a websocket
# drop (RuntimeError "Connection was lost.") has no on-demand trigger and
# is covered by unit tests only.
#
# The kernel's id is not in the session record until the first execute
# call returns, so `restart-kernel -s` cannot reach it during install; the
# script finds the busy kernel through /api/kernels instead. It prints the
# reason it observed.

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
printf 'print("never runs")\n' >"$TMP_DIR/train.py"
cat >"$SPEC_FILE" <<SPEC
name: install-lost-$(date -u +%Y%m%dT%H%M%SZ)-$$
accelerator:
  prefer: []
  accept_cpu: true
code:
  kind: file
  root: $TMP_DIR
  entry: train.py
deps:
  - torch==2.6.0
budgets:
  wall_clock: 600
SPEC

JOB_ID=$(mc --json job plan "$SPEC_FILE" --no-probe | uv run python -c \
    "import json, sys; print(json.load(sys.stdin)['job_id'])")
JOB_DIR="$TMP_DIR/jobs/$JOB_ID"
SESSION_NAME="job-$JOB_ID"

mc --json job apply --job-id "$JOB_ID" --timeout 1200 >"$APPLY_JSON" 2>"$APPLY_ERR" &
APPLY_PID=$!

PHASE=""
for _ in $(seq 1 120); do
    if [ -f "$JOB_DIR/envelope.json" ]; then
        PHASE=$(JOB_DIR="$JOB_DIR" uv run python -c \
            "import json, os; print(json.load(open(os.path.join(os.environ['JOB_DIR'], 'envelope.json'))).get('phase') or '')")
        [ "$PHASE" = "install" ] && break
    fi
    if ! kill -0 "$APPLY_PID" 2>/dev/null; then
        cat "$APPLY_JSON" "$APPLY_ERR"
        echo "job apply exited before the install phase" >&2
        exit 1
    fi
    sleep 2
done
[ "$PHASE" = "install" ] || { echo "apply never reached the install phase" >&2; exit 1; }

sleep 15
echo "[*] shutting down the busy launch kernel during install"
SESSION_FILE="$SESSION_FILE" SESSION_NAME="$SESSION_NAME" uv run python - <<'PY'
import json
import os

import requests

record = json.load(open(os.environ["SESSION_FILE"]))[os.environ["SESSION_NAME"]]
base = record["url"].rstrip("/")
params = {"authuser": "0", "colab-runtime-proxy-token": record["token"]}
kernels = requests.get(f"{base}/api/kernels", params=params, timeout=30).json()
busy = [k for k in kernels if k.get("execution_state") == "busy"]
assert busy, f"no busy kernel during install: {kernels}"
response = requests.delete(f"{base}/api/kernels/{busy[0]['id']}", params=params, timeout=60)
print(f"shut down kernel {busy[0]['id']}: HTTP {response.status_code}")
assert response.status_code in (200, 204), response.text[:300]
PY
RESTARTED=$(date +%s)

for _ in $(seq 1 240); do
    kill -0 "$APPLY_PID" 2>/dev/null || break
    sleep 2
done
if kill -0 "$APPLY_PID" 2>/dev/null; then
    echo "job apply had not returned 8 minutes after the kernel shutdown" >&2
    exit 1
fi
set +e
wait "$APPLY_PID"
APPLY_RC=$?
set -e
APPLY_PID=""
ELAPSED=$(( $(date +%s) - RESTARTED ))

APPLY_JSON="$APPLY_JSON" APPLY_RC="$APPLY_RC" ELAPSED="$ELAPSED" JOB_DIR="$JOB_DIR" uv run python - <<'PY'
import json
import os
from pathlib import Path

envelope = json.loads(Path(os.environ["APPLY_JSON"]).read_text())
job = envelope["job"]
print(f"apply returned {os.environ['ELAPSED']}s after the kernel shutdown: {job['reason']}")
assert os.environ["APPLY_RC"] == "1", envelope
assert job["retry_class"] == "retry_same", job
assert job["reason"].startswith("kernel interrupted during install"), job
assert job["cleanup"] == "released", job
log = Path(os.environ["JOB_DIR"], "install.log").read_text()
assert "=== mighty-colab install attempt " in log, log[:2000]
assert any("install.log" in h for h in job["hints"]), job["hints"]
PY
ENDPOINT=$(APPLY_JSON="$APPLY_JSON" uv run python -c \
    "import json, os; print(json.load(open(os.environ['APPLY_JSON']))['job']['endpoint'])")
JOB_ID=""
if grep -q -- "$ENDPOINT" <<<"$(mc sessions)"; then
    mc sessions
    echo "the released endpoint $ENDPOINT is still listed" >&2
    exit 1
fi

echo "[SUCCESS] a kernel interrupted during install was retry_same, with install.log kept and the VM released"
