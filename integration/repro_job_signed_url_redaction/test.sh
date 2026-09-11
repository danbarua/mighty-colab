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
PLAN_FILE="$TMP_DIR/plan.json"
TRAIN_FILE="$TMP_DIR/train.py"
APPLY_LOG="$TMP_DIR/apply.log"
SENTINEL="ISSUE18_LIVE_SIGNED_URL_SENTINEL_$$"
JOB_ID=""
SESSION_NAME=""

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
from pathlib import Path

payload = Path("/content/data/input.txt").read_bytes()
assert payload
print("signed URL input downloaded")
PY

RUN_NAME="signed-url-redaction-$(date -u +%Y%m%dT%H%M%SZ)-$$"
cat >"$SPEC_FILE" <<EOF
name: $RUN_NAME
ignore_warnings: true
accelerator:
  prefer: []
  accept_cpu: true
code:
  kind: file
  root: $TMP_DIR
  entry: train.py
data:
  - url: https://raw.githubusercontent.com/danbarua/mighty-colab/main/README.md?signature=$SENTINEL
    dest: /content/data/input.txt
budgets:
  wall_clock: 180
EOF

cd "$REPO_ROOT"
PLAN_JSON=$(mc --json job plan "$SPEC_FILE" --out "$PLAN_FILE" --no-probe)
if echo "$PLAN_JSON" | grep -Fq "$SENTINEL"; then
    echo "plan output disclosed the signed URL" >&2
    exit 1
fi
JOB_ID=$(PLAN_JSON="$PLAN_JSON" uv run python - <<'PY'
import json
import os

print(json.loads(os.environ["PLAN_JSON"])["job_id"])
PY
)
SESSION_NAME="job-$JOB_ID"

PLAN_FILE="$PLAN_FILE" SENTINEL="$SENTINEL" uv run python - <<'PY'
import os
import stat
from pathlib import Path

plan = Path(os.environ["PLAN_FILE"])
sentinel = os.environ["SENTINEL"]
assert sentinel not in plan.read_text()
sidecar = plan.with_name(plan.name + ".mighty-colab-secrets.json")
assert sentinel in sidecar.read_text()
assert stat.S_IMODE(sidecar.stat().st_mode) == 0o600
PY

if ! mc --json job apply "$PLAN_FILE" --leave-up >"$APPLY_LOG" 2>&1; then
    sed "s/$SENTINEL/[REDACTED]/g" "$APPLY_LOG" >&2
    exit 1
fi
if grep -Fq "$SENTINEL" "$APPLY_LOG"; then
    sed "s/$SENTINEL/[REDACTED]/g" "$APPLY_LOG" >&2
    echo "apply output disclosed the signed URL" >&2
    exit 1
fi

JOB_ROOT="$TMP_DIR/jobs/$JOB_ID"
JOB_ROOT="$JOB_ROOT" SENTINEL="$SENTINEL" uv run python - <<'PY'
import os
from pathlib import Path

root = Path(os.environ["JOB_ROOT"])
sentinel = os.environ["SENTINEL"]
for path in root.rglob("*"):
    if path.is_file() and not path.name.endswith(".mighty-colab-secrets.json"):
        assert sentinel.encode() not in path.read_bytes(), path
PY

PROBE_OUTPUT=$(cat <<'PY' | mc exec -s "$SESSION_NAME"
from pathlib import Path

needle = ("ISSUE18_LIVE_SIGNED" + "_URL_SENTINEL_" + str(""))
# Recover the variable suffix from no public file: a leaked credential has the
# fixed prefix even when this probe does not know the local shell PID.
bad = []
for path in Path("/content/jobs").rglob("*"):
    if path.is_file():
        try:
            if needle.encode() in path.read_bytes():
                bad.append(str(path))
        except OSError:
            pass
ip = get_ipython()
for _session, _line, source in ip.history_manager.search("*", raw=True, output=False):
    if needle in source:
        bad.append("kernel-history")
secret = list(Path("/content/jobs").rglob("mighty_runtime/.secrets/transfer.json"))
assert not bad, bad
assert not secret, secret
print("REMOTE_SECRET_SCAN_OK")
PY
)
if ! echo "$PROBE_OUTPUT" | grep -q "REMOTE_SECRET_SCAN_OK"; then
    echo "$PROBE_OUTPUT"
    echo "remote secrecy probe failed" >&2
    exit 1
fi

DESTROY_JSON=$(mc --json job destroy "$JOB_ID")
DESTROY_JSON="$DESTROY_JSON" uv run python - <<'PY'
import json
import os

payload = json.loads(os.environ["DESTROY_JSON"])
assert payload["job"]["cleanup"] in {"released", "already_absent"}, payload
PY
JOB_ID=""

if ! mc sessions | grep -q "No active sessions"; then
    mc sessions
    echo "job integration left an active session" >&2
    exit 1
fi

echo "[SUCCESS] signed URL sentinel remained only in approved local secret channels"
