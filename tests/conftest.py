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

import pytest
from unittest.mock import MagicMock


@pytest.fixture(autouse=True)
def mock_common_state(mocker, tmp_path):
    # Patch the state singleton in common.py
    mock_state = mocker.patch("colab_cli.common.state")

    # Same auto-vivify hazard as `json_output` below, but with a worse
    # symptom: `config_path` is fed to `Path()`, and a MagicMock satisfies
    # `os.fspath`, so anything deriving a directory from it silently
    # creates a literal `MagicMock/state.config_path/` tree in the repo
    # root -- and one test's leftovers become the next test's input.
    # Pin a real per-test path.
    mock_state.config_path = str(tmp_path / "colab-cli" / "sessions.json")

    # Setup standard mocks for properties
    mock_state.store = MagicMock()
    mock_state.client = MagicMock()
    mock_state.history = MagicMock()

    # Default behavior for sync_sessions
    mock_state.sync_sessions.return_value = ({}, [])

    # `state` is a MagicMock, so an unset `json_output` would auto-vivify as
    # a truthy MagicMock and silently flip every `--json`-gated branch on.
    # Pin the real default here; tests that want `--json` behavior override
    # it explicitly.
    mock_state.json_output = False
    # Same auto-vivify hazard for `no_strip_ansi` -- pin its real default
    # (stripping ANSI) so tests not exercising `--no-strip-ansi` don't
    # silently get raw tracebacks.
    mock_state.no_strip_ansi = False
    # Same hazard for `debug` -- pin it off so no test's uncaught-exception
    # path silently re-raises instead of exercising the --json/plain-text
    # error envelope `_handle_uncaught_exception` builds.
    mock_state.debug = False

    # Global patch for ColabRuntime to prevent network calls
    # We patch it in the modules where it is imported and used
    mocker.patch("colab_cli.commands.session.ColabRuntime")
    # `commands/job.py` imports ColabRuntime *inside* `apply()`, so a patch
    # on that module's namespace would be re-resolved away on every call.
    # Patch the source module: this is the last line of defence stopping a
    # CLI test from provisioning a real VM (it has happened).
    mocker.patch("colab_cli.runtime.ColabRuntime")
    mocker.patch("colab_cli.commands.execution.ColabRuntime")
    mocker.patch("colab_cli.commands.automation.ColabRuntime")
    mocker.patch("colab_cli.commands.run.ColabRuntime")

    return mock_state
