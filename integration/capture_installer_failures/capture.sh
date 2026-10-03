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

# Not a regression test: captures how uv and pip fail on a real Colab VM,
# for the install-failure classifier's fixtures. Allocates one CPU VM, runs
# probe.py there, downloads the result to
# tests/fixtures/installer_failures.json, and releases the VM.

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)
TMP_DIR=$(cd "$(mktemp -d)" && pwd -P)
SESSION_FILE="$TMP_DIR/sessions.json"
SESSION="installer-capture-$$"
OUT="$REPO_ROOT/tests/fixtures/installer_failures.json"
CREATED=""

mc() {
    uv run mighty-colab --auth=adc --config "$SESSION_FILE" "$@"
}

cleanup() {
    if [ -n "$CREATED" ]; then
        mc stop -s "$SESSION" >/dev/null 2>&1 || true
    fi
    rm -rf "$TMP_DIR"
}
trap cleanup EXIT

cd "$REPO_ROOT"
# The probe runs for several minutes (pip retries network failures). It is
# started as a detached process, the way `job` launches its runner, so a
# dropped kernel connection does not kill it; its result is then polled.
BOOT="$TMP_DIR/boot.py"
uv run python - "$SCRIPT_DIR/probe.py" "$BOOT" <<'PY'
import sys

source = open(sys.argv[1]).read()
open(sys.argv[2], "w").write(
    "import subprocess, sys\n"
    f"open('/content/probe.py', 'w').write({source!r})\n"
    "subprocess.Popen([sys.executable, '/content/probe.py'],"
    " stdout=open('/content/probe.log', 'w'), stderr=subprocess.STDOUT,"
    " start_new_session=True)\n"
    "print('probe started')\n"
)
PY

mc new -s "$SESSION"
CREATED=1
mc exec -s "$SESSION" -f "$BOOT"
mkdir -p "$(dirname "$OUT")"
for _ in $(seq 1 120); do
    if mc download -s "$SESSION" /content/installer_failures.json "$OUT" >/dev/null 2>&1; then
        break
    fi
    sleep 10
done
mc download -s "$SESSION" /content/probe.log "$TMP_DIR/probe.log" >/dev/null 2>&1 || true
tail -n 30 "$TMP_DIR/probe.log" 2>/dev/null || true
[ -f "$OUT" ] || { echo "the probe wrote no result within 20 minutes" >&2; exit 1; }
mc stop -s "$SESSION"
CREATED=""
echo "[SUCCESS] wrote $OUT"
