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

# Integration Test: `exec -f` / `run` set __file__, __name__, sys.argv
#
# Regression test for a real incident (docs/AGENT_USABILITY_LEARNINGS.md,
# "The driver that crashed on a billing A100 because there was no file"):
# a script using `os.path.dirname(os.path.abspath(__file__))` at module
# scope crashed with NameError under `exec -f`, because the file's *text*
# is transmitted into an existing kernel -- it's never run as a script, so
# __file__ was never defined. `colab run` has the same gap for __file__
# (it already set sys.argv/__name__, just not __file__).
#
# This verifies both commands now give module-scope code an honest,
# synthetic __file__ (a `<mighty-colab-exec:...>` sentinel, not a
# plausible-but-wrong real path) instead of crashing, plus sys.argv and
# __name__ == "__main__".

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
SCRIPT_PATH="$TMP_DIR/driver.py"
SESSION_NAME="repro-dunder-file-$(date +%s)"

cleanup() {
    echo "[*] Cleaning up..."
    uv run mighty-colab $AUTH_FLAGS --config "$SESSION_FILE" stop -s "$SESSION_NAME" 2>/dev/null || true
    rm -rf "$TMP_DIR"
}
trap cleanup EXIT

cat > "$SCRIPT_PATH" <<'PYEOF'
import os, sys
# This exact pattern crashed a real billing A100 run before __file__ was set:
here = os.path.dirname(os.path.abspath(__file__))
print(f"HERE={here}")
print(f"FILE={__file__}")
print(f"NAME={__name__}")
print(f"ARGV={sys.argv}")
PYEOF

echo "[*] Creating session '$SESSION_NAME' (REAL API CALL)..."
uv run mighty-colab $AUTH_FLAGS --config "$SESSION_FILE" new -s "$SESSION_NAME"

# ---------- Phase 1: exec -f must not crash with NameError -------------------
echo ""
echo "[*] Phase 1: exec -f must not raise NameError: name '__file__' is not defined"
OUTPUT=$(uv run mighty-colab $AUTH_FLAGS --config "$SESSION_FILE" exec -s "$SESSION_NAME" -f "$SCRIPT_PATH" 2>&1)
RC=$?
echo "$OUTPUT"

if [ $RC -ne 0 ]; then
    echo "[FAILURE] exec -f exited $RC"
    exit 1
fi
if echo "$OUTPUT" | grep -q "NameError"; then
    echo "[FAILURE] __file__ is still undefined under exec -f."
    exit 1
fi
if ! echo "$OUTPUT" | grep -q "FILE=<mighty-colab-exec:driver.py>"; then
    echo "[FAILURE] __file__ was not the expected synthetic sentinel."
    exit 1
fi
if ! echo "$OUTPUT" | grep -q "NAME=__main__"; then
    echo "[FAILURE] __name__ was not '__main__'."
    exit 1
fi
if ! echo "$OUTPUT" | grep -q "ARGV=\['driver.py'\]"; then
    echo "[FAILURE] sys.argv was not ['driver.py']."
    exit 1
fi
echo "[SUCCESS] Phase 1 passed: exec -f sets __file__/__name__/sys.argv honestly."

# ---------- Phase 2: colab run must set __file__ too (it already had the rest)
echo ""
echo "[*] Phase 2: colab run must also set __file__ (sys.argv/__name__ already worked)"
RUN_OUTPUT=$(uv run mighty-colab $AUTH_FLAGS --config "$SESSION_FILE" run "$SCRIPT_PATH" 2>&1)
RC=$?
echo "$RUN_OUTPUT"

if [ $RC -ne 0 ]; then
    echo "[FAILURE] run exited $RC"
    exit 1
fi
if echo "$RUN_OUTPUT" | grep -q "NameError"; then
    echo "[FAILURE] __file__ is still undefined under run."
    exit 1
fi
if ! echo "$RUN_OUTPUT" | grep -q "FILE=<mighty-colab-exec:driver.py>"; then
    echo "[FAILURE] run's __file__ was not the expected synthetic sentinel."
    exit 1
fi
echo "[SUCCESS] Phase 2 passed: run also sets __file__ honestly."

echo ""
echo "[SUCCESS] All phases passed."
exit 0
