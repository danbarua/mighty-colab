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

# Integration Test: `colab exec-async` + `colab log -f`
#
# Verifies the background-execution flow added alongside `colab exec`
# (which blocks the caller for the whole run):
#   1. `exec-async` returns near-instantly even though the submitted script
#      takes several seconds -- it does not block on the kernel reply.
#   2. `log -s <session> -f` streams the worker's stdout live, in order, as
#      the remote script prints it (not just after the run completes).
#   3. A second `exec-async` on the SAME session while the first is still
#      running is refused (exit 1) -- the "already running" pid_alive guard.
#   4. Once the first run has finished, `exec-async` on that session is
#      allowed again (stale pid must not block a restart).
#
# Does not exercise the cross-session log-path collision guard
# (state.store.list() scan in exec_async) -- that's covered by mocked unit
# tests in tests/test_exec_async.py since it requires fabricating colliding
# local state, not live backend behavior.

# Don't `set -e` so we can capture failures and still clean up explicitly.

# ---------- Auth detection (mirrors integration/repro_keep_alive/test.sh) ----
if [ -f "$HOME/.config/colab-cli/token.json" ]; then
    AUTH_FLAGS="--auth=oauth2"
elif command -v gcloud > /dev/null && gcloud auth application-default print-access-token > /dev/null 2>&1; then
    ADC_TOKEN=$(gcloud auth application-default print-access-token 2>/dev/null)
    ADC_SCOPES=$(curl -s "https://www.googleapis.com/oauth2/v3/tokeninfo?access_token=$ADC_TOKEN" | python3 -c "import json,sys; print(json.load(sys.stdin).get('scope',''))" 2>/dev/null)
    if echo "$ADC_SCOPES" | grep -q "colaboratory" && echo "$ADC_SCOPES" | grep -q "userinfo.email"; then
        AUTH_FLAGS="--auth=adc"
    else
        echo "Error: ADC token lacks the required scopes (colaboratory + userinfo.email)."
        echo "Re-issue ADC creds with all required scopes:"
        echo "  gcloud auth application-default login \\"
        echo "      --scopes=openid,\\"
        echo "              https://www.googleapis.com/auth/cloud-platform,\\"
        echo "              https://www.googleapis.com/auth/userinfo.email,\\"
        echo "              https://www.googleapis.com/auth/colaboratory"
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
SCRIPT_PATH="$TMP_DIR/slow_train.py"
SESSION_NAME="repro-exec-async-$(date +%s)"

cleanup() {
    echo "[*] Cleaning up..."
    uv run mighty-colab $AUTH_FLAGS --config "$SESSION_FILE" stop -s "$SESSION_NAME" 2>/dev/null || true
    rm -rf "$TMP_DIR"
}
trap cleanup EXIT

cat > "$SCRIPT_PATH" <<'PYEOF'
import sys
import time

for i in range(5):
    print(f"step {i}", flush=True)
    sys.stdout.flush()
    time.sleep(2)
print("done", flush=True)
PYEOF

echo "[*] Creating session '$SESSION_NAME' (REAL API CALL)..."
uv run mighty-colab $AUTH_FLAGS --config "$SESSION_FILE" new -s "$SESSION_NAME"

# ---------- Phase 1: exec-async returns immediately --------------------------
echo ""
echo "[*] Phase 1: exec-async must return well before the ~10s script finishes"
START=$(date +%s)
OUTPUT=$(uv run mighty-colab $AUTH_FLAGS --config "$SESSION_FILE" exec-async -s "$SESSION_NAME" -f "$SCRIPT_PATH" 2>&1)
RC=$?
END=$(date +%s)
ELAPSED=$((END - START))
echo "$OUTPUT"

if [ $RC -ne 0 ]; then
    echo "[FAILURE] exec-async exited $RC"
    exit 1
fi
if ! echo "$OUTPUT" | grep -q "Started background exec"; then
    echo "[FAILURE] exec-async did not report starting a background job."
    exit 1
fi
if [ "$ELAPSED" -gt 5 ]; then
    echo "[FAILURE] exec-async took ${ELAPSED}s -- it should return almost"
    echo "          instantly regardless of the submitted script's runtime."
    exit 1
fi
echo "[SUCCESS] Phase 1 passed: exec-async returned in ${ELAPSED}s (non-blocking)."

# ---------- Phase 2: a second exec-async while the first runs is refused ----
echo ""
echo "[*] Phase 2: a concurrent exec-async on the same session must be refused"
OUTPUT=$(uv run mighty-colab $AUTH_FLAGS --config "$SESSION_FILE" exec-async -s "$SESSION_NAME" -f "$SCRIPT_PATH" 2>&1)
RC=$?
echo "$OUTPUT"

if [ $RC -eq 0 ]; then
    echo "[FAILURE] Second concurrent exec-async was NOT refused (exit 0)."
    exit 1
fi
if ! echo "$OUTPUT" | grep -q "already has a background exec running"; then
    echo "[FAILURE] Refusal message missing/changed -- update this assertion"
    echo "          if the wording in exec_async() changed intentionally."
    exit 1
fi
echo "[SUCCESS] Phase 2 passed: concurrent exec-async correctly refused."

# ---------- Phase 3: log -f streams the first run's output live -------------
echo ""
echo "[*] Phase 3: log -s $SESSION_NAME -f must stream all 6 lines in order"
LOG_OUTPUT=$(timeout 30 uv run mighty-colab $AUTH_FLAGS --config "$SESSION_FILE" log -s "$SESSION_NAME" -f 2>&1)
RC=$?
echo "$LOG_OUTPUT"

if [ $RC -ne 0 ]; then
    echo "[FAILURE] log -f exited $RC (timed out or errored before the worker finished)."
    exit 1
fi
EXPECTED_ORDER="step 0
step 1
step 2
step 3
step 4
done"
if [ "$(echo "$LOG_OUTPUT" | grep -E '^(step [0-4]|done)$')" != "$EXPECTED_ORDER" ]; then
    echo "[FAILURE] log -f did not stream the expected lines in order."
    echo "  Wanted:"
    echo "$EXPECTED_ORDER"
    exit 1
fi
echo "[SUCCESS] Phase 3 passed: log -f streamed the run live, in order."

# ---------- Phase 4: exec-async is allowed again once the run has finished --
echo ""
echo "[*] Phase 4: a fresh exec-async after completion must be allowed"
OUTPUT=$(uv run mighty-colab $AUTH_FLAGS --config "$SESSION_FILE" exec-async -s "$SESSION_NAME" -f "$SCRIPT_PATH" 2>&1)
RC=$?
echo "$OUTPUT"

if [ $RC -ne 0 ]; then
    echo "[FAILURE] exec-async after completion was refused (a stale pid should"
    echo "          never block a restart)."
    exit 1
fi
echo "[SUCCESS] Phase 4 passed: restart after completion allowed."

# Let the second run finish before `stop` (in the cleanup trap) tears down
# the kernel out from under it.
sleep 12

echo ""
echo "[SUCCESS] All phases passed."
exit 0
