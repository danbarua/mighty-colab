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

# Integration Test: a full session lifecycle driven entirely by `--json` + `jq`
#
# Where integration/repro_json_output/test.sh deliberately parses with
# `python3 -c` (modeling a caller with no `jq` available), this test is the
# opposite case: a caller that DOES have `jq`, composing mighty-colab's
# `--json` output the way real shell automation actually would --
# `mighty-colab ... --json | jq -r '.field'` piped straight into the next
# command, no application code in between. One session, one continuous
# narrative, every one of the eight `--json`-capable commands
# (new/exec/exec-async/log/status/sessions/stop, plus a `run`-equivalent
# check folded into the exec phase):
#   1. `new --json` -> capture the session name jq extracted, not the one
#      we asked for (it may differ when omitted) -- proves the caller
#      never has to duplicate name-generation logic itself.
#   2. `exec --json` a sys.exit(0) script -> status=ok via jq (the
#      flagship regression this whole design exists for).
#   3. `status --json -s <name>` -> jq reads the busy/idle field back.
#   4. `sessions --json` -> jq confirms the session is listed.
#   5. `exec-async --json` -> jq extracts pid/log_path, feeds them
#      straight into a `log --tail --json --since-offset` poll loop whose
#      offset bookkeeping is entirely jq-driven (no shell arithmetic on
#      raw text, no re-reading the whole log every poll).
#   6. `stop --json` -> a real teardown this time, status=ok.
#   7. `stop --json` again -> status=ok, reason=already_stopped (idempotent).
#   8. `status --json -s <name>` after stop -> status=error,
#      reason=session_not_found, and jq reads the exit code straight out
#      of the envelope.
#
# `--json` implies `--logtostderr`, so every invocation below sends its
# INFO-level log lines to stderr too (DEBUG-level urllib3/
# jupyter_kernel_client/websocket chatter is opt-in via `--debug` and off
# by default -- see docs/02_execution_and_interactive.md's 2026-08-12
# entry). Still, that's noise when skimming for pass/fail, so it's
# captured into one running $DEBUG_LOG instead of the console; `fail()`
# dumps it on the way out if a phase actually fails, and the cleanup trap
# deletes it (with the rest of $TMP_DIR) either way, so it never lingers
# on disk.

# Don't `set -e` so we can capture failures and still clean up explicitly.

if ! command -v jq > /dev/null; then
    echo "Error: this test requires jq (https://jqlang.org/) -- install it and re-run."
    exit 1
fi

# ---------- Auth detection (mirrors integration/repro_json_output/test.sh) --
if [ -f "$HOME/.config/colab-cli/token.json" ]; then
    AUTH_FLAGS="--auth=oauth2"
elif command -v gcloud > /dev/null && gcloud auth application-default print-access-token > /dev/null 2>&1; then
    ADC_TOKEN=$(gcloud auth application-default print-access-token 2>/dev/null)
    ADC_SCOPES=$(curl -s "https://www.googleapis.com/oauth2/v3/tokeninfo?access_token=$ADC_TOKEN" | jq -r '.scope // ""')
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
DEBUG_LOG="$TMP_DIR/debug.log"
MC="uv run mighty-colab $AUTH_FLAGS --config $SESSION_FILE"

SESSION_NAME=""
cleanup() {
    echo "[*] Cleaning up..."
    if [ -n "$SESSION_NAME" ]; then
        $MC --json stop -s "$SESSION_NAME" > /dev/null 2>&1 || true
    fi
    rm -rf "$TMP_DIR"
}
trap cleanup EXIT

# Prints a failure message, dumps whatever mighty-colab chatter has
# accumulated in $DEBUG_LOG so far (the cleanup trap deletes it right
# after -- this is the one chance to see it), then exits 1.
fail() {
    echo "[FAILURE] $1"
    if [ -s "$DEBUG_LOG" ]; then
        echo "--- debug log ($DEBUG_LOG) ---"
        cat "$DEBUG_LOG"
        echo "--- end debug log ---"
    fi
    exit 1
}

# ---------- Phase 1: new --json -> jq extracts the session name -------------
echo ""
echo "[*] Phase 1: new --json (REAL API CALL) -- jq extracts the session name"
NEW_OUT=$($MC --json new 2>>"$DEBUG_LOG")
RC=$?
echo "$NEW_OUT"
if [ $RC -ne 0 ]; then
    fail "new --json exited $RC"
fi

NEW_STATUS=$(echo "$NEW_OUT" | jq -r '.status')
SESSION_NAME=$(echo "$NEW_OUT" | jq -r '.session')
if [ "$NEW_STATUS" != "ok" ] || [ -z "$SESSION_NAME" ] || [ "$SESSION_NAME" = "null" ]; then
    fail "new --json did not report status=ok with a session name."
fi
echo "[SUCCESS] Phase 1 passed: session '$SESSION_NAME' created."

# ---------- Phase 2: exec --json a sys.exit(0) script ------------------------
echo ""
echo "[*] Phase 2: exec --json -- sys.exit(0) after real work must be status=ok"
EXEC_OUT=$(cat <<'PYEOF' | $MC --json exec -s "$SESSION_NAME" 2>>"$DEBUG_LOG"
import sys
print("computed:", 1 + 1)
sys.exit(0)
PYEOF
)
RC=$?
echo "$EXEC_OUT"
if [ $RC -ne 0 ]; then
    fail "exec --json exited $RC on a sys.exit(0) script."
fi

EXEC_STATUS=$(echo "$EXEC_OUT" | jq -r '.status')
STDOUT_TEXT=$(echo "$EXEC_OUT" | jq -r '.blocks[0].outputs[] | select(.output_type=="stream") | .text')
if [ "$EXEC_STATUS" != "ok" ]; then
    # This is the exact incident (SystemExit(0) misread as failure) this
    # design fixes -- if it regresses, that's what a status!=ok here means.
    fail "status=$EXEC_STATUS, wanted 'ok'."
fi
if ! echo "$STDOUT_TEXT" | grep -q "computed: 2"; then
    fail "jq did not find the expected stdout inside blocks[].outputs[]."
fi
echo "[SUCCESS] Phase 2 passed: status=ok, jq read the script's stdout out of blocks[]."

# ---------- Phase 3: status --json -s <name> ---------------------------------
echo ""
echo "[*] Phase 3: status --json -s $SESSION_NAME -- jq reads the busy/idle field"
STATUS_OUT=$($MC --json status -s "$SESSION_NAME" 2>>"$DEBUG_LOG")
echo "$STATUS_OUT"
SESSION_STATE=$(echo "$STATUS_OUT" | jq -r '.session.status')
if [ "$SESSION_STATE" != "IDLE" ]; then
    fail "session.status=$SESSION_STATE, wanted 'IDLE' (exec finished above)."
fi
echo "[SUCCESS] Phase 3 passed: session.status=$SESSION_STATE."

# ---------- Phase 4: sessions --json -----------------------------------------
echo ""
echo "[*] Phase 4: sessions --json -- jq confirms the session is listed"
SESSIONS_OUT=$($MC --json sessions 2>>"$DEBUG_LOG")
echo "$SESSIONS_OUT"
FOUND=$(echo "$SESSIONS_OUT" | jq -r --arg name "$SESSION_NAME" '.sessions[] | select(.name == $name) | .name')
if [ "$FOUND" != "$SESSION_NAME" ]; then
    fail "'$SESSION_NAME' not found in sessions --json's list."
fi
echo "[SUCCESS] Phase 4 passed: sessions --json lists '$SESSION_NAME'."

# ---------- Phase 5: exec-async --json + a pure-jq poll loop -----------------
echo ""
echo "[*] Phase 5: exec-async --json -- jq extracts pid/log_path, then a"
echo "    log --tail --json --since-offset poll loop driven entirely by jq"
ASYNC_SCRIPT="$TMP_DIR/async_script.py"
cat > "$ASYNC_SCRIPT" <<'PYEOF'
import time
for i in range(4):
    print(f"step {i}", flush=True)
    time.sleep(2)
print("done", flush=True)
PYEOF

SUBMIT_OUT=$($MC --json exec-async -s "$SESSION_NAME" -f "$ASYNC_SCRIPT" 2>>"$DEBUG_LOG")
echo "$SUBMIT_OUT"
SUBMIT_STATUS=$(echo "$SUBMIT_OUT" | jq -r '.status')
LOG_PATH=$(echo "$SUBMIT_OUT" | jq -r '.log_path')
if [ "$SUBMIT_STATUS" != "started" ] || [ -z "$LOG_PATH" ] || [ "$LOG_PATH" = "null" ]; then
    fail "exec-async --json did not report status=started with a log_path."
fi

OFFSET=0
FINAL_STATUS=""
for i in $(seq 1 15); do
    POLL_OUT=$($MC --json log -s "$SESSION_NAME" --tail --since-offset "$OFFSET" 2>>"$DEBUG_LOG")
    POLL_STATUS=$(echo "$POLL_OUT" | jq -r '.status')
    OFFSET=$(echo "$POLL_OUT" | jq -r '.next_offset')
    echo "[poll $i] status=$POLL_STATUS next_offset=$OFFSET"
    if [ "$POLL_STATUS" != "running" ]; then
        FINAL_STATUS="$POLL_STATUS"
        break
    fi
    sleep 2
done

if [ "$FINAL_STATUS" != "ok" ]; then
    fail "exec-async job's final status=$FINAL_STATUS, wanted 'ok'."
fi
echo "[SUCCESS] Phase 5 passed: exec-async --json -> jq-driven poll loop -> ok."

# ---------- Phase 6: stop --json (real teardown) ------------------------------
echo ""
echo "[*] Phase 6: stop --json -- a real teardown this time"
STOP_OUT=$($MC --json stop -s "$SESSION_NAME" 2>>"$DEBUG_LOG")
echo "$STOP_OUT"
STOP_STATUS=$(echo "$STOP_OUT" | jq -r '.status')
if [ "$STOP_STATUS" != "ok" ]; then
    fail "stop --json status=$STOP_STATUS, wanted 'ok'."
fi
echo "[SUCCESS] Phase 6 passed: stop --json status=ok."

# ---------- Phase 7: stop --json again -- idempotent -------------------------
echo ""
echo "[*] Phase 7: stop --json again -- must be idempotent, not an error"
STOP_AGAIN_OUT=$($MC --json stop -s "$SESSION_NAME" 2>>"$DEBUG_LOG")
echo "$STOP_AGAIN_OUT"
STOP_AGAIN_STATUS=$(echo "$STOP_AGAIN_OUT" | jq -r '.status')
STOP_AGAIN_REASON=$(echo "$STOP_AGAIN_OUT" | jq -r '.reason')
if [ "$STOP_AGAIN_STATUS" != "ok" ] || [ "$STOP_AGAIN_REASON" != "already_stopped" ]; then
    fail "second stop --json was not the idempotent ok/already_stopped case."
fi
echo "[SUCCESS] Phase 7 passed: idempotent stop -- status=ok, reason=already_stopped."

# ---------- Phase 8: status --json -s <name> after stop -----------------------
echo ""
echo "[*] Phase 8: status --json -s $SESSION_NAME after stop -- must now error"
AFTER_STOP_OUT=$($MC --json status -s "$SESSION_NAME" 2>>"$DEBUG_LOG")
AFTER_STOP_RC=$?
echo "$AFTER_STOP_OUT"
AFTER_STOP_STATUS=$(echo "$AFTER_STOP_OUT" | jq -r '.status')
AFTER_STOP_REASON=$(echo "$AFTER_STOP_OUT" | jq -r '.reason')
AFTER_STOP_EXIT_CODE=$(echo "$AFTER_STOP_OUT" | jq -r '.exit_code')
if [ "$AFTER_STOP_STATUS" != "error" ] || [ "$AFTER_STOP_REASON" != "session_not_found" ]; then
    fail "status --json after stop did not report error/session_not_found."
fi
if [ "$AFTER_STOP_RC" -eq 0 ] || [ "$AFTER_STOP_EXIT_CODE" != "1" ]; then
    fail "status --json on a gone session must exit non-zero AND carry exit_code=1 in the envelope -- got process rc=$AFTER_STOP_RC, envelope exit_code=$AFTER_STOP_EXIT_CODE."
fi
echo "[SUCCESS] Phase 8 passed: status --json correctly errors after teardown."

SESSION_NAME=""  # nothing left for the cleanup trap to do
echo ""
echo "[SUCCESS] All phases passed -- full lifecycle driven entirely by --json + jq."
exit 0
