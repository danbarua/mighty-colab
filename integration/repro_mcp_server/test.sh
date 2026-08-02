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

# Integration Test: `mighty-colab mcp`
#
# Verifies the MCP (Model Context Protocol) server end-to-end against a live
# Colab backend, driven through the `mcp` SDK's own stdio client rather than
# the CLI directly -- exercising exactly the path a real MCP client (Claude
# Desktop, etc.) would take. See docs/07_mcp_server.md for the design this
# pins down:
#   1. `list_tools()` respects EXCLUDED_COMMANDS / `hidden=True` (no
#      ssh/repl/console/edit/drivemount/mcp/help; ordinary commands present).
#   2. `call_tool("new", ...)` allocates a real CPU VM.
#   3. `call_tool("exec", ...)` runs a script file on it and returns output.
#   4. `call_tool("status", ...)` reports the session.
#   5. `call_tool("stop", ...)` tears it down; `call_tool("sessions", {})`
#      confirms no orphan remains (cross-checked with the CLI directly).
#   6. `call_tool("adopt", {})` with neither ENDPOINT nor --orphanage comes
#      back as `is_error: True` with the actual validation message, not an
#      opaque bare exit code.

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

# ---------- Isolated session state -------------------------------------------
TMP_DIR=$(mktemp -d)
SESSION_FILE="$TMP_DIR/sessions.json"
SCRIPT_PATH="$TMP_DIR/script.py"
DRIVER_PATH="$TMP_DIR/mcp_driver.py"
SESSION_NAME="repro-mcp-$(date +%s)"

cleanup() {
    echo "[*] Cleaning up..."
    uv run mighty-colab $AUTH_FLAGS --config "$SESSION_FILE" stop -s "$SESSION_NAME" 2>/dev/null || true
    rm -rf "$TMP_DIR"
}
trap cleanup EXIT

cat > "$SCRIPT_PATH" <<'PYEOF'
print("mcp_exec_marker_ok")
PYEOF

# ---------- MCP driver: speaks the real client protocol over stdio -----------
# Spawns `mighty-colab mcp` as a subprocess exactly like a real MCP client
# would (e.g. Claude Desktop), rather than calling into the CLI directly.
cat > "$DRIVER_PATH" <<'PYEOF'
import asyncio
import os
import sys

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

AUTH_FLAGS = os.environ["MCP_AUTH_FLAGS"]
CONFIG_PATH = os.environ["MCP_CONFIG_PATH"]
SESSION_NAME = os.environ["MCP_SESSION_NAME"]
SCRIPT_PATH = os.environ["MCP_SCRIPT_PATH"]

EXCLUDED_COMMANDS = {"ssh", "repl", "console", "edit", "drivemount", "mcp", "help"}
EXPECTED_COMMANDS = {"new", "status", "stop", "sessions", "adopt", "exec"}


def text_of(result) -> str:
    return "\n".join(c.text for c in result.content if hasattr(c, "text"))


async def main() -> int:
    params = StdioServerParameters(
        command="mighty-colab",
        args=[AUTH_FLAGS, "--config", CONFIG_PATH, "mcp"],
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            print("[*] Phase 1: list_tools()")
            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            leaked = names & EXCLUDED_COMMANDS
            missing = EXPECTED_COMMANDS - names
            if leaked:
                print(f"[FAILURE] excluded commands leaked into tool list: {sorted(leaked)}")
                return 1
            if missing:
                print(f"[FAILURE] expected tools missing from list_tools(): {sorted(missing)}")
                return 1
            print(f"[SUCCESS] Phase 1 passed: {len(names)} tools, exclusions respected.")

            print(f"\n[*] Phase 2: call_tool('new', session={SESSION_NAME!r}) -- allocates a real CPU VM")
            result = await session.call_tool("new", {"session": SESSION_NAME})
            print(text_of(result))
            if result.is_error:
                print("[FAILURE] 'new' tool call reported an error")
                return 1
            print("[SUCCESS] Phase 2 passed.")

            print(f"\n[*] Phase 3: call_tool('exec', session={SESSION_NAME!r}, file={SCRIPT_PATH!r})")
            result = await session.call_tool("exec", {"session": SESSION_NAME, "file": SCRIPT_PATH})
            text = text_of(result)
            print(text)
            if result.is_error or "mcp_exec_marker_ok" not in text:
                print("[FAILURE] 'exec' tool call did not run the script as expected")
                return 1
            print("[SUCCESS] Phase 3 passed.")

            print(f"\n[*] Phase 4: call_tool('status', session={SESSION_NAME!r})")
            result = await session.call_tool("status", {"session": SESSION_NAME})
            text = text_of(result)
            print(text)
            if result.is_error or SESSION_NAME not in text:
                print("[FAILURE] 'status' tool call did not report the session")
                return 1
            print("[SUCCESS] Phase 4 passed.")

            print(f"\n[*] Phase 5: call_tool('stop', session={SESSION_NAME!r})")
            result = await session.call_tool("stop", {"session": SESSION_NAME})
            print(text_of(result))
            if result.is_error:
                print("[FAILURE] 'stop' tool call reported an error")
                return 1

            result = await session.call_tool("sessions", {})
            text = text_of(result)
            print(text)
            if "No active sessions found on server." not in text:
                print("[FAILURE] orphan VM suspected after 'stop' via MCP")
                return 1
            print("[SUCCESS] Phase 5 passed: stopped via MCP, no orphan reported.")

            print("\n[*] Phase 6: call_tool('adopt', {}) -- expect a validation error, not a crash")
            result = await session.call_tool("adopt", {})
            text = text_of(result)
            print(text)
            if not result.is_error or "ENDPOINT" not in text:
                print("[FAILURE] 'adopt' with no ENDPOINT/--orphanage should report the usage error")
                return 1
            print("[SUCCESS] Phase 6 passed: validation error surfaced over MCP, not swallowed.")

    print("\n[SUCCESS] All MCP phases passed.")
    return 0


sys.exit(asyncio.run(main()))
PYEOF

echo "[*] Driving mighty-colab mcp via the real MCP client protocol (mcp.client.stdio)"
MCP_AUTH_FLAGS="$AUTH_FLAGS" MCP_CONFIG_PATH="$SESSION_FILE" MCP_SESSION_NAME="$SESSION_NAME" MCP_SCRIPT_PATH="$SCRIPT_PATH" \
    uv run python3 "$DRIVER_PATH"
RC=$?
if [ $RC -ne 0 ]; then
    echo "[FAILURE] MCP driver exited $RC"
    exit 1
fi

# Belt-and-braces: confirm via the CLI directly (independent of the MCP
# dispatch path under test) that 'stop' actually released the VM server-side.
SESSIONS_OUT=$(uv run mighty-colab $AUTH_FLAGS --config "$SESSION_FILE" sessions 2>&1)
echo "$SESSIONS_OUT"
if ! echo "$SESSIONS_OUT" | grep -q "No active sessions found on server."; then
    echo "[FAILURE] CLI-side check disagrees: server still reports active sessions."
    exit 1
fi

echo "[SUCCESS] integration/repro_mcp_server/test.sh passed."
exit 0
