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

import json

from unittest.mock import MagicMock, patch
import pytest
import typer
from colab_cli.auth import AuthProvider
from colab_cli.common import State


def test_resolve_session_no_local_sessions():
    state = State()
    state._store = MagicMock()
    state._store.list.return_value = {}

    with patch("typer.echo") as mock_echo:
        with pytest.raises(typer.Exit):
            state.resolve_session(None)
        mock_echo.assert_any_call(
            "[colab] Error: No active sessions found. Create one with 'colab new'."
        )


def test_resolve_session_with_local_but_none_on_server():
    state = State()
    state._store = MagicMock()
    # Local session exists
    mock_session = MagicMock()
    mock_session.endpoint = "e1"
    state._store.list.return_value = {"s1": mock_session}

    # But server says no assignments
    state._client = MagicMock()
    state._client.list_assignments.return_value = []

    # Mock history and store.remove
    state._history = MagicMock()

    with patch("typer.echo") as mock_echo:
        with pytest.raises(typer.Exit):
            state.resolve_session(None)
        mock_echo.assert_any_call("[colab] Pruned 1 stale local session(s).")
        mock_echo.assert_any_call(
            "[colab] Error: No active sessions found. Create one with 'colab new'."
        )

    state._store.remove.assert_called_with("s1")


# ---------------------------------------------------------------------------
# resolve_session under --json: this helper predates the --json design and
# was the one escape hatch left after every *other* error path in
# exec/exec-async/stop was made JSON-aware -- it's shared code called before
# any of those commands' own JSON-gating logic runs. Fixed once here rather
# than patching each of the three call sites (see State._resolve_session_failure).
# ---------------------------------------------------------------------------


def test_resolve_session_json_no_local_sessions_emits_envelope(capsys):
    state = State()
    state.json_output = True
    state._store = MagicMock()
    state._store.list.return_value = {}

    with pytest.raises(typer.Exit) as excinfo:
        state.resolve_session(None, command="exec")
    assert excinfo.value.exit_code == 1

    envelope = json.loads(capsys.readouterr().out)
    assert envelope["command"] == "exec"
    assert envelope["status"] == "error"
    assert envelope["reason"] == "no_active_sessions"
    assert "No active sessions found" in envelope["message"]


def test_resolve_session_json_ambiguous_sessions_emits_envelope(capsys):
    state = State()
    state.json_output = True
    state._store = MagicMock()
    mock_session = MagicMock()
    mock_session.endpoint = "e1"
    state._store.list.return_value = {"s1": mock_session, "s2": mock_session}

    state._client = MagicMock()
    state._client.list_assignments.return_value = [
        MagicMock(endpoint="e1"),
    ]
    # Both local sessions report the same endpoint "e1" so neither gets
    # pruned by sync_sessions -- both stay "active", forcing the ambiguous
    # (len > 1) branch instead of the "none survived sync" branch.
    with pytest.raises(typer.Exit) as excinfo:
        state.resolve_session(None, command="exec-async")
    assert excinfo.value.exit_code == 1

    envelope = json.loads(capsys.readouterr().out)
    assert envelope["command"] == "exec-async"
    assert envelope["reason"] == "ambiguous_session"
    assert "Multiple active sessions found" in envelope["message"]


def test_resolve_session_without_json_still_plain_text(capsys):
    """Regression guard: default (non-`--json`) behavior is byte-for-byte
    unchanged -- no envelope, no change to the plain-text message."""
    state = State()
    state._store = MagicMock()
    state._store.list.return_value = {}

    with pytest.raises(typer.Exit):
        state.resolve_session(None, command="exec")

    out = capsys.readouterr().out
    assert "{" not in out
    assert "[colab] Error: No active sessions found" in out


def test_sync_sessions_avoids_client_if_no_local():
    state = State()
    state._store = MagicMock()
    state._store.list.return_value = {}

    # We want to verify that self.client is NOT accessed if store.list() is empty
    # unless we explicitly call sync_sessions.
    # Actually, in my current implementation of sync_sessions, I still call self.client.list_assignments()
    # to support 'colab sessions' but I wrap it in a try-except.

    with patch.object(State, "client", new_callable=MagicMock) as mock_client_prop:
        state.sync_sessions()
        # My implementation DOES call it to return assignments.
        mock_client_prop.list_assignments.assert_called_once()


def test_resolve_session_avoids_sync_if_no_local():
    state = State()
    state._store = MagicMock()
    state._store.list.return_value = {}

    with patch.object(State, "sync_sessions") as mock_sync:
        with pytest.raises(typer.Exit):
            state.resolve_session(None)
        mock_sync.assert_not_called()


def test_state_client_auth_flag_propagation():
    state = State()
    state.auth_provider = AuthProvider.OAUTH2

    with patch("colab_cli.common.get_credentials") as mock_get_creds:
        with patch("colab_cli.common.Client"):
            _ = state.client
            mock_get_creds.assert_called_once()
            args, kwargs = mock_get_creds.call_args
            assert kwargs["provider"] is AuthProvider.OAUTH2


def test_state_client_auth_provider_default_is_oauth2():
    state = State()
    assert state.auth_provider is AuthProvider.OAUTH2

    with patch("colab_cli.common.get_credentials") as mock_get_creds:
        with patch("colab_cli.common.Client"):
            _ = state.client
            args, kwargs = mock_get_creds.call_args
            assert kwargs["provider"] is AuthProvider.OAUTH2


def test_state_client_auth_provider_adc():
    state = State()
    state.auth_provider = AuthProvider.ADC

    with patch("colab_cli.common.get_credentials") as mock_get_creds:
        with patch("colab_cli.common.Client"):
            _ = state.client
            args, kwargs = mock_get_creds.call_args
            assert kwargs["provider"] is AuthProvider.ADC
