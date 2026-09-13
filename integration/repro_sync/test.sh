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

# Live integration test for gzip-first and Git-aware `mighty-colab sync`.
# Do not use `set -e`: every failure path must still release the assignment.

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

TMP_DIR=$(mktemp -d)
SESSION_FILE="$TMP_DIR/sessions.json"
SESSION_NAME="repro-sync-$(date +%s)"
DEFAULT_SOURCE="$TMP_DIR/default-source"
REPO="$TMP_DIR/repo"
VERIFY_SCRIPT="$TMP_DIR/verify.py"

cleanup() {
    echo "[*] Cleaning up..."
    uv run mighty-colab $AUTH_FLAGS --config "$SESSION_FILE" stop -s "$SESSION_NAME" 2>/dev/null || true
    rm -rf "$TMP_DIR"
}
trap cleanup EXIT

mkdir -p "$DEFAULT_SOURCE" "$REPO/experiment"
printf 'stale\n' > "$DEFAULT_SOURCE/stale.txt"
printf 'value = 1\n' > "$REPO/experiment/modified.py"
printf 'delete = True\n' > "$REPO/experiment/deleted.py"
printf 'staged = 1\n' > "$REPO/experiment/staged.py"
printf 'delete = True\n' > "$REPO/experiment/staged_deleted.py"
printf 'both = 1\n' > "$REPO/experiment/both.py"
python3 -c "import os; from pathlib import Path; Path('$REPO/outside.bin').write_bytes(os.urandom(2000000))"
git -C "$REPO" init -q
git -C "$REPO" config user.email sync-test@example.com
git -C "$REPO" config user.name "Sync Test"
git -C "$REPO" add .
git -C "$REPO" commit -qm fixture
HEAD_COMMIT=$(git -C "$REPO" rev-parse HEAD)
OUTSIDE_OID=$(git -C "$REPO" rev-parse HEAD:outside.bin)
printf 'value = 2\n' > "$REPO/experiment/modified.py"
rm "$REPO/experiment/deleted.py"
printf 'untracked = True\n' > "$REPO/experiment/untracked.py"
printf 'ignored = True\n' > "$REPO/experiment/ignored.py"
printf 'experiment/ignored.py\n' > "$REPO/.gitignore"
printf 'staged = 2\n' > "$REPO/experiment/staged.py"
rm "$REPO/experiment/staged_deleted.py"
printf 'added = True\n' > "$REPO/experiment/staged_added.py"
printf 'both = 2\n' > "$REPO/experiment/both.py"
git -C "$REPO" add experiment/staged.py experiment/staged_deleted.py experiment/staged_added.py experiment/both.py
printf 'both = 3\n' > "$REPO/experiment/both.py"

OUTPUT=$(uv run mighty-colab $AUTH_FLAGS --config "$SESSION_FILE" new -s "$SESSION_NAME" 2>&1)
RC=$?
echo "$OUTPUT"
if [ $RC -ne 0 ]; then
    echo "[FAILURE] new exited $RC"
    exit 1
fi
ENDPOINT=$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))[sys.argv[2]]["endpoint"])' "$SESSION_FILE" "$SESSION_NAME")
if [ -z "$ENDPOINT" ]; then
    echo "[FAILURE] new did not persist an endpoint"
    exit 1
fi

OUTPUT=$(uv run mighty-colab $AUTH_FLAGS --config "$SESSION_FILE" sync "$DEFAULT_SOURCE" content/sync-default -s "$SESSION_NAME" 2>&1)
RC=$?
echo "$OUTPUT"
if [ $RC -ne 0 ]; then
    echo "[FAILURE] first default sync exited $RC"
    exit 1
fi
rm "$DEFAULT_SOURCE/stale.txt"
printf 'fresh\n' > "$DEFAULT_SOURCE/fresh.txt"
OUTPUT=$(uv run mighty-colab $AUTH_FLAGS --config "$SESSION_FILE" sync "$DEFAULT_SOURCE" content/sync-default -s "$SESSION_NAME" 2>&1)
RC=$?
echo "$OUTPUT"
if [ $RC -ne 0 ]; then
    echo "[FAILURE] replacement default sync exited $RC"
    exit 1
fi

OUTPUT=$(uv run mighty-colab $AUTH_FLAGS --config "$SESSION_FILE" sync --git-aware "$REPO/experiment" content/sync-git -s "$SESSION_NAME" 2>&1)
RC=$?
echo "$OUTPUT"
if [ $RC -ne 0 ]; then
    echo "[FAILURE] Git-aware sync exited $RC"
    exit 1
fi

cat > "$VERIFY_SCRIPT" <<PYEOF
import os
import subprocess

assert os.path.exists('/content/sync-default/fresh.txt')
assert not os.path.exists('/content/sync-default/stale.txt')
repo = '/content/sync-git'
def git(*args):
    return subprocess.run(
        ['git', '-C', repo, *args], check=True, capture_output=True, text=True
    ).stdout.rstrip('\\n')
assert git('rev-parse', 'HEAD') == '$HEAD_COMMIT'
assert git('remote', 'get-url', 'origin') == 'offline://mighty-colab/sync'
assert set(git('status', '--porcelain', '--', 'experiment').splitlines()) == {
    'MM experiment/both.py',
    ' M experiment/modified.py',
    ' D experiment/deleted.py',
    'M  experiment/staged.py',
    'D  experiment/staged_deleted.py',
    'A  experiment/staged_added.py',
    '?? experiment/untracked.py',
}
assert open(repo + '/experiment/modified.py').read() == 'value = 2\\n'
assert open(repo + '/experiment/untracked.py').read() == 'untracked = True\\n'
assert not os.path.exists(repo + '/experiment/ignored.py')
assert not os.path.exists(repo + '/outside.bin')
outside_probe = subprocess.run(
    ['git', '-C', repo, 'cat-file', '-e', '$OUTSIDE_OID'], capture_output=True
)
assert outside_probe.returncode != 0
print('SYNC_E2E_OK')
PYEOF

OUTPUT=$(uv run mighty-colab $AUTH_FLAGS --config "$SESSION_FILE" exec -s "$SESSION_NAME" -f "$VERIFY_SCRIPT" 2>&1)
RC=$?
echo "$OUTPUT"
if [ $RC -ne 0 ] || ! echo "$OUTPUT" | grep -q "SYNC_E2E_OK"; then
    echo "[FAILURE] remote sync verification failed"
    exit 1
fi

OUTPUT=$(uv run mighty-colab $AUTH_FLAGS --config "$SESSION_FILE" stop -s "$SESSION_NAME" 2>&1)
RC=$?
echo "$OUTPUT"
if [ $RC -ne 0 ]; then
    echo "[FAILURE] stop exited $RC"
    exit 1
fi
SESSIONS_OUT=$(uv run mighty-colab $AUTH_FLAGS --json --config "$SESSION_FILE" sessions)
RC=$?
echo "$SESSIONS_OUT"
if [ $RC -ne 0 ]; then
    echo "[FAILURE] Final sessions check exited $RC"
    exit 1
fi
if ! python3 -c \
    'import json, sys; data = json.load(sys.stdin); name, endpoint = sys.argv[1:];
leftover = [item for item in data["sessions"] if item["name"] == name or item["endpoint"] == endpoint];
raise SystemExit(0 if not leftover else "test assignment still listed")' \
    "$SESSION_NAME" "$ENDPOINT" <<< "$SESSIONS_OUT"; then
    echo "[FAILURE] Final sessions check did not confirm assignment teardown"
    exit 1
fi

echo "[SUCCESS] gzip replacement and Git-aware sparse sync passed live."
exit 0
