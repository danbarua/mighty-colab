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

import os
import sys
from typing import List, NoReturn, Optional

import click
import typer
from typer import rich_utils
from typer.core import TyperGroup
from typing_extensions import Annotated

from colab_cli import auto_update
from colab_cli import common
from colab_cli.auth import AuthProvider
from colab_cli.common import build_envelope, emit_json, state, setup_logging
from colab_cli.commands import session, execution, files, automation, run, ssh, utility
from colab_cli.commands import adopt, mcp

# The only commands that emit a `--json` envelope. Kept as one literal set
# (not derived from the Click tree) so both `callback()` (does this
# invocation's subcommand actually support --json?) and `main()`'s
# flag-position pre-check (does this look like a global-flag typo?) agree
# on the exact same list without importing each other's internals.
JSON_CAPABLE_COMMANDS = {
    "exec",
    "run",
    "exec-async",
    "log",
    "new",
    "stop",
    "sessions",
    "status",
}

# Every option defined on the root `@app.callback()` below -- i.e. one that
# must precede the subcommand name, not follow it. Click's per-subcommand
# parser has no visibility into its parent's options, so one of these
# appearing after the subcommand fails with a generic "No such option"
# instead of a hint pointing at the actual mistake (see
# `_check_global_option_position`).
GLOBAL_OPTION_NAMES = {
    "-c",
    "--client-oauth-config",
    "--config",
    "--logtostderr",
    "--debug",
    "--json",
    "--no-strip-ansi",
    "--auth",
}

# The subset of GLOBAL_OPTION_NAMES that consume a following argv token as
# their value (as opposed to `--json`/`--logtostderr`, which are boolean
# flags). Needed so `_check_global_option_position` doesn't mistake e.g.
# `--config`'s path argument for the subcommand name.
GLOBAL_OPTIONS_WITH_VALUE = {"-c", "--client-oauth-config", "--config", "--auth"}

_original_echo = typer.echo


def _json_aware_echo(message=None, file=None, nl=True, err=False, color=None):
    """Route `typer.echo(...)` calls to stderr while `--json` is active.

    Installed once, globally, by reassigning `typer.echo` itself: every one
    of this codebase's ~160 `typer.echo(...)` call sites is module-qualified
    (no `from typer import echo`, no direct `click.echo`), so Python's
    attribute-lookup-at-call-time means this single reassignment covers all
    of them with zero per-call-site edits.

    Looks up `common.state` fresh on every call (not a closed-over
    reference) so it reflects whatever `--json` actually did for *this*
    invocation. A no-op when `--json` isn't set (the default) -- zero risk
    to existing behavior. The final JSON envelope itself must be printed via
    the original `typer.echo` (or with an explicit `file=`) so it isn't
    caught by its own redirect rule.
    """
    if common.state.json_output and not err and file is None:
        err = True
    return _original_echo(message=message, file=file, nl=nl, err=err, color=color)


typer.echo = _json_aware_echo


def _usage_error_hint(exc) -> Optional[str]:
    """A short, actionable suggestion for the handful of parse-error shapes
    worth special-casing -- not general-purpose inference. New shapes get
    added when a real one shows up, the same bar already applied to
    deferring input-parameter echoing in the envelope.
    """
    message = exc.format_message()
    ctx = getattr(exc, "ctx", None)
    if ctx is not None and "unexpected extra argument" in message.lower():
        # The single most common shape: a session name passed positionally
        # (e.g. `stop SESSION`) instead of via the command's own `-s/
        # --session` option.
        for param in ctx.command.params:
            if getattr(param, "name", None) == "session" and getattr(
                param, "opts", None
            ):
                opt = param.opts[0]
                return f"Pass the session name with '{opt}', not as a positional argument."
    return None


_original_rich_format_error = rich_utils.rich_format_error


def _json_aware_rich_format_error(exc) -> None:
    """Divert Click/Typer parse errors (unknown option, missing argument,
    unexpected extra argument, ...) into a JSON envelope on stdout while
    `--json` is active, instead of Typer's Rich-boxed stderr display --
    which a `--json` caller piping into `jq` would otherwise have to
    text-scrape (or get nothing at all, since the box goes to stderr).

    Hooked here because Typer funnels every parse-time `ClickException`
    through this one function (`typer.core._main`'s except-ClickException
    branch) whenever Rich rendering is enabled -- the same choke point that
    produces the boxed "Error: ..." panel. By the time this fires, the
    root `@app.callback()` has already run (a Click Group invokes its own
    callback before parsing its subcommand's args -- verified empirically,
    not assumed), so `state.json_output` already reflects whatever `--json`
    did for this invocation. Checks the module-level `state` (the exact
    object `callback()` itself mutates), not `common.state` -- unlike
    `_json_aware_echo`'s ~160 call sites spread across the whole codebase,
    this function has exactly one caller (Typer's own error path) and
    fires only after `callback()` has already run, so there's no benefit
    to a fresh `common.state` lookup, only a cost: under test mocking that
    patches `colab_cli.common.state` to a separate object (see
    `tests/conftest.py`), a fresh lookup would miss the mutation this
    module's own `callback()` just made.
    """
    if state.json_output:
        ctx = getattr(exc, "ctx", None)
        command_name = ctx.command.name if ctx is not None and ctx.command.name else "cli"
        extra = {"message": exc.format_message()}
        hint = _usage_error_hint(exc)
        if hint is not None:
            extra["hint"] = hint
        envelope = build_envelope(
            "error", command_name, exit_code=exc.exit_code, reason="usage_error", **extra
        )
        emit_json(envelope)
        return
    _original_rich_format_error(exc)


rich_utils.rich_format_error = _json_aware_rich_format_error


class AlphabeticalGroup(TyperGroup):
    """A `TyperGroup` that lists subcommands alphabetically in `--help` output.

    Subcommands are registered in functional groups (session, execution, files,
    automation, utility), but users discovering the CLI via `colab --help` /
    `colab help` benefit from a deterministic, alphabetical listing.
    """

    def list_commands(self, ctx: click.Context) -> list[str]:
        return sorted(super().list_commands(ctx))


app = typer.Typer(
    help="Colab CLI",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
    cls=AlphabeticalGroup,
)


@app.callback()
def callback(
    ctx: typer.Context,
    client_oauth_config: Annotated[
        str,
        typer.Option(
            "-c", "--client-oauth-config", help="Path to client OAuth config JSON file"
        ),
    ] = os.path.expanduser("~/.colab-cli-oauth-config.json"),
    config: Annotated[
        Optional[str],
        typer.Option(
            "--config",
            help="Path to session state file (~/.config/colab-cli/sessions.json)",
        ),
    ] = None,
    logtostderr: Annotated[
        bool, typer.Option("--logtostderr", help="Log all output to stderr")
    ] = False,
    debug: Annotated[
        bool,
        typer.Option(
            "--debug",
            help=(
                "Enable DEBUG-level logging, including third-party library "
                "chatter (urllib3, jupyter_kernel_client, websocket). "
                "Default level is INFO."
            ),
        ),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option(
            "--json",
            help=(
                "Emit machine-readable JSON on stdout for exec/run/exec-async/"
                "log --tail. Human-readable '[colab] ...' chatter moves to "
                "stderr. Implies --logtostderr."
            ),
        ),
    ] = False,
    no_strip_ansi: Annotated[
        bool,
        typer.Option(
            "--no-strip-ansi",
            help=(
                "Keep raw ANSI escapes in traceback text instead of "
                "stripping them. Only applies with --json on "
                "exec/exec-async/run; ignored otherwise. Off by default."
            ),
        ),
    ] = False,
    auth: Annotated[
        AuthProvider,
        typer.Option(
            "--auth",
            help=(
                "Authentication strategy to use: 'oauth2' (public InstalledAppFlow),"
                " or 'adc' (Application Default Credentials)."
            ),
            case_sensitive=False,
        ),
    ] = AuthProvider.OAUTH2,
):
    """
    Colab CLI global configuration.
    """
    state.client_oauth_config = client_oauth_config
    state.config_path = config
    state.json_output = json_output
    state.no_strip_ansi = no_strip_ansi
    if json_output:
        # Free complementary flip: also route logging.* output to stderr.
        # Unrelated mechanism from the typer.echo redirect above (this one
        # controls the stdlib `logging` module), but both belong on stderr
        # under --json for the same reason -- stdout must carry the JSON only.
        logtostderr = True
        if ctx.invoked_subcommand not in JSON_CAPABLE_COMMANDS:
            # `--json` positioned correctly (it parsed) but on a command
            # that never builds an envelope. Without this, the command
            # would run "successfully" with its entire normal stdout
            # silently redirected to stderr by the typer.echo wrapper
            # below (which only keys off `state.json_output`, not which
            # command is running) -- indistinguishable from the command
            # just producing no output. Restore normal stdout behavior and
            # say so, rather than degrade silently.
            state.json_output = False
            typer.echo(
                f"[colab] --json has no effect on '{ctx.invoked_subcommand}'; "
                f"supported on: {', '.join(sorted(JSON_CAPABLE_COMMANDS))}.",
                err=True,
            )
    state.logtostderr = logtostderr
    state.debug = debug
    state.auth_provider = auth
    setup_logging(logtostderr, debug)

    # Daily fetch + cached banner on every invocation.
    #
    # Suppress the banner for short-lived informational subcommands so their
    # output stays clean and machine-parseable:
    #   - `update`: runs its own check + announce; would duplicate the banner.
    #   - `version`, `log`, `pay`, `help`, `url`: pure-display commands whose
    #     output users routinely pipe / scrape (e.g. `colab url -s s1 | xclip`);
    #     a stochastic upgrade banner injected once a day would corrupt those
    #     pipelines.
    #   - `whoami`: developer-only debugging tool; banner would obscure the
    #     auth/scope info the user invoked it to see.
    _AUTO_UPDATE_SUPPRESSED = {
        "update",
        "version",
        "log",
        "pay",
        "help",
        "url",
        "whoami",
        "readme",
        "README",
        "skill",
        "SKILL",
    }
    if ctx.invoked_subcommand not in _AUTO_UPDATE_SUPPRESSED:
        auto_update.run_background_check()


@app.command(name="help")
def help_command(
    ctx: typer.Context,
    command: Annotated[
        Optional[str], typer.Argument(help="Command to show help for")
    ] = None,
):
    """
    Show help for a command.
    """
    if not command:
        typer.echo(ctx.parent.get_help())
        return

    group = ctx.parent.command
    cmd = group.get_command(ctx, command)
    if cmd is None:
        typer.echo(f"No such command '{command}'.", err=True)
        raise typer.Exit(code=2)

    with click.Context(cmd, info_name=command, parent=ctx.parent) as cmd_ctx:
        typer.echo(cmd.get_help(cmd_ctx))


# Register subcommands
adopt.register(app)
session.register(app)
execution.register(app)
files.register(app)
automation.register(app)
run.register(app)
ssh.register(app)
utility.register(app)
mcp.register(app)


def _command_name_from_argv(argv: List[str]) -> str:
    """Best-effort subcommand name, scanning raw `argv` ourselves.

    Skips past global options (and, for value-taking ones, their separate
    value token) to find the first non-option token. Shared by
    `_check_global_option_position` (which runs before Click ever touches
    `argv`) and `main()`'s top-level catch-all (which runs *after* Click
    has unwound its context on an exception, so `click.get_current_context()`
    is no longer available there either).
    """
    i = 0
    while i < len(argv) and argv[i].startswith("-"):
        option_name = argv[i].split("=", 1)[0]
        if option_name in GLOBAL_OPTIONS_WITH_VALUE and "=" not in argv[i]:
            i += 2  # this option and its separate value token
        else:
            i += 1
    return argv[i] if i < len(argv) else "cli"


def _check_global_option_position(argv: List[str]) -> None:
    """Catch the common mistake of putting a global option (`--json`,
    `--auth`, ...) after the subcommand instead of before it.

    Click parses top-down: global options belong to the root group's own
    parser, and once it hits the first non-option token (the subcommand
    name) it hands the rest of `argv` to that subcommand's parser --  which
    has no knowledge of its parent's options at all. `mighty-colab help
    --json` therefore fails inside `help`'s own parser with a generic
    "No such option: --json", which doesn't hint that `--json` is real,
    just misplaced. Scanning `argv` ourselves, before Click ever sees it,
    lets us catch exactly that shape of mistake and point at the fix.

    Deliberately narrow: this only recognizes the option *names* this CLI
    itself defines globally, appearing after the subcommand. It does not
    attempt to parse `argv` in general -- it only needs to correctly find
    *where* the subcommand token is (skipping past global options AND, for
    value-taking ones like `--config PATH`, their value token too, so the
    path itself is never mistaken for the subcommand name), then treat
    everything after that as fair game to check. Confirmed no current
    subcommand redefines any of `GLOBAL_OPTION_NAMES`.
    """
    command_name = _command_name_from_argv(argv)
    i = 0
    while i < len(argv) and argv[i].startswith("-"):
        option_name = argv[i].split("=", 1)[0]
        if option_name in GLOBAL_OPTIONS_WITH_VALUE and "=" not in argv[i]:
            i += 2  # this option and its separate value token
        else:
            i += 1
    i += 1  # skip the subcommand token itself, if there was one

    for arg in argv[i:]:
        if arg.startswith("-"):
            option_name = arg.split("=", 1)[0]
            if option_name in GLOBAL_OPTION_NAMES:
                message = (
                    f"'{option_name}' is a global option and must come "
                    f"before the subcommand, e.g.: mighty-colab {option_name} "
                    f"<command> [args...]"
                )
                if "--json" in argv:
                    # This runs before Click (and this CLI's own callback)
                    # ever parses argv, so `common.state.json_output` isn't
                    # set yet -- check the raw tokens instead. `--json`
                    # itself may be the misplaced option here, or it may
                    # genuinely precede the subcommand while some other
                    # global option doesn't; either way, a caller piping
                    # into `jq` should get valid JSON back, not plain text
                    # that breaks the pipeline.
                    emit_json(
                        build_envelope(
                            "error",
                            command_name,
                            exit_code=2,
                            reason="usage_error",
                            message=message,
                        )
                    )
                else:
                    typer.echo(f"[colab] {message}", err=True)
                raise SystemExit(2)


def _handle_uncaught_exception(exc: Exception, argv: List[str]) -> NoReturn:
    """Convert an exception that escaped every command's own error handling
    into the same `[colab] Error: ...` / JSON-envelope shape every other
    failure mode in this CLI produces, instead of a raw Python traceback --
    "like a normal software tool" (AGENT_USABILITY_LEARNINGS.md).

    Click's own `Command.main()` (verified via its source) only catches
    `ClickException`, `Abort`, and `OSError` with `errno == EPIPE` --
    anything else (e.g. `auth.py`'s credential-loading failures) propagates
    all the way out of `app()` uncaught. This is the last chance to catch
    it before the interpreter does, hence living in `main()` around the
    `app()` call rather than deeper in any one command.

    By the time an exception reaches here, Click has already unwound its
    context stack, so `click.get_current_context()` is unavailable --
    `_command_name_from_argv` recovers the subcommand name from raw `argv`
    the same way `_check_global_option_position` does, before Click ever
    touches it.

    `--debug` bypasses this entirely and re-raises, so the full traceback
    still reaches whoever's diagnosing the CLI itself -- this handler is
    for end users, not for developing the CLI.

    Split out from `main()` as its own function so it's callable directly
    from a test with a crafted exception and argv, since Typer's
    `CliRunner` (used by nearly every other test in this codebase) invokes
    commands through Click's own `main()` and never reaches this code --
    only the real installed console script does.
    """
    if state.debug:
        raise exc
    command_name = _command_name_from_argv(argv)
    message = str(exc) or type(exc).__name__
    reason = getattr(exc, "envelope_reason", "unhandled_error")
    hint = getattr(exc, "envelope_hint", None)
    if state.json_output or "--json" in argv:
        extra = {"hint": hint} if hint else {}
        emit_json(
            build_envelope(
                "error", command_name, exit_code=1, reason=reason, message=message, **extra
            )
        )
        typer.echo(f"[colab] Error: {message}", err=True)
    else:
        typer.echo(f"[colab] Error: {message}", err=True)
        if hint:
            typer.echo(f"[colab] Hint: {hint}", err=True)
    raise SystemExit(1)


def main():
    argv = sys.argv[1:]
    _check_global_option_position(argv)
    try:
        app()
    except Exception as e:
        _handle_uncaught_exception(e, argv)


if __name__ == "__main__":
    main()
