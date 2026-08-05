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

from typer.testing import CliRunner

from colab_cli.cli import app

runner = CliRunner()


def test_restart_kernel_missing_session_errors_cleanly(mock_common_state):
    """Regression guard: restart_kernel was the only session-targeting
    command missing the `if not s:` guard, so an unknown session name
    raised a raw AttributeError instead of a clean 'not found' message."""
    mock_common_state.resolve_session.return_value = "bogus"
    mock_common_state.store.get.return_value = None

    result = runner.invoke(app, ["restart-kernel", "-s", "bogus"])

    assert result.exit_code == 1
    assert "not found" in result.stderr
