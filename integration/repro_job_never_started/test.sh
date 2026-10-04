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

# A runner that exits before it writes launch.json (a broken runtime
# payload, a bad runner argument) must not keep `job apply` waiting for its
# local deadline with the VM billing. apply declares the runner never
# started once launch.json has stayed absent past the grace and
# confirmation windows (about 3.5 minutes after launch), copies runner.log,
# and releases the VM.
#
# The fault is injected into a copy of the CLI, never into the checkout:
# src/colab_cli is copied to an overlay directory that comes first on
# PYTHONPATH, and the copy's runner raises at the start of main().

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)
TMP_DIR=$(cd "$(mktemp -d)" && pwd -P)
SESSION_FILE="$TMP_DIR/sessions.json"
SPEC_FILE="$TMP_DIR/spec.yaml"
OVERLAY="$TMP_DIR/overlay"
APPLY_JSON="$TMP_DIR/apply.json"
MARKER="repro_job_never_started: runner exits before launch.json"
JOB_ID=""

mc() {
    PYTHONPATH="$OVERLAY" uv run mighty-colab --auth=adc --config "$SESSION_FILE" "$@"
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
mkdir -p "$OVERLAY"
cp -R "$REPO_ROOT/src/colab_cli" "$OVERLAY/"
OVERLAY="$OVERLAY" MARKER="$MARKER" uv run python - <<'PY'
import os
from pathlib import Path

runner = Path(os.environ["OVERLAY"], "colab_cli/job/runtime_payload/runner.py")
text = runner.read_text()
signature = "def main(argv):\n"
assert text.count(signature) == 1, "runner.main signature changed"
runner.write_text(
    text.replace(signature, signature + f"    raise SystemExit({os.environ['MARKER']!r})\n")
)
PY
LOADED=$(PYTHONPATH="$OVERLAY" uv run python -c \
    "import colab_cli.job.runtime_payload.runner as r; print(r.__file__)")
case "$LOADED" in
    "$OVERLAY"/*) ;;
    *) echo "the faulty overlay was not loaded (got $LOADED)" >&2; exit 1 ;;
esac

printf 'print("never runs")\n' >"$TMP_DIR/train.py"
cat >"$SPEC_FILE" <<SPEC
name: never-started-$(date -u +%Y%m%dT%H%M%SZ)-$$
accelerator:
  prefer: []
  accept_cpu: true
code:
  kind: file
  root: $TMP_DIR
  entry: train.py
budgets:
  wall_clock: 600
SPEC

JOB_ID=$(mc --json job plan "$SPEC_FILE" | uv run python -c \
    "import json, sys; print(json.load(sys.stdin)['job_id'])")

STARTED=$(date +%s)
set +e
mc --json job apply --job-id "$JOB_ID" --timeout 900 >"$APPLY_JSON"
APPLY_RC=$?
set -e
ELAPSED=$(( $(date +%s) - STARTED ))

APPLY_JSON="$APPLY_JSON" APPLY_RC="$APPLY_RC" ELAPSED="$ELAPSED" MARKER="$MARKER" \
JOB_DIR="$TMP_DIR/jobs/$JOB_ID" uv run python - <<'PY'
import json
import os
from pathlib import Path

envelope = json.loads(Path(os.environ["APPLY_JSON"]).read_text())
job = envelope["job"]
assert os.environ["APPLY_RC"] == "1", envelope
assert int(os.environ["ELAPSED"]) < 900, f"apply ran into its --timeout: {envelope}"
assert job["workload"] == "unknown", job
assert "never started" in (job["reason"] or ""), job
assert "runner.log" in job["reason"], job
assert job["retry_class"] == "retry_same", job
assert job["offload"] == "not_required", job
assert job["cleanup"] == "released", job
assert job["supervisor"] == "finished", job
assert envelope["done"] is True, envelope
assert job["finished_at"], job
assert any(
    "VM records copied before release" in h and "runner.log" in h for h in job["hints"]
), job["hints"]
log = Path(os.environ["JOB_DIR"], "runner.log").read_text()
assert os.environ["MARKER"] in log, log
print(f"apply released the never-started job after {os.environ['ELAPSED']}s: {job['reason']}")
PY
ENDPOINT=$(APPLY_JSON="$APPLY_JSON" uv run python -c \
    "import json, os; print(json.load(open(os.environ['APPLY_JSON']))['job']['endpoint'])")
JOB_ID=""

if endpoint_listed "$ENDPOINT"; then
    mc sessions
    echo "the released endpoint $ENDPOINT is still listed" >&2
    exit 1
fi

echo "[SUCCESS] a runner that never wrote launch.json was declared never started and its VM released"
