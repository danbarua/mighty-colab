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

# Integration Test: `--json` on exec/run/exec-async/log --tail
#
# Verifies the machine-readable output flag added for
# docs/AGENT_USABILITY_LEARNINGS.md asks #2/#6/#8, against a real backend:
#   1. `exec --json` returns one clean JSON object on stdout with the
#      documented envelope fields.
#   2. A script that ends with `sys.exit(0)` after doing real work resolves
#      to status="ok" -- NOT "job_raised" -- even though IPython reports
#      SystemExit as an error-type kernel output. This is the exact
#      incident that motivated the whole design (see the plan's Context
#      section): a naive check misread a successful run as a failure and
#      tore down a session with its result still on it.
#   3. `run --json` produces the equivalent single-block envelope.
#   4. `exec-async --json` returns `{status:"started", pid, log_path}`
#      immediately; polling `log --tail --json --since-offset` (no jq
#      available in the driving shell, deliberately -- parsed with
#      `python3 -c` instead, mirroring a real caller with no JSON tooling)
#      shows status="running" while the job is in flight, then a terminal
#      envelope once the sidecar file appears, whose fields match the
#      sidecar file's own content on disk.
#   5. After `stop`, `log --tail --json` (default path) still resolves the
#      sidecar -- the result survives session teardown.
#
# Does not exercise `--json-result-path` directly (it's a hidden flag
# `exec-async` wires up internally) -- covered by mocked unit tests in
# tests/test_exec_async_json.py.

# Don't `set -e` so we can capture failures and still clean up explicitly.

# ---------- Auth detection (mirrors integration/repro_exec_async/test.sh) ---
if [ -f "$HOME/.config/colab-cli/token.json" ]; then
    AUTH_FLAGS="--auth=oauth2"
elif command -v gcloud > /dev/null && gcloud auth application-default print-access-token > /dev/null 2>&1; then
    ADC_TOKEN=$(gcloud auth application-default print-access-token 2>/dev/null)
    ADC_SCOPES=$(curl -s "https://www.googleapis.com/oauth2/v3/tokeninfo?access_token=$ADC_TOKEN" | python3 -c "import json,sys; print(json.load(sys.stdin).get('scope',''))" 2>/dev/null)
    if echo "$ADC_SCOPES" | grep -q "colaboratory" && echo "$ADC_SCOPES" | grep -q "userinfo.email"; then
        AUTH_FLAGS="--auth=adc"
    else
        echo "Error: ADC token lacks the required scopes (colaboratory + userinfo.email)."
        exit 1
    fi
else
    echo "Error: No usable auth provider found."
    exit 1
fi
echo "[*] Using $AUTH_FLAGS"

# ---------- Isolated session state -------------------------------------------
TMP_DIR=$(mktemp -d)
SESSION_FILE="$TMP_DIR/sessions.json"
SESSION_NAME="repro-json-output-$(date +%s)"

cleanup() {
    echo "[*] Cleaning up..."
    uv run mighty-colab $AUTH_FLAGS --config "$SESSION_FILE" stop -s "$SESSION_NAME" 2>/dev/null || true
    rm -rf "$TMP_DIR"
}
trap cleanup EXIT

# Extracts a top-level field from a JSON object on stdin. Deliberately not
# jq -- a real caller reported not having it available either.
jfield() {
    python3 -c "import json,sys; print(json.load(sys.stdin).get('$1', ''))"
}

echo "[*] Creating session '$SESSION_NAME' (REAL API CALL)..."
uv run mighty-colab $AUTH_FLAGS --config "$SESSION_FILE" new -s "$SESSION_NAME"

# ---------- Phase 1: exec --json envelope shape ------------------------------
echo ""
echo "[*] Phase 1: exec --json produces a clean JSON envelope on stdout"
JSON_OUT=$(echo "print('hello from --json')" | uv run mighty-colab $AUTH_FLAGS --config "$SESSION_FILE" --json exec -s "$SESSION_NAME" 2>/tmp/repro_json_stderr.log)
RC=$?
echo "$JSON_OUT"

if [ $RC -ne 0 ]; then
    echo "[FAILURE] exec --json exited $RC"
    cat /tmp/repro_json_stderr.log
    exit 1
fi
STATUS=$(echo "$JSON_OUT" | jfield status)
SCHEMA=$(echo "$JSON_OUT" | jfield schema_version)
CLI_VERSION=$(echo "$JSON_OUT" | jfield cli_version)
if [ "$STATUS" != "ok" ] || [ -z "$SCHEMA" ] || [ -z "$CLI_VERSION" ]; then
    echo "[FAILURE] envelope missing expected fields (status=$STATUS schema_version=$SCHEMA cli_version=$CLI_VERSION)"
    exit 1
fi
echo "[SUCCESS] Phase 1 passed: status=ok, schema_version=$SCHEMA, cli_version=$CLI_VERSION"

# ---------- Phase 2: SystemExit(0) after real work resolves to status=ok ----
echo ""
echo "[*] Phase 2: sys.exit(0) after real work must be status=ok, not job_raised"
JSON_OUT=$(cat <<'PYEOF' | uv run mighty-colab $AUTH_FLAGS --config "$SESSION_FILE" --json exec -s "$SESSION_NAME"
import sys
result = 1 + 1
print(f"computed: {result}")
sys.exit(0)
PYEOF
)
RC=$?
echo "$JSON_OUT"

if [ $RC -ne 0 ]; then
    echo "[FAILURE] exec --json exited $RC on a sys.exit(0) script -- process"
    echo "          exit code must stay 0 under --json even though this is"
    echo "          exactly the shape IPython reports as an 'error' output."
    exit 1
fi
STATUS=$(echo "$JSON_OUT" | jfield status)
EXIT_CODE=$(echo "$JSON_OUT" | jfield exit_code)
if [ "$STATUS" != "ok" ]; then
    echo "[FAILURE] status=$STATUS, wanted 'ok'. This is the exact incident"
    echo "          that motivated this design: SystemExit(0) misread as a"
    echo "          job failure."
    exit 1
fi
if [ "$EXIT_CODE" != "0" ]; then
    echo "[FAILURE] exit_code=$EXIT_CODE, wanted 0."
    exit 1
fi
echo "[SUCCESS] Phase 2 passed: status=ok, exit_code=0 for a sys.exit(0) script."

# ---------- Phase 3: run --json ----------------------------------------------
echo ""
echo "[*] Phase 3: run --json produces a single-block envelope"
SCRIPT_PATH="$TMP_DIR/run_script.py"
cat > "$SCRIPT_PATH" <<'PYEOF'
print("hello from run --json")
PYEOF
RUN_SESSION="repro-json-run-$(date +%s)"
JSON_OUT=$(uv run mighty-colab $AUTH_FLAGS --config "$SESSION_FILE" --json run -s "$RUN_SESSION" "$SCRIPT_PATH" 2>/tmp/repro_json_run_stderr.log)
RC=$?
echo "$JSON_OUT"
if [ $RC -ne 0 ]; then
    echo "[FAILURE] run --json exited $RC"
    cat /tmp/repro_json_run_stderr.log
    exit 1
fi
STATUS=$(echo "$JSON_OUT" | jfield status)
if [ "$STATUS" != "ok" ]; then
    echo "[FAILURE] run --json status=$STATUS, wanted 'ok'."
    exit 1
fi
echo "[SUCCESS] Phase 3 passed: run --json status=ok."

# ---------- Phase 4: exec-async --json + log --tail --json polling ----------
echo ""
echo "[*] Phase 4: exec-async --json submission envelope + polling to completion"
ASYNC_SCRIPT="$TMP_DIR/async_script.py"
cat > "$ASYNC_SCRIPT" <<'PYEOF'
import time
for i in range(4):
    print(f"step {i}", flush=True)
    time.sleep(2)
print("done", flush=True)
PYEOF

SUBMIT_OUT=$(uv run mighty-colab $AUTH_FLAGS --config "$SESSION_FILE" --json exec-async -s "$SESSION_NAME" -f "$ASYNC_SCRIPT" 2>/tmp/repro_json_async_stderr.log)
RC=$?
echo "$SUBMIT_OUT"
if [ $RC -ne 0 ]; then
    echo "[FAILURE] exec-async --json exited $RC"
    cat /tmp/repro_json_async_stderr.log
    exit 1
fi
SUBMIT_STATUS=$(echo "$SUBMIT_OUT" | jfield status)
LOG_PATH=$(echo "$SUBMIT_OUT" | jfield log_path)
if [ "$SUBMIT_STATUS" != "started" ] || [ -z "$LOG_PATH" ]; then
    echo "[FAILURE] submission envelope missing status=started/log_path (status=$SUBMIT_STATUS log_path=$LOG_PATH)"
    exit 1
fi
echo "[*] log_path=$LOG_PATH"

OFFSET=0
SAW_RUNNING=0
FINAL_ENVELOPE=""
for i in $(seq 1 15); do
    POLL_OUT=$(uv run mighty-colab $AUTH_FLAGS --config "$SESSION_FILE" --json log -s "$SESSION_NAME" --tail --since-offset "$OFFSET")
    POLL_STATUS=$(echo "$POLL_OUT" | jfield status)
    NEXT_OFFSET=$(echo "$POLL_OUT" | jfield next_offset)
    echo "[poll $i] status=$POLL_STATUS next_offset=$NEXT_OFFSET"
    OFFSET="$NEXT_OFFSET"
    if [ "$POLL_STATUS" = "running" ]; then
        SAW_RUNNING=1
    elif [ -n "$POLL_STATUS" ] && [ "$POLL_STATUS" != "running" ]; then
        FINAL_ENVELOPE="$POLL_OUT"
        break
    fi
    sleep 2
done

if [ -z "$FINAL_ENVELOPE" ]; then
    echo "[FAILURE] job never reached a terminal status within the poll budget."
    exit 1
fi
if [ "$SAW_RUNNING" -ne 1 ]; then
    echo "[FAILURE] never observed status=running while the job was in flight"
    echo "          -- either the job finished before the first poll, or"
    echo "          the running-state detection is broken."
    exit 1
fi
FINAL_STATUS=$(echo "$FINAL_ENVELOPE" | jfield status)
if [ "$FINAL_STATUS" != "ok" ]; then
    echo "[FAILURE] final status=$FINAL_STATUS, wanted 'ok'."
    exit 1
fi
echo "[SUCCESS] observed running -> $FINAL_STATUS."

echo "[*] Confirming the sidecar file's on-disk content matches the final poll"
SIDECAR_PATH="${LOG_PATH}.json"
if [ ! -f "$SIDECAR_PATH" ]; then
    echo "[FAILURE] sidecar file not found: $SIDECAR_PATH"
    exit 1
fi
SIDECAR_STATUS=$(jfield status < "$SIDECAR_PATH")
if [ "$SIDECAR_STATUS" != "$FINAL_STATUS" ]; then
    echo "[FAILURE] sidecar status=$SIDECAR_STATUS != final poll status=$FINAL_STATUS"
    exit 1
fi
echo "[SUCCESS] Phase 4 passed: sidecar content matches the final --tail --json poll."

# ---------- Phase 5: result survives `stop` ----------------------------------
echo ""
echo "[*] Phase 5: log --tail --json (default path) still resolves after stop"
uv run mighty-colab $AUTH_FLAGS --config "$SESSION_FILE" stop -s "$SESSION_NAME"
AFTER_STOP=$(uv run mighty-colab $AUTH_FLAGS --config "$SESSION_FILE" --json log -s "$SESSION_NAME" --tail)
echo "$AFTER_STOP"
AFTER_STOP_STATUS=$(echo "$AFTER_STOP" | jfield status)
if [ "$AFTER_STOP_STATUS" != "ok" ]; then
    echo "[FAILURE] status=$AFTER_STOP_STATUS after stop, wanted 'ok' -- the"
    echo "          async job's result must survive session teardown."
    exit 1
fi
echo "[SUCCESS] Phase 5 passed: result resolved via the default path after stop."

echo ""
echo "[SUCCESS] All phases passed."
exit 0
