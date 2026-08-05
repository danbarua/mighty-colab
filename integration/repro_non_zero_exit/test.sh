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

# Integration Test: `mighty-colab exec` process exit code
#
# Regression test for the bug fixed in commit 679c0b6: an uncaught exception
# on the remote kernel was printed to stderr but the CLI still exited 0,
# indistinguishable from success to anything checking $?. Verifies against a
# real Colab kernel that:
#   1. Successful code (piped via stdin) still exits 0.
#   2. Code piped via stdin that raises exits non-zero and prints the
#      traceback to stderr.
#   3. A raw `.py` script passed via `-f` (not stdin, not a notebook) that
#      raises also exits non-zero -- covers the plain `exec_command` code
#      path, since stdin/`-f .py`/`-f .ipynb` share the same fix but read
#      code differently going in.
#   4. In a multi-cell notebook, a mid-notebook error still lets later cells
#      run and the output notebook still gets saved -- only the process exit
#      code changes.

# Don't `set -e` so we can capture failures and clean up explicitly.

# ---------- Auth detection (mirrors integration/repro_run_command/test.sh) ---
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

# `exec` doesn't strip the ANSI color codes IPython's traceback formatter
# emits (only the MCP server boundary does that) -- strip them here so
# plain-text greps for e.g. "ValueError: boom" aren't split by a reset code
# landing between the name and the colon.
strip_ansi() {
    sed -E 's/\x1b\[[0-9;]*[a-zA-Z]//g'
}

# ---------- Isolated session state -------------------------------------------
TMP_DIR=$(mktemp -d)
SESSION_FILE="$TMP_DIR/sessions.json"
SESSION_NAME="repro-nonzero-exit-$(date +%s)"

cleanup() {
    echo "[*] Cleaning up..."
    uv run mighty-colab $AUTH_FLAGS --config "$SESSION_FILE" stop -s "$SESSION_NAME" 2>/dev/null || true
    rm -rf "$TMP_DIR"
}
trap cleanup EXIT

echo "[*] Starting session $SESSION_NAME..."
uv run mighty-colab $AUTH_FLAGS --config "$SESSION_FILE" new -s "$SESSION_NAME"
if [ $? -ne 0 ]; then
    echo "[FAILURE] Could not allocate a session."
    exit 1
fi

# ---------- Phase 1: successful code still exits 0 ---------------------------
echo ""
echo "[*] Phase 1: successful code exits 0"
OUTPUT=$(echo 'print("ok")' | uv run mighty-colab $AUTH_FLAGS --config "$SESSION_FILE" exec -s "$SESSION_NAME" 2>&1)
RC=$?
echo "$OUTPUT"

if [ $RC -ne 0 ]; then
    echo "[FAILURE] Successful code exited $RC, expected 0."
    exit 1
fi
if ! echo "$OUTPUT" | grep -q "^ok$"; then
    echo "[FAILURE] Expected stdout 'ok' not found."
    exit 1
fi
echo "[SUCCESS] Phase 1 passed."

# ---------- Phase 2: raising code exits non-zero ------------------------------
echo ""
echo "[*] Phase 2: code that raises exits non-zero"
OUTPUT=$(echo 'raise ValueError("boom")' | uv run mighty-colab $AUTH_FLAGS --config "$SESSION_FILE" exec -s "$SESSION_NAME" 2>&1)
RC=$?
echo "$OUTPUT"
CLEAN_OUTPUT=$(echo "$OUTPUT" | strip_ansi)

if [ $RC -eq 0 ]; then
    echo "[FAILURE] Remote exception did not change the exit code (still 0)."
    exit 1
fi
if ! echo "$CLEAN_OUTPUT" | grep -q "ValueError: boom"; then
    echo "[FAILURE] Expected traceback text 'ValueError: boom' not found in output."
    exit 1
fi
echo "[SUCCESS] Phase 2 passed (exit $RC, traceback present)."

# ---------- Phase 3: raw `.py` file (via -f) that raises exits non-zero ------
echo ""
echo "[*] Phase 3: raw .py script (via -f) that raises exits non-zero"

PY_SCRIPT_PATH="$TMP_DIR/repro_raises.py"
cat > "$PY_SCRIPT_PATH" <<'PYEOF'
print("before raise")
raise RuntimeError("script boom")
PYEOF

OUTPUT=$(uv run mighty-colab $AUTH_FLAGS --config "$SESSION_FILE" exec -s "$SESSION_NAME" -f "$PY_SCRIPT_PATH" 2>&1)
RC=$?
echo "$OUTPUT"
CLEAN_OUTPUT=$(echo "$OUTPUT" | strip_ansi)

if [ $RC -eq 0 ]; then
    echo "[FAILURE] Raw .py script via -f did not change the exit code (still 0)."
    exit 1
fi
if ! echo "$CLEAN_OUTPUT" | grep -q "before raise"; then
    echo "[FAILURE] Expected stdout 'before raise' not found."
    exit 1
fi
if ! echo "$CLEAN_OUTPUT" | grep -q "RuntimeError: script boom"; then
    echo "[FAILURE] Expected traceback text 'RuntimeError: script boom' not found in output."
    exit 1
fi
echo "[SUCCESS] Phase 3 passed (exit $RC, script ran and traceback present)."

# ---------- Phase 4: mid-notebook error still runs later cells and saves -----
echo ""
echo "[*] Phase 4: notebook with a failing cell still runs remaining cells,"
echo "    saves the output notebook, and the process exits non-zero"

NB_PATH="$TMP_DIR/repro.ipynb"
python3 - "$NB_PATH" <<'PYEOF'
import json
import sys

nb = {
    "cells": [
        {
            "cell_type": "code",
            "execution_count": None,
            "id": "cell-ok-1",
            "metadata": {},
            "outputs": [],
            "source": "print('before')",
        },
        {
            "cell_type": "code",
            "execution_count": None,
            "id": "cell-raise",
            "metadata": {},
            "outputs": [],
            "source": "raise ValueError('boom')",
        },
        {
            "cell_type": "code",
            "execution_count": None,
            "id": "cell-ok-2",
            "metadata": {},
            "outputs": [],
            "source": "print('after')",
        },
    ],
    "metadata": {},
    "nbformat": 4,
    "nbformat_minor": 5,
}
with open(sys.argv[1], "w", encoding="utf-8") as f:
    json.dump(nb, f)
PYEOF

OUTPUT=$(uv run mighty-colab $AUTH_FLAGS --config "$SESSION_FILE" exec -s "$SESSION_NAME" -f "$NB_PATH" 2>&1)
RC=$?
echo "$OUTPUT"

OUTPUT_NB="${NB_PATH%.ipynb}_output.ipynb"

if [ $RC -eq 0 ]; then
    echo "[FAILURE] Notebook with a failing cell did not change the exit code (still 0)."
    exit 1
fi
if ! echo "$OUTPUT" | grep -q "Executing cell 3/3"; then
    echo "[FAILURE] Cell after the failing one did not run (expected 'Executing cell 3/3')."
    exit 1
fi
if [ ! -f "$OUTPUT_NB" ]; then
    echo "[FAILURE] Output notebook was not saved despite the mid-run error."
    exit 1
fi
if ! python3 -c "
import json

def text_of(o):
    # nbformat serializes multi-line 'text' as a list of lines, not a str.
    t = o.get('text', '')
    return ''.join(t) if isinstance(t, list) else t

nb = json.load(open('$OUTPUT_NB'))
cells = nb['cells']
assert any(o.get('name') == 'stdout' and 'before' in text_of(o) for c in cells for o in c.get('outputs', [])), 'missing before output'
assert any(o.get('output_type') == 'error' and o.get('ename') == 'ValueError' for c in cells for o in c.get('outputs', [])), 'missing error output'
assert any(o.get('name') == 'stdout' and 'after' in text_of(o) for c in cells for o in c.get('outputs', [])), 'missing after output'
"; then
    echo "[FAILURE] Output notebook is missing expected cell outputs."
    exit 1
fi
echo "[SUCCESS] Phase 4 passed (exit $RC, all 3 cells ran, output notebook saved)."

echo ""
echo "[SUCCESS] All phases passed."
exit 0
