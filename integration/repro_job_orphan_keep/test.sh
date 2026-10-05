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

# The detached `job status --poll` that apply hands off to on SIGTERM keeps
# the VM by the same rule as apply. Two CPU jobs, each SIGTERMed after
# launch:
#   1. the run writes its artifact and the upload fails (a GCS URL with a
#      fabricated signature answers 403 SignatureDoesNotMatch): under the
#      default on_offload_fail: leave_up the detached poll keeps the VM
#      (cleanup: left_up), and the artifact record keeps the 403 and the
#      start of its body;
#   2. the run never writes its artifact: offload fails (fix_code) but
#      there is nothing to rescue, so the detached poll releases the VM.

set -euo pipefail
# Background jobs with SIGINT at default; see repro_job_timeout_and_interrupt.
set -m

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)
TMP_DIR=$(cd "$(mktemp -d)" && pwd -P)
SESSION_FILE="$TMP_DIR/sessions.json"
JOBS=()
APPLY_PID=""
NOW=$(date -u +%Y%m%dT%H%M%SZ)
PUT_URL="https://storage.googleapis.com/gcp-public-data-landsat/mighty-colab-repro/out.bin?X-Goog-Algorithm=GOOG4-RSA-SHA256&X-Goog-Credential=nobody%40example.iam.gserviceaccount.com%2F${NOW:0:8}%2Fauto%2Fstorage%2Fgoog4_request&X-Goog-Date=${NOW}&X-Goog-Expires=604800&X-Goog-SignedHeaders=host&X-Goog-Signature=0123SENTINEL"

mc() {
    uv run mighty-colab --auth=adc --config "$SESSION_FILE" "$@"
}

endpoint_listed() {
    SESSIONS="$(mc --json sessions 2>/dev/null)" ENDPOINT="$1" uv run python -c \
        'import json, os; listed = {s.get("endpoint") for s in json.loads(os.environ["SESSIONS"])["sessions"]}; raise SystemExit(0 if os.environ["ENDPOINT"] in listed else 1)'
}

cleanup() {
    if [ -n "$APPLY_PID" ] && kill -0 "$APPLY_PID" 2>/dev/null; then
        kill "$APPLY_PID" 2>/dev/null || true
    fi
    for job in "${JOBS[@]}"; do
        mc --json job destroy "$job" --wait 0 >/dev/null 2>&1 || true
    done
    rm -rf "$TMP_DIR"
}
trap cleanup EXIT

cd "$REPO_ROOT"
cat >"$TMP_DIR/writes.py" <<'PY'
import os, time
os.makedirs("/content/out", exist_ok=True)
with open("/content/out/model.bin", "wb") as f:
    f.write(b"x" * 1024)
for i in range(6):
    print(f"tick {i}", flush=True)
    time.sleep(5)
PY
cat >"$TMP_DIR/writes_nothing.py" <<'PY'
import time
for i in range(6):
    print(f"tick {i}", flush=True)
    time.sleep(5)
PY

field() {
    JOB_DIR="$TMP_DIR/jobs/$1" FIELD="$2" uv run python -c \
        "import json, os; print(json.load(open(os.path.join(os.environ['JOB_DIR'], 'envelope.json'))).get(os.environ['FIELD']) or '')"
}

# run NAME ENTRY: plan, apply in the background, SIGTERM it after launch,
# then wait (bounded) for the detached poll to finish the job.
run() {
    local name="$1" entry="$2" job pid
    cat >"$TMP_DIR/$name.yaml" <<YAML
name: $name-$(date -u +%Y%m%dT%H%M%SZ)-$$
accelerator: {prefer: [], accept_cpu: true}
code: {kind: file, root: $TMP_DIR, entry: $entry}
budgets: {wall_clock: 600}
artifacts: [{path: /content/out/model.bin, url: "$PUT_URL", size_bytes: 1024}]
YAML
    job=$(mc --json job plan "$TMP_DIR/$name.yaml" --no-probe | uv run python -c \
        "import json, sys; print(json.load(sys.stdin)['job_id'])")
    JOBS+=("$job")
    JOB_ID="$job"
    mc --json job apply --job-id "$job" >"$TMP_DIR/$name.apply.json" 2>/dev/null &
    APPLY_PID=$!
    for _ in $(seq 1 150); do
        [ -f "$TMP_DIR/jobs/$job/runner.log" ] && grep -q "tick 0" "$TMP_DIR/jobs/$job/runner.log" && break
        kill -0 "$APPLY_PID" 2>/dev/null || { echo "$name: apply exited before the run" >&2; exit 1; }
        sleep 2
    done
    pid=$(JOB_DIR="$TMP_DIR/jobs/$job" uv run python -c \
        "import json, os; print(json.load(open(os.path.join(os.environ['JOB_DIR'], 'supervisor.json')))['pid'])")
    kill -TERM "$pid"
    for _ in $(seq 1 30); do kill -0 "$APPLY_PID" 2>/dev/null || break; sleep 2; done
    kill -0 "$APPLY_PID" 2>/dev/null && { echo "$name: apply did not exit after SIGTERM" >&2; exit 1; }
    set +e; wait "$APPLY_PID"; set -e
    APPLY_PID=""
    for _ in $(seq 1 120); do
        case "$(field "$job" cleanup)" in released|left_up|already_absent|failed) break ;; esac
        sleep 5
    done
}

# 1. a failed upload: kept
run failed-upload writes.py
JOB_DIR="$TMP_DIR/jobs/$JOB_ID" uv run python - <<'PY'
import json, os
env = json.load(open(os.path.join(os.environ["JOB_DIR"], "envelope.json")))
assert env["cleanup"] == "left_up", env
assert env["offload"] == "failed", env
[artifact] = env["artifacts"]
assert artifact["status"] == "failed" and artifact["error"]["http_status"] == 403, artifact
assert "SignatureDoesNotMatch" in (artifact["error"]["body"] or ""), artifact
assert "0123SENTINEL" not in json.dumps(env), "the fabricated signature reached the envelope"
assert env["retry_class"] == "refresh_urls", env
print("failed upload:", env["cleanup"], "|", env["reason"][:160], "| body:", artifact["error"]["body"][:60])
PY
ENDPOINT=$(field "$JOB_ID" endpoint)
endpoint_listed "$ENDPOINT" || { echo "the kept VM is not listed" >&2; exit 1; }
mc --json job destroy "$JOB_ID" --wait 0 >/dev/null
endpoint_listed "$ENDPOINT" && { echo "destroy left $ENDPOINT listed" >&2; exit 1; }
echo "failed upload: kept, then destroyed"

# 2. no artifact written: released
run missing-artifact writes_nothing.py
JOB_DIR="$TMP_DIR/jobs/$JOB_ID" uv run python - <<'PY'
import json, os
env = json.load(open(os.path.join(os.environ["JOB_DIR"], "envelope.json")))
assert env["cleanup"] == "released", env
assert env["offload"] == "failed" and env["retry_class"] == "fix_code", env
assert "required artifact(s) not produced: /content/out/model.bin" in env["reason"], env
print("missing artifact:", env["cleanup"], "|", env["reason"][:160])
PY
ENDPOINT=$(field "$JOB_ID" endpoint)
endpoint_listed "$ENDPOINT" && { echo "the missing-artifact VM is still listed" >&2; exit 1; }

echo "[SUCCESS] the detached poll kept the VM for a failed upload and released it for a missing artifact"
