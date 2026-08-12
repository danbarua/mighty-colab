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
#
# Integration Test: `-c/--client-oauth-config` combined with `--json` --
# happy and unhappy paths.
#
# `auth.py`'s credential-loading code predates `--json` entirely and had
# zero awareness of it: a malformed `-c` config file raised an uncaught
# `json.JSONDecodeError`, and a missing config with no bundled fallback
# raised an uncaught `FileNotFoundError` -- both would have printed a raw
# Python traceback instead of a parseable envelope, breaking a `--json |
# jq` pipeline. This test proves the fix end-to-end against the real CLI
# entry point (`cli.py:main()`'s top-level catch-all, since Click's own
# `Command.main()` only catches `ClickException`/`Abort`/EPIPE -- verified
# against its source -- so nothing upstream of that catch-all would have
# saved us).
#
# Unhappy path is deterministic regardless of machine auth state: a
# malformed config fails at `json.load()`, before any cached token or ADC
# credential is ever consulted. Happy path needs a config that resolves to
# real credentials without blocking on the interactive OAuth prompt's
# `input()` call, so it's skipped (not failed) when no cached token exists
# at `~/.config/colab-cli/token.json` -- mirroring `repro_bundled_oauth/
# test.sh`'s own care around not hanging in CI.

set +e  # capture failures ourselves so cleanup always runs

TMP_DIR=$(mktemp -d)
cleanup() { rm -rf "$TMP_DIR"; }
trap cleanup EXIT

FAILED=0
fail() {
    echo "[FAILURE] $1"
    FAILED=1
}

# ---------------------------------------------------------------------------
# Unhappy path: malformed OAuth config file + --json
# ---------------------------------------------------------------------------
echo "[*] Unhappy path: malformed -c config + --json"

BAD_CONFIG="$TMP_DIR/malformed_config.json"
echo 'not valid json{{{' > "$BAD_CONFIG"

OUTPUT=$(uv run mighty-colab --json -c "$BAD_CONFIG" sessions 2>/dev/null)
EXIT_CODE=$?

if [ "$EXIT_CODE" -ne 1 ]; then
    fail "expected exit code 1 for malformed config, got $EXIT_CODE"
fi

if ! echo "$OUTPUT" | python3 -c "import json,sys; json.loads(sys.stdin.read())" 2>/dev/null; then
    fail "stdout was not valid JSON for a malformed -c config: $OUTPUT"
else
    STATUS=$(echo "$OUTPUT" | python3 -c "import json,sys; print(json.loads(sys.stdin.read())['status'])")
    REASON=$(echo "$OUTPUT" | python3 -c "import json,sys; print(json.loads(sys.stdin.read())['reason'])")
    HINT=$(echo "$OUTPUT" | python3 -c "import json,sys; print(json.loads(sys.stdin.read()).get('hint', ''))")
    [ "$STATUS" = "error" ] || fail "expected status=error, got status=$STATUS"
    [ "$REASON" = "auth_config_invalid" ] || fail "expected reason=auth_config_invalid, got reason=$REASON"
    [ -n "$HINT" ] || fail "expected a non-empty hint field"
fi

# Same failure, without --json: no traceback, no stray '{' on stdout.
PLAIN_OUTPUT=$(uv run mighty-colab -c "$BAD_CONFIG" sessions 2>&1)
PLAIN_EXIT=$?
[ "$PLAIN_EXIT" -eq 1 ] || fail "expected exit code 1 (plain text), got $PLAIN_EXIT"
echo "$PLAIN_OUTPUT" | grep -q "Traceback (most recent call last)" && \
    fail "a raw Python traceback leaked to the user: $PLAIN_OUTPUT"
echo "$PLAIN_OUTPUT" | grep -q "\[colab\] Error:" || \
    fail "expected a '[colab] Error: ...' line, got: $PLAIN_OUTPUT"

if [ "$FAILED" -eq 0 ]; then
    echo "[OK] Malformed -c config produces a clean error, json and plain text alike."
fi

# ---------------------------------------------------------------------------
# Happy path: valid OAuth config file + --json
# ---------------------------------------------------------------------------
echo "[*] Happy path: valid -c config + --json"

TOKEN_PATH="$HOME/.config/colab-cli/token.json"
if [ ! -f "$TOKEN_PATH" ]; then
    echo "[SKIP] No cached OAuth token at $TOKEN_PATH -- the happy path needs" \
        "previously-completed interactive auth so it doesn't block on" \
        "input(). Run 'mighty-colab sessions' once interactively, then re-run" \
        "this test."
else
    GOOD_CONFIG="$TMP_DIR/valid_config.json"
    if [ -f "$HOME/.colab-cli-oauth-config.json" ]; then
        cp "$HOME/.colab-cli-oauth-config.json" "$GOOD_CONFIG"
    else
        # The cached token above means the config's own contents are never
        # actually exercised (no interactive flow will run) -- only that
        # it's valid, present JSON. Same shape as the bundled default.
        echo '{"installed":{"client_id":"test-client-id","client_secret":"test-secret","redirect_uris":["http://localhost"]}}' \
            > "$GOOD_CONFIG"
    fi

    HAPPY_OUTPUT=$(uv run mighty-colab --json -c "$GOOD_CONFIG" sessions)
    HAPPY_EXIT=$?

    if [ "$HAPPY_EXIT" -ne 0 ]; then
        fail "expected exit code 0 for a valid -c config, got $HAPPY_EXIT: $HAPPY_OUTPUT"
    elif ! echo "$HAPPY_OUTPUT" | python3 -c "import json,sys; json.loads(sys.stdin.read())" 2>/dev/null; then
        fail "stdout was not valid JSON for a valid -c config: $HAPPY_OUTPUT"
    else
        HAPPY_STATUS=$(echo "$HAPPY_OUTPUT" | python3 -c "import json,sys; print(json.loads(sys.stdin.read())['status'])")
        [ "$HAPPY_STATUS" = "ok" ] || fail "expected status=ok, got status=$HAPPY_STATUS: $HAPPY_OUTPUT"
    fi

    if [ "$FAILED" -eq 0 ]; then
        echo "[OK] Valid -c config + --json authenticates and returns a clean envelope."
    fi
fi

exit $FAILED
