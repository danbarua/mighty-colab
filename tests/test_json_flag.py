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

"""The global `--json` flag: callback wiring and the `typer.echo` redirect.

`cli.callback` mutates the real `colab_cli.cli.state` object it captured at
import time (not the per-test `colab_cli.common.state` mock the autouse
`mock_common_state` fixture installs -- `from colab_cli.common import state`
binds a static reference, so patching the module attribute later doesn't
retarget it). So the callback is exercised directly here, against the real
singleton, rather than through `CliRunner` + the mock.
"""

from unittest.mock import MagicMock

from colab_cli.auth import AuthProvider
from colab_cli.cli import callback, state, _json_aware_echo


def _invoke_callback(**overrides):
    ctx = MagicMock()
    ctx.invoked_subcommand = "version"  # in _AUTO_UPDATE_SUPPRESSED, no side effects
    kwargs = dict(
        ctx=ctx,
        client_oauth_config="/tmp/oauth.json",
        config=None,
        logtostderr=False,
        json_output=False,
        auth=AuthProvider.OAUTH2,
    )
    kwargs.update(overrides)
    return callback(**kwargs)


def test_json_flag_off_by_default(mocker):
    mocker.patch("colab_cli.cli.setup_logging")
    _invoke_callback()
    assert state.json_output is False
    assert state.logtostderr is False


def test_json_flag_sets_state_and_forces_logtostderr(mocker):
    mock_setup_logging = mocker.patch("colab_cli.cli.setup_logging")
    _invoke_callback(json_output=True)
    assert state.json_output is True
    assert state.logtostderr is True
    mock_setup_logging.assert_called_once_with(True)


def test_explicit_logtostderr_is_not_clobbered_by_json_false(mocker):
    mock_setup_logging = mocker.patch("colab_cli.cli.setup_logging")
    _invoke_callback(json_output=False, logtostderr=True)
    assert state.logtostderr is True
    mock_setup_logging.assert_called_once_with(True)


def test_echo_wrapper_noop_when_json_output_false(mock_common_state, mocker):
    mock_common_state.json_output = False
    mock_original = mocker.patch("colab_cli.cli._original_echo")
    _json_aware_echo("hello")
    mock_original.assert_called_once_with(
        message="hello", file=None, nl=True, err=False, color=None
    )


def test_echo_wrapper_redirects_to_stderr_when_json_output_true(
    mock_common_state, mocker
):
    mock_common_state.json_output = True
    mock_original = mocker.patch("colab_cli.cli._original_echo")
    _json_aware_echo("hello")
    mock_original.assert_called_once_with(
        message="hello", file=None, nl=True, err=True, color=None
    )


def test_echo_wrapper_respects_explicit_err_true(mock_common_state, mocker):
    """Already explicit err=True call sites are untouched either way."""
    mock_common_state.json_output = True
    mock_original = mocker.patch("colab_cli.cli._original_echo")
    _json_aware_echo("hello", err=True)
    mock_original.assert_called_once_with(
        message="hello", file=None, nl=True, err=True, color=None
    )


def test_echo_wrapper_respects_explicit_file(mock_common_state, mocker):
    """A caller passing an explicit `file=` (e.g. the final JSON envelope
    itself, or the wrapper's own bypass mechanism) must not be redirected --
    this is how the envelope print escapes its own redirect rule."""
    mock_common_state.json_output = True
    mock_original = mocker.patch("colab_cli.cli._original_echo")
    import sys

    _json_aware_echo("hello", file=sys.stdout)
    mock_original.assert_called_once_with(
        message="hello", file=sys.stdout, nl=True, err=False, color=None
    )
