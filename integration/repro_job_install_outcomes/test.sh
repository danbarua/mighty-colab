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

# Dependency install on a real VM, three jobs:
#   1. a small real package: uv installs it on the first try; the envelope
#      has a one-line hint and no install_attempts, and the job succeeds;
#   2. a pin that does not exist: uv and pip both fail to resolve, the job
#      is fix_code, and both attempts are recorded with their key lines;
#   3. a direct reference on a host that does not resolve: both installers
#      fail to fetch, the job is retry_same.
# Each job's install.log, with a header and footer per attempt, is copied
# to the local job directory before its VM is released.

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)
TMP_DIR=$(cd "$(mktemp -d)" && pwd -P)
SESSION_FILE="$TMP_DIR/sessions.json"
JOB_ID=""

mc() {
    uv run mighty-colab --auth=adc --config "$SESSION_FILE" "$@"
}

cleanup() {
    if [ -n "$JOB_ID" ]; then
        mc --json job destroy "$JOB_ID" --wait 0 >/dev/null 2>&1 || true
    fi
    rm -rf "$TMP_DIR"
}
trap cleanup EXIT

cd "$REPO_ROOT"
printf 'import pip_install_test\nprint("imported")\n' >"$TMP_DIR/train.py"

# run_case NAME DEP: plans and applies a CPU job with one dependency, and
# leaves its apply envelope in $TMP_DIR/NAME.json and its job id in $JOB_ID.
run_case() {
    local name="$1" dep="$2"
    cat >"$TMP_DIR/$name.yaml" <<SPEC
name: install-$name-$(date -u +%Y%m%dT%H%M%SZ)-$$
accelerator:
  prefer: []
  accept_cpu: true
code:
  kind: file
  root: $TMP_DIR
  entry: train.py
deps:
  - "$dep"
budgets:
  wall_clock: 600
SPEC
    JOB_ID=$(mc --json job plan "$TMP_DIR/$name.yaml" --no-probe | uv run python -c \
        "import json, sys; print(json.load(sys.stdin)['job_id'])")
    set +e
    mc --json job apply --job-id "$JOB_ID" --timeout 1200 >"$TMP_DIR/$name.json"
    echo $? >"$TMP_DIR/$name.rc"
    set -e
}

check() {
    NAME="$1" TMP_DIR="$TMP_DIR" JOB_DIR="$TMP_DIR/jobs/$JOB_ID" uv run python -c "$2"
    ENDPOINT=$(uv run python -c \
        "import json; print(json.load(open('$TMP_DIR/$1.json'))['job']['endpoint'])")
    JOB_ID=""
    if mc sessions | grep -q "$ENDPOINT"; then
        mc sessions
        echo "case $1 left $ENDPOINT listed" >&2
        exit 1
    fi
}

COMMON='
import json, os
from pathlib import Path
name = os.environ["NAME"]
envelope = json.loads(Path(os.environ["TMP_DIR"], name + ".json").read_text())
job = envelope["job"]
rc = Path(os.environ["TMP_DIR"], name + ".rc").read_text().strip()
log = Path(os.environ["JOB_DIR"], "install.log").read_text()
assert log.count("=== mighty-colab install attempt ") >= 1, log[:2000]
assert job["cleanup"] == "released", job
'

run_case success "pip-install-test==0.5"
check success "$COMMON"'
assert rc == "0", envelope
assert job["workload"] == "succeeded", job
assert "install_attempts" not in job, job
assert any(h.startswith("dependencies installed with uv ") for h in job["hints"]), job["hints"]
assert "\"installer\": \"uv\"" in log
print("success:", [h for h in job["hints"] if h.startswith("dependencies installed")][0])
'

run_case resolution "mighty-colab-no-such-package==0.0.1"
check resolution "$COMMON"'
assert rc == "1", envelope
assert job["workload"] == "failed", job
assert job["reason"].startswith("dependency install failed (resolution)"), job
assert job["finished_at"], job
assert job["retry_class"] == "fix_code", job
assert "mighty-colab-no-such-package==0.0.1" in job["reason"], job
attempts = job["install_attempts"]
assert [a["installer"] for a in attempts] == ["uv", "pip"], attempts
assert [a["failure"] for a in attempts] == ["resolution", "resolution"], attempts
assert all(a["key_lines"] for a in attempts), attempts
assert log.count("=== mighty-colab install result ") == 2, log[-2000:]
print("resolution:", job["reason"][:300])
'

run_case transient "pip-install-test @ https://no-such-host.invalid/pip_install_test-0.5-py3-none-any.whl"
check transient "$COMMON"'
assert rc == "1", envelope
assert job["retry_class"] == "retry_same", job
attempts = job["install_attempts"]
assert attempts[0]["installer"] == "uv" and attempts[0]["failure"] == "transient", attempts
print("transient:", [(a["installer"], a["failure"]) for a in attempts], job["reason"][:300])
'

echo "[SUCCESS] uv installed a real package; a bad pin was fix_code and an unreachable host retry_same, with both installers recorded"
