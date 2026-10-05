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

# Every runner-side failure reaches the envelope with a reason that says
# what happened and the retry class chosen for it. Five CPU jobs:
#   1. wall_clock passes: "wall_clock budget of 60s reached", fix_code;
#   2. the workload allocates until the kernel's OOM killer takes it:
#      SIGKILL with the kernel's "Killed process" line, fix_code;
#   3. one input stages, the next is a 404 on a URL with a signed-looking
#      query: both inputs recorded, the response body kept, fix_code, the
#      consumer never runs, and the query appears in no local record;
#   4. an input whose sha256 is wrong: planned and received digests,
#      fix_code;
#   5. an uncaught exception from a library: its module-qualified type
#      and message are the reason;
#   6. the same exception before a required artifact was written: offload
#      fails (fix_code), and the default on_offload_fail: leave_up releases
#      the VM, because there is no file on it to rescue.

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)
TMP_DIR=$(cd "$(mktemp -d)" && pwd -P)
SESSION_FILE="$TMP_DIR/sessions.json"
JOB_ID=""
# A file pinned to a commit, so its size and digest never change.
DATA_URL="https://raw.githubusercontent.com/danbarua/mighty-colab/011b7978bab3d9356bae8a10f8253411c95ad8dd/LICENSE"
DATA_SHA256="cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30"
DATA_BYTES=11358
SENTINEL="RUNTIME_DETAIL_SIGNED_QUERY_SENTINEL_$$"
MISSING_URL="https://raw.githubusercontent.com/danbarua/mighty-colab/011b7978bab3d9356bae8a10f8253411c95ad8dd/no-such-file?X-Goog-Signature=$SENTINEL"

mc() {
    uv run mighty-colab --auth=adc --config "$SESSION_FILE" "$@"
}

endpoint_listed() {
    SESSIONS="$(mc --json sessions 2>/dev/null)" ENDPOINT="$1" uv run python -c \
        'import json, os; listed = {s.get("endpoint") for s in json.loads(os.environ["SESSIONS"])["sessions"]}; raise SystemExit(0 if os.environ["ENDPOINT"] in listed else 1)'
}

cleanup() {
    if [ -n "$JOB_ID" ]; then
        mc --json job destroy "$JOB_ID" --wait 0 >/dev/null 2>&1 || true
    fi
    rm -rf "$TMP_DIR"
}
trap cleanup EXIT

cd "$REPO_ROOT"
cat >"$TMP_DIR/sleeper.py" <<'PY'
import sys
import time

for i in range(int(sys.argv[1]) // 5):
    print(f"tick {i}", flush=True)
    time.sleep(5)
PY
cat >"$TMP_DIR/hog.py" <<'PY'
chunks = []
while True:
    chunks.append(bytearray(256 * 1024 * 1024))
    print(f"allocated {len(chunks) * 256} MiB", flush=True)
PY
cat >"$TMP_DIR/raises.py" <<'PY'
import json

json.loads("{")
PY

# run NAME ENTRY ARGS [EXTRA_YAML_LINES...]: plans and applies one CPU job
# (ARGS is a YAML list) and leaves its id in $JOB_ID. Planning skips the
# data probe so a bad input reaches the runner.
run() {
    local name="$1" entry="$2" args="$3"
    shift 3
    {
        echo "name: $name-$(date -u +%Y%m%dT%H%M%SZ)-$$"
        echo "accelerator: {prefer: [], accept_cpu: true}"
        echo "code: {kind: file, root: $TMP_DIR, entry: $entry, args: $args}"
        for line in "$@"; do echo "$line"; done
    } >"$TMP_DIR/$name.yaml"
    JOB_ID=$(mc --json job plan "$TMP_DIR/$name.yaml" --no-probe | uv run python -c \
        "import json, sys; print(json.load(sys.stdin)['job_id'])")
    mc --json job apply --job-id "$JOB_ID" >"$TMP_DIR/$name.json" || true
    if [ ! -f "$TMP_DIR/jobs/$JOB_ID/envelope.json" ]; then
        echo "$name: apply wrote no envelope:" >&2
        cat "$TMP_DIR/$name.json" >&2
        exit 1
    fi
}

check() {
    JOB_DIR="$TMP_DIR/jobs/$JOB_ID" uv run python -c "
import json, os
job_dir = os.environ['JOB_DIR']
env = json.load(open(os.path.join(job_dir, 'envelope.json')))
log = open(os.path.join(job_dir, 'runner.log')).read() if os.path.exists(os.path.join(job_dir, 'runner.log')) else ''
assert env['cleanup'] == 'released', env
$1"
    local endpoint
    endpoint=$(JOB_DIR="$TMP_DIR/jobs/$JOB_ID" uv run python -c \
        "import json, os; print(json.load(open(os.path.join(os.environ['JOB_DIR'], 'envelope.json')))['endpoint'] or '')")
    if [ -n "$endpoint" ] && endpoint_listed "$endpoint"; then
        echo "$JOB_ID left $endpoint listed" >&2
        exit 1
    fi
    JOB_ID=""
}

# 1. wall_clock
run wall-clock sleeper.py '["600"]' 'budgets: {wall_clock: 60}'
check '
assert env["workload"] == "cancelled", env
assert env["reason"] == "wall_clock budget of 60s reached; the workload was stopped by SIGTERM (15)", env
assert env["retry_class"] == "fix_code", env
assert env["failed_phase"] == "run", env
print("wall_clock:", env["reason"])
'

# 2. OOM
run oom hog.py '[]' 'budgets: {wall_clock: 600}'
check '
assert env["workload"] == "failed", env
assert env["signal"] == 9, env
assert "killed by SIGKILL (9) with no cancel request" in env["reason"], env
assert "out-of-memory killer ran" in env["reason"], env
assert "Killed process" in env["reason"], env
assert env["retry_class"] == "fix_code", env
print("oom:", env["reason"])
'

# 3. a 404 after a staged input
run stage-404 sleeper.py '["10"]' 'budgets: {wall_clock: 600}' \
    "data: [{url: \"$DATA_URL\", dest: inputs/LICENSE, sha256: $DATA_SHA256, size_bytes: $DATA_BYTES}, {url: \"$MISSING_URL\", dest: inputs/missing.bin, size_bytes: 1}]"
STAGE_JOB="$JOB_ID"
check '
assert env["workload"] == "failed", env
assert env["failed_phase"] == "stage", env
assert env["retry_class"] == "fix_code", env
ok, failed = env["inputs"]
assert ok["status"] == "ok" and ok["bytes"] == '"$DATA_BYTES"' and ok["sha256"] == "'"$DATA_SHA256"'", ok
assert failed["dest"] == "inputs/missing.bin" and failed["status"] == "failed", failed
assert failed["error"]["http_status"] == 404 and failed["error"]["category"] == "http", failed
assert failed["error"]["body"], failed
assert env["reason"].startswith("staging failed at inputs/missing.bin ("), env
assert "tick" not in log, "the consumer ran"
print("stage 404:", env["reason"], "| body:", failed["error"]["body"][:60])
'
# The secrets sidecar holds the full URL by design; nothing else may.
JOB_ROOT="$TMP_DIR/jobs/$STAGE_JOB" SENTINEL="$SENTINEL" APPLY_OUT="$TMP_DIR/stage-404.json" uv run python -c '
import os
from pathlib import Path
sentinel = os.environ["SENTINEL"].encode()
files = [p for p in Path(os.environ["JOB_ROOT"]).rglob("*") if p.is_file()]
files.append(Path(os.environ["APPLY_OUT"]))
leaks = [str(p) for p in files if not p.name.endswith(".mighty-colab-secrets.json") and sentinel in p.read_bytes()]
assert not leaks, leaks
print("stage 404: signed query in no local record")
'

# 4. a wrong sha256
run stage-sha sleeper.py '["10"]' 'budgets: {wall_clock: 600}' \
    "data: [{url: \"$DATA_URL\", dest: inputs/LICENSE, sha256: \"$(printf '0%.0s' $(seq 1 64))\", size_bytes: $DATA_BYTES}]"
check '
assert env["failed_phase"] == "stage", env
assert env["retry_class"] == "fix_code", env
[failed] = env["inputs"]
assert failed["error"]["category"] == "checksum", failed
assert "received sha256 '"$DATA_SHA256"', planned 0000" in env["reason"], env
print("stage sha256:", env["reason"])
'

# 5. an uncaught library exception
run raises raises.py '[]' 'budgets: {wall_clock: 600}'
check '
assert env["workload"] == "failed", env
assert env["exception"]["type"] == "json.decoder.JSONDecodeError", env
assert env["reason"].startswith("the workload exited 1: json.decoder.JSONDecodeError: Expecting property name"), env
assert env["retry_class"] == "fix_code", env
assert "Traceback" in log, "runner.log has no traceback"
print("exception:", env["reason"])
'

# 6. no artifact was produced: released despite leave_up
run missing-artifact raises.py '[]' 'budgets: {wall_clock: 600}' \
    "artifacts: [{path: out/model.pt, url: \"https://storage.googleapis.com/mighty-colab-no-such-bucket-4821/model.pt\", size_bytes: 1}]"
check '
assert env["offload"] == "failed", env
assert env["retry_class"] == "fix_code", env
assert "required artifact(s) not produced: out/model.pt" in env["reason"], env
assert not any("left running" in h for h in env["hints"]), env
print("missing artifact:", env["offload"], env["cleanup"], "|", env["reason"])
'

echo "[SUCCESS] wall_clock, OOM, a 404 input, a sha256 mismatch and an exception each carry their cause; a missing artifact releases the VM"
