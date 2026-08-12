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

import json

from unittest.mock import MagicMock

from colab_cli.auth import AuthProvider
from colab_cli.cli import callback, state, _json_aware_echo, _handle_uncaught_exception


def _invoke_callback(invoked_subcommand="version", **overrides):
    ctx = MagicMock()
    # "version" is in _AUTO_UPDATE_SUPPRESSED (no background-check side
    # effect) but NOT in JSON_CAPABLE_COMMANDS -- fine for tests that don't
    # care about --json actually taking effect. Tests exercising a
    # successful --json invocation must pass a JSON_CAPABLE_COMMANDS name
    # (e.g. "exec") explicitly and mock `auto_update.run_background_check`.
    ctx.invoked_subcommand = invoked_subcommand
    kwargs = dict(
        ctx=ctx,
        client_oauth_config="/tmp/oauth.json",
        config=None,
        logtostderr=False,
        debug=False,
        no_strip_ansi=False,
        json_output=False,
        auth=AuthProvider.OAUTH2,
    )
    kwargs.update(overrides)
    return callback(**kwargs)


def test_no_strip_ansi_flag_off_by_default(mocker):
    mocker.patch("colab_cli.cli.setup_logging")
    _invoke_callback()
    assert state.no_strip_ansi is False


def test_no_strip_ansi_flag_sets_state(mocker):
    mocker.patch("colab_cli.cli.setup_logging")
    _invoke_callback(no_strip_ansi=True)
    assert state.no_strip_ansi is True


def test_json_flag_skips_background_check(mocker):
    """`--json`'s stdout is a machine-parseable envelope -- the daily
    upgrade-banner check must not run at all (not just get redirected to
    stderr), both to keep the invocation deterministic and to skip the
    unnecessary network round-trip. Uses `run` (JSON-capable, not
    `exec` -- see the `exec`-specific tests below for why that one's
    suppression is handled differently)."""
    mocker.patch("colab_cli.cli.setup_logging")
    mock_check = mocker.patch("colab_cli.cli.auto_update.run_background_check")
    _invoke_callback(invoked_subcommand="run", json_output=True)
    mock_check.assert_not_called()


def test_background_check_still_runs_without_any_json_signal(mocker):
    """Regression guard for the test above: a plain, non-`--json`
    invocation of a non-suppressed command must still run the background
    check -- suppression is scoped to machine-readable output, not
    disabled outright."""
    mocker.patch("colab_cli.cli.setup_logging")
    mock_check = mocker.patch("colab_cli.cli.auto_update.run_background_check")
    _invoke_callback(invoked_subcommand="run", json_output=False)
    mock_check.assert_called_once()


def test_exec_is_unconditionally_suppressed_at_the_root_callback(mocker):
    """`exec` is always in `_AUTO_UPDATE_SUPPRESSED`, regardless of
    `--json` -- unlike every other command, the root callback can't tell
    whether *this* invocation wants machine-parseable output, because
    `exec`'s hidden `--json-result-path` flag (used only by `exec-async`'s
    spawned child, which deliberately never receives `--json` itself --
    see `spawn_exec_async`) lives on `exec`'s own parser and isn't parsed
    yet when the root callback runs. An earlier fix tried to detect it by
    scanning raw `sys.argv` here instead -- that works for a real
    invocation (`main()` sees the process's actual argv) but silently does
    nothing under `CliRunner`-based tests (and thus in CI), which invoke
    `app()` directly and never touch `sys.argv` at all. `exec_command`
    itself now runs (or skips) the check after parsing its own
    `--json-result-path` -- see `tests/test_exec_json.py`'s
    `test_exec_*_background_check` tests for that half."""
    mocker.patch("colab_cli.cli.setup_logging")
    mock_check = mocker.patch("colab_cli.cli.auto_update.run_background_check")
    _invoke_callback(invoked_subcommand="exec", json_output=False)
    mock_check.assert_not_called()
    _invoke_callback(invoked_subcommand="exec", json_output=True)
    mock_check.assert_not_called()


def test_json_flag_off_by_default(mocker):
    mocker.patch("colab_cli.cli.setup_logging")
    _invoke_callback()
    assert state.json_output is False
    assert state.logtostderr is False


def test_json_flag_sets_state_and_forces_logtostderr(mocker):
    mock_setup_logging = mocker.patch("colab_cli.cli.setup_logging")
    mocker.patch("colab_cli.cli.auto_update.run_background_check")
    _invoke_callback(invoked_subcommand="exec", json_output=True)
    assert state.json_output is True
    assert state.logtostderr is True
    mock_setup_logging.assert_called_once_with(True, False)


def test_json_flag_on_unsupported_command_warns_and_disables_json_output(
    mocker, capsys
):
    mock_setup_logging = mocker.patch("colab_cli.cli.setup_logging")
    _invoke_callback(invoked_subcommand="version", json_output=True)
    # The command doesn't build an envelope -- state.json_output must be
    # reset so its normal stdout output isn't silently redirected to
    # stderr by the typer.echo wrapper, which only keys off that flag.
    assert state.json_output is False
    # --logtostderr is still forced -- harmless, and simpler than carving
    # out an exception for it too.
    assert state.logtostderr is True
    mock_setup_logging.assert_called_once_with(True, False)
    assert "--json has no effect on 'version'" in capsys.readouterr().err


def test_json_flag_on_each_json_capable_command_stays_enabled(mocker):
    mocker.patch("colab_cli.cli.setup_logging")
    mocker.patch("colab_cli.cli.auto_update.run_background_check")
    from colab_cli.cli import JSON_CAPABLE_COMMANDS

    for command in JSON_CAPABLE_COMMANDS:
        _invoke_callback(invoked_subcommand=command, json_output=True)
        assert state.json_output is True, f"--json disabled for {command!r}"


def test_explicit_logtostderr_is_not_clobbered_by_json_false(mocker):
    mock_setup_logging = mocker.patch("colab_cli.cli.setup_logging")
    _invoke_callback(json_output=False, logtostderr=True)
    assert state.logtostderr is True
    mock_setup_logging.assert_called_once_with(True, False)


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


# ---------------------------------------------------------------------------
# _check_global_option_position: catches a global flag placed after the
# subcommand (Click's own per-subcommand parser has no visibility into its
# parent's options, so this fails with a generic "No such option" otherwise).
# ---------------------------------------------------------------------------


def test_check_global_option_position_flags_json_after_subcommand_as_json_envelope(
    capsys,
):
    """`--json` itself being the misplaced option is exactly the case a
    caller piping into `jq` needs a parseable answer for -- plain text
    would just break their pipeline -- so this diverts to a JSON envelope
    on stdout instead, same as any other `--json`-present-in-argv case."""
    from colab_cli.cli import _check_global_option_position

    try:
        _check_global_option_position(["help", "--json"])
        assert False, "expected SystemExit"
    except SystemExit as e:
        assert e.code == 2
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["status"] == "error"
    assert envelope["exit_code"] == 2
    assert envelope["reason"] == "usage_error"
    assert envelope["command"] == "help"
    assert "'--json' is a global option" in envelope["message"]
    assert "must come before the subcommand" in envelope["message"]


def test_check_global_option_position_other_option_after_correctly_placed_json(
    capsys,
):
    """`--json` correctly precedes the subcommand while `--auth` doesn't --
    still JSON-shaped, since `--json` appearing anywhere in argv is enough
    to know the caller wants JSON back."""
    from colab_cli.cli import _check_global_option_position

    try:
        _check_global_option_position(["--json", "sessions", "--auth", "adc"])
        assert False, "expected SystemExit"
    except SystemExit as e:
        assert e.code == 2
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["command"] == "sessions"
    assert "'--auth' is a global option" in envelope["message"]


def test_check_global_option_position_allows_json_before_subcommand():
    from colab_cli.cli import _check_global_option_position

    _check_global_option_position(["--json", "help"])  # must not raise


def test_check_global_option_position_ignores_subcommand_local_options():
    from colab_cli.cli import _check_global_option_position

    # --tail/-n are exec/log's own options, not global ones -- must not
    # trigger the global-option hint just for appearing after the
    # subcommand, which is exactly where a subcommand's own options belong.
    _check_global_option_position(["log", "-s", "s1", "--tail", "-n", "5"])


def test_check_global_option_position_no_subcommand_at_all():
    from colab_cli.cli import _check_global_option_position

    _check_global_option_position(["--json"])  # must not raise
    _check_global_option_position([])  # must not raise


def test_check_global_option_position_flags_auth_after_subcommand(capsys):
    from colab_cli.cli import _check_global_option_position

    try:
        _check_global_option_position(["sessions", "--auth", "adc"])
        assert False, "expected SystemExit"
    except SystemExit as e:
        assert e.code == 2
    assert "'--auth' is a global option" in capsys.readouterr().err


def test_check_global_option_position_skips_value_of_preceding_global_option():
    """Regression guard: `--config <path>` (a value-taking global option)
    must not have its path argument mistaken for the subcommand name --
    that misidentification would make the *real* subcommand token (and
    anything genuinely after it) look like it came "after the subcommand"
    one token too early, false-flagging a correctly-positioned --json."""
    from colab_cli.cli import _check_global_option_position

    _check_global_option_position(
        ["--config", "/tmp/sessions.json", "--json", "log", "-s", "s1", "--tail"]
    )  # must not raise


def test_check_global_option_position_still_catches_mistake_after_value_taking_option(
    capsys,
):
    from colab_cli.cli import _check_global_option_position

    try:
        _check_global_option_position(
            ["--config", "/tmp/sessions.json", "log", "-s", "s1", "--json"]
        )
        assert False, "expected SystemExit"
    except SystemExit as e:
        assert e.code == 2
    # --json is present (it's the misplaced option itself here) -- JSON
    # envelope, not plain text, and the resolved command name skips past
    # --config's value token correctly.
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["command"] == "log"


# ---------------------------------------------------------------------------
# _handle_uncaught_exception: the top-level catch-all in main() around
# app(). Click's own Command.main() only catches ClickException/Abort/EPIPE
# (verified against its source) -- anything else, like auth.py's credential
# loading failures, escapes uncaught unless this catches it. Exercised
# directly (not through CliRunner, which never reaches main()'s own
# try/except -- it invokes commands through Click's main() itself).
# ---------------------------------------------------------------------------


def test_handle_uncaught_exception_reraises_under_debug():
    state.debug = False
    try:
        state.debug = True
        exc = RuntimeError("boom")
        try:
            _handle_uncaught_exception(exc, ["exec"])
            assert False, "expected the original exception to propagate"
        except RuntimeError as e:
            assert e is exc
    finally:
        state.debug = False


def test_handle_uncaught_exception_plain_text(capsys):
    state.debug = False
    state.json_output = False
    try:
        _handle_uncaught_exception(RuntimeError("boom"), ["exec"])
        assert False, "expected SystemExit"
    except SystemExit as e:
        assert e.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "[colab] Error: boom" in captured.err


def test_handle_uncaught_exception_plain_text_includes_hint(capsys):
    state.debug = False
    state.json_output = False
    exc = RuntimeError("boom")
    exc.envelope_hint = "Try again with --auth adc."
    try:
        _handle_uncaught_exception(exc, ["exec"])
        assert False, "expected SystemExit"
    except SystemExit:
        pass
    assert "[colab] Hint: Try again with --auth adc." in capsys.readouterr().err


def test_handle_uncaught_exception_json_envelope(capsys):
    state.debug = False
    state.json_output = True
    try:
        exc = RuntimeError("boom")
        exc.envelope_reason = "auth_adc_missing"
        exc.envelope_hint = "Run: gcloud auth application-default login"
        try:
            _handle_uncaught_exception(exc, ["run", "--json"])
            assert False, "expected SystemExit"
        except SystemExit as e:
            assert e.code == 1
        captured = capsys.readouterr()
        envelope = json.loads(captured.out)
        assert envelope["status"] == "error"
        assert envelope["command"] == "run"
        assert envelope["exit_code"] == 1
        assert envelope["reason"] == "auth_adc_missing"
        assert envelope["message"] == "boom"
        assert envelope["hint"] == "Run: gcloud auth application-default login"
        # The envelope is the stdout payload; the supplementary echo must
        # not corrupt it by sharing that stream.
        assert "[colab] Error: boom" in captured.err
    finally:
        state.json_output = False


def test_handle_uncaught_exception_json_from_argv_when_state_not_yet_set(capsys):
    """`--json` present in raw argv is honored even if `state.json_output`
    somehow isn't set yet -- same defensive check `_check_global_option_
    position` uses, for the same reason: a `--json` caller piping into `jq`
    must never get plain text back."""
    state.debug = False
    state.json_output = False
    try:
        exc = RuntimeError("boom")
        try:
            _handle_uncaught_exception(exc, ["run", "--json"])
            assert False, "expected SystemExit"
        except SystemExit:
            pass
        envelope = json.loads(capsys.readouterr().out)
        assert envelope["reason"] == "unhandled_error"
    finally:
        state.json_output = False


def test_handle_uncaught_exception_defaults_reason_when_exception_has_none(capsys):
    state.debug = False
    state.json_output = True
    try:
        try:
            _handle_uncaught_exception(RuntimeError("boom"), ["exec"])
        except SystemExit:
            pass
        envelope = json.loads(capsys.readouterr().out)
        assert envelope["reason"] == "unhandled_error"
        assert "hint" not in envelope
    finally:
        state.json_output = False


def test_handle_uncaught_exception_resolves_command_name_from_argv(capsys):
    state.debug = False
    state.json_output = True
    try:
        try:
            _handle_uncaught_exception(
                RuntimeError("boom"), ["--auth", "adc", "stop", "--json"]
            )
        except SystemExit:
            pass
        envelope = json.loads(capsys.readouterr().out)
        assert envelope["command"] == "stop"
    finally:
        state.json_output = False
