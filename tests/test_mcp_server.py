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

from unittest.mock import MagicMock, patch

import click
import pytest
import typer.main

from colab_cli.cli import app
from colab_cli.client import Accelerator, AssignmentVariant
from colab_cli.mcp_server import (
    EXCLUDED_COMMANDS,
    build_tools,
    invoke_command,
)


def _listed_assignment(endpoint, variant, accelerator, token, url):
    return MagicMock(
        endpoint=endpoint,
        variant=variant,
        accelerator=accelerator,
        runtime_proxy_info=MagicMock(token=token, url=url),
    )


@pytest.fixture(scope="module")
def click_group():
    return typer.main.get_command(app)


@pytest.fixture(scope="module")
def tools_and_commands(click_group):
    return build_tools(click_group)


# --- scanning: which commands get exposed -----------------------------------


def test_excludes_interactive_and_internal_commands(click_group, tools_and_commands):
    """ssh/repl/console/edit/drivemount must never become MCP tools -- they
    block on a live terminal, an editor, or a Drive re-auth ceremony. `auth`
    and `keep-alive` are covered separately since they're excluded via
    Click's `hidden` flag rather than by name."""
    tools, _ = tools_and_commands
    names = {t.name for t in tools}

    assert names.isdisjoint(EXCLUDED_COMMANDS)
    for interactive in ("ssh", "repl", "console", "edit", "drivemount", "mcp", "help", "pay"):
        assert interactive not in names


def test_excludes_hidden_commands(click_group, tools_and_commands):
    tools, _ = tools_and_commands
    names = {t.name for t in tools}

    hidden_names = {n for n, c in click_group.commands.items() if c.hidden}
    assert hidden_names, "expected at least one hidden command in the real CLI"
    assert names.isdisjoint(hidden_names)


def test_includes_ordinary_scriptable_commands(tools_and_commands):
    tools, _ = tools_and_commands
    names = {t.name for t in tools}

    for scriptable in ("new", "stop", "status", "sessions", "adopt", "exec", "run"):
        assert scriptable in names


def test_every_tool_has_a_description(tools_and_commands):
    tools, _ = tools_and_commands
    for tool in tools:
        assert tool.description, f"{tool.name} has no description"


def test_help_option_is_not_a_tool_parameter(tools_and_commands):
    tools, _ = tools_and_commands
    for tool in tools:
        assert "help" not in tool.input_schema["properties"]


# --- schema generation for real, representative commands --------------------


def test_run_schema_types(tools_and_commands):
    """Regression guard: Typer builds params from its own vendored
    `typer._click.types.*` classes, not `click.types.*` -- an `isinstance`
    check against the real `click` module silently never matches, which
    previously mis-typed every bool/int/float param as "string"."""
    tools, _ = tools_and_commands
    by_name = {t.name: t for t in tools}
    props = by_name["run"].input_schema["properties"]

    assert props["script"]["type"] == "string"
    assert props["keep"]["type"] == "boolean"
    assert props["keep"]["default"] is False
    assert props["timeout"]["type"] == "number"
    assert props["timeout"]["default"] == 30.0
    assert by_name["run"].input_schema["required"] == ["script"]


def test_run_variadic_and_repeatable_params_are_arrays(tools_and_commands):
    """click-mcp's own schema builder mis-typed `multiple=True` options and
    variadic (nargs=-1) arguments as plain strings; make sure ours doesn't."""
    tools, _ = tools_and_commands
    by_name = {t.name: t for t in tools}
    props = by_name["run"].input_schema["properties"]

    assert props["script_args"]["type"] == "array"
    assert props["script_args"]["items"]["type"] == "string"
    assert props["env"]["type"] == "array"
    assert props["env"]["items"]["type"] == "string"


def test_adopt_schema_matches_command(tools_and_commands):
    tools, _ = tools_and_commands
    by_name = {t.name: t for t in tools}
    props = by_name["adopt"].input_schema["properties"]

    assert props["endpoint"]["type"] == "string"
    assert props["orphanage"]["type"] == "boolean"
    assert props["name"]["type"] == "string"
    assert "required" not in by_name["adopt"].input_schema


def test_version_description_returns_actual_version(click_group):
    # `tools_and_commands` is module-scoped and built once, before this
    # patch takes effect -- reading it here would just see whatever version
    # was installed the first time any test in this module touched the
    # fixture. Build tools fresh, inside the patched context, instead.
    with patch("colab_cli.auto_update.installed_version") as mock_version:
        mock_version.return_value = "1.2.3"
        tools, _ = build_tools(click_group)
        by_name = {t.name: t for t in tools}
        tool = by_name["version"]
        description = tool.description
        assert description == "Version: 1.2.3"


# --- dispatch: invoking a Click command's callback in-process ---------------


def test_invoke_command_runs_and_captures_stdout(tools_and_commands, mock_common_state):
    _, commands = tools_and_commands
    mock_common_state.store.list.return_value = {}
    mock_common_state.client.list_assignments.return_value = [
        _listed_assignment("ep1", AssignmentVariant.GPU, Accelerator.T4, "tok", "http://u"),
    ]

    ok, text = invoke_command("adopt", commands["adopt"], {"endpoint": "ep1"})

    assert ok is True
    assert "Successfully adopted session as 'ep1'" in text
    mock_common_state.store.add.assert_called_once()


def test_invoke_command_captures_stderr_error_messages(tools_and_commands, mock_common_state):
    """Commands report user errors via `typer.echo(..., err=True)` before
    raising `typer.Exit`. Losing stderr would leave the MCP caller with only
    a bare exit code instead of the actual reason."""
    _, commands = tools_and_commands

    ok, text = invoke_command("adopt", commands["adopt"], {})

    assert ok is False
    assert "Provide an ENDPOINT to adopt, or use --orphanage" in text


def test_invoke_command_reports_missing_required_argument(tools_and_commands, mock_common_state):
    _, commands = tools_and_commands

    ok, text = invoke_command("run", commands["run"], {})

    assert ok is False
    assert "script" in text.lower()


def test_invoke_command_applies_defaults_for_omitted_optional_args(
    tools_and_commands, mock_common_state
):
    """Omitting an optional MCP argument must fall back to the command's own
    Click default, not `None`/a missing kwarg crash."""
    _, commands = tools_and_commands
    mock_common_state.store.list.return_value = {}
    mock_common_state.client.list_assignments.return_value = [
        _listed_assignment(
            "ep2", AssignmentVariant.DEFAULT, Accelerator.NONE, "tok2", "http://u2"
        ),
    ]

    # `name` omitted entirely -- adopt() must default it to the endpoint.
    ok, _ = invoke_command("adopt", commands["adopt"], {"endpoint": "ep2"})

    assert ok is True
    saved = mock_common_state.store.add.call_args.args[0]
    assert saved.name == "ep2"


def test_invoke_command_unknown_tool_name_is_not_registered(tools_and_commands):
    _, commands = tools_and_commands
    assert "ssh" not in commands
    assert "does-not-exist" not in commands


# --- a minimal synthetic command, decoupled from CLI business logic ---------


@click.command()
@click.option("--count", type=int, required=True, help="How many")
@click.option("--flag", is_flag=True, help="A boolean flag")
@click.option("--tag", multiple=True, help="Repeatable string option")
def _sample(count, flag, tag):
    """A synthetic command for isolated schema/dispatch tests."""
    click.echo(f"count={count} flag={flag} tags={list(tag)}")


@click.command()
def _sample_ansi():
    """A synthetic command emitting IPython-style colored traceback text."""
    click.echo(
        "\x1b[0;31m---------------------------------------------------------"
        "------------------\x1b[0m\x1b[0;31mCalledProcessError\x1b[0m"
        "Traceback (most recent call last)"
    )


def test_invoke_command_strips_ansi_escape_codes():
    """Regression test for the raw \\x1b[0;31m-style SGR codes IPython's
    colored traceback formatter embeds in Colab kernel error output --
    unreadable noise for an MCP client, even though a human terminal wants
    them. Stripped only at this MCP boundary; the CLI's own stdout/stderr
    keeps its color for direct terminal use."""
    group = click.Group(commands={"sample-ansi": _sample_ansi})
    _, commands = build_tools(group)

    ok, text = invoke_command("sample-ansi", commands["sample-ansi"], {})

    assert ok is True
    assert "\x1b" not in text
    assert "[0;31m" not in text
    assert "CalledProcessError" in text


def test_synthetic_command_schema_and_dispatch():
    group = click.Group(commands={"sample": _sample})
    tools, commands = build_tools(group)

    assert len(tools) == 1
    schema = tools[0].input_schema
    assert schema["properties"]["count"]["type"] == "integer"
    assert schema["properties"]["flag"]["type"] == "boolean"
    assert schema["properties"]["tag"]["type"] == "array"
    assert schema["required"] == ["count"]

    ok, text = invoke_command("sample", commands["sample"], {"count": 3, "tag": ["a", "b"]})
    assert ok is True
    assert text == "count=3 flag=False tags=['a', 'b']"
