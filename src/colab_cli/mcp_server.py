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

"""Exposes a subset of the CLI's Click commands as MCP tools.

Hand-rolled rather than delegating to the `click-mcp` package: that package's
`Server.list_tools()`/`.call_tool()` decorator API doesn't exist in the
installed `mcp` SDK generation (rewritten around constructor-based
`on_list_tools`/`on_call_tool` handlers -- see `run_stdio_server` below), and
it has no supported way to exclude commands (the registry hook exists in its
source but nothing public ever calls it).

We also invoke each Click command's callback directly in-process (via
`Command.invoke`) rather than click-mcp's approach of round-tripping through
a synthesized argv and re-running `cli_group.main()`. That matters here: this
CLI's root `@app.callback()` sets `state.auth_provider`/`state.config_path`
from `--auth`/`--config`. Re-running the whole group's `main()` for every
tool call -- with no global flags in the synthesized argv -- would silently
reset both to their defaults on every single call.
"""

import contextlib
import io
import re
from typing import Any, Dict, List, Tuple

import click
import typer
import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

# Colab kernels format tracebacks with IPython's colored formatter, which
# embeds raw ANSI SGR escape bytes (e.g. \x1b[0;31m) in error output. Those
# are meaningless -- and hard to parse -- in a JSON text field read by an
# MCP client, even though they're exactly what a human wants in a real
# terminal. So we strip them only here, at the MCP boundary, leaving the
# CLI's own stdout/stderr (and a human's terminal experience) untouched.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)

# Commands that require a live human at a terminal (interactive shell, editor,
# TTY auth ceremony) or are internal-only. Never exposed as MCP tools, even
# though they're normal, visible CLI commands with their own --help.
EXCLUDED_COMMANDS = {
    "ssh",  # interactive shell / spawns a subprocess `ssh`
    "repl",  # interactive Python REPL
    "console",  # raw TTY passthrough (sets the terminal to raw mode)
    "edit",  # blocks on launching the local $EDITOR
    "drivemount",  # can block on /dev/tty for a Drive re-auth ceremony
    "mcp",  # the MCP server command itself
    "help",  # redundant with MCP's own tool discovery,
    "pay" # user-facing accounts + billing
}


def _is_exposable(name: str, cmd: click.Command) -> bool:
    return name not in EXCLUDED_COMMANDS and not cmd.hidden


def _command_params(cmd: click.Command) -> List[click.Parameter]:
    # Click auto-adds an eager `--help`/`-h` option to every command; it's
    # not a real tool argument.
    return [p for p in cmd.params if p.name and p.name != "help"]


def _is_array_param(param: click.Parameter) -> bool:
    return bool(getattr(param, "multiple", False)) or getattr(param, "nargs", 1) not in (
        1,
        None,
    )


_SCALAR_TYPES = {"boolean": "boolean", "integer": "integer", "float": "number"}


def _scalar_type(param: click.Parameter) -> str:
    # Typer vendors its own copy of Click's param-type classes
    # (`typer._click.types.*`, not `click.types.*`), so `isinstance` checks
    # against the real `click` module never match Typer-built commands.
    # `ParamType.name` ("text"/"integer"/"float"/"boolean"/...) is the
    # stable, public-facing identifier both hierarchies agree on -- it's
    # what Click already renders into `--help` (e.g. `--timeout FLOAT`).
    return _SCALAR_TYPES.get(param.type.name, "string")


def _param_schema(param: click.Parameter) -> Dict[str, Any]:
    """JSON Schema for one Click parameter.

    Loosely modeled on click-mcp's own `_get_parameter_info`, extended to
    handle `multiple=True` options and variadic (`nargs=-1`) arguments --
    e.g. `run`'s trailing `script_args` or `exec`'s repeatable `--env` --
    which click-mcp's version silently mis-typed as plain strings.
    """
    item_type = _scalar_type(param)
    is_array = _is_array_param(param)
    schema: Dict[str, Any] = (
        {"type": "array", "items": {"type": item_type}} if is_array else {"type": item_type}
    )

    choices = getattr(param.type, "choices", None)
    if choices:
        target = schema["items"] if is_array else schema
        target["enum"] = list(choices)

    if param.help:
        schema["description"] = param.help

    default = param.default
    if default is not None and not callable(default):
        if isinstance(default, (str, int, float, bool, list, dict)):
            schema["default"] = default

    return schema


def build_tools(click_group: click.Group) -> Tuple[List[types.Tool], Dict[str, click.Command]]:
    """Scan a flat Click group and build an MCP tool per exposable command.

    Returns the tool list (for `tools/list`) alongside a name -> Command
    lookup used to dispatch `tools/call` requests.
    """
    tools: List[types.Tool] = []
    commands: Dict[str, click.Command] = {}

    for name, cmd in sorted(click_group.commands.items()):
        if not _is_exposable(name, cmd):
            continue

        properties: Dict[str, Any] = {}
        required: List[str] = []
        for param in _command_params(cmd):
            properties[param.name] = _param_schema(param)
            if param.required:
                required.append(param.name)

        input_schema: Dict[str, Any] = {"type": "object", "properties": properties}

        if required:
            input_schema["required"] = sorted(required)

        description: str = (cmd.help or cmd.short_help or "")

        if name == "version":
            from colab_cli.auto_update import get_app_version
            description = f"Version: {get_app_version()}"

        tools.append(
            types.Tool(
                name=name,
                description=description,
                input_schema=input_schema,
            )
        )
        commands[name] = cmd

    return tools, commands


def _build_kwargs(
    cmd: click.Command, arguments: Dict[str, Any], ctx: click.Context
) -> Dict[str, Any]:
    kwargs: Dict[str, Any] = {}
    missing: List[str] = []

    for param in _command_params(cmd):
        if param.name in arguments:
            value = arguments[param.name]
            if _is_array_param(param):
                value = [param.type.convert(v, param, ctx) for v in value]
            else:
                value = param.type.convert(value, param, ctx)
            kwargs[param.name] = value
        elif param.required:
            missing.append(param.name)
        else:
            kwargs[param.name] = param.get_default(ctx)

    if missing:
        raise click.UsageError(f"Missing required argument(s): {', '.join(sorted(missing))}")

    return kwargs


def invoke_command(name: str, cmd: click.Command, arguments: Dict[str, Any]) -> Tuple[bool, str]:
    """Run one Click command's callback in-process. Returns (ok, text)."""
    output = io.StringIO()

    def captured() -> str:
        return _strip_ansi(output.getvalue()).strip()

    try:
        with click.Context(cmd, info_name=name) as ctx:
            kwargs = _build_kwargs(cmd, arguments, ctx)
            ctx.params = kwargs
            # Commands report errors via `typer.echo(..., err=True)` before
            # raising -- capture stderr too, or those messages are lost and
            # the caller sees only a bare exit code.
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                cmd.invoke(ctx)
    except typer.Exit as e:
        if e.exit_code not in (0, None):
            return False, captured() or f"[exit code {e.exit_code}]"
    except click.ClickException as e:
        return False, (captured() + "\n" + e.format_message()).strip()
    except Exception as e:
        return False, (captured() + f"\n{e}").strip()

    return True, captured()


async def run_stdio_server(click_group: click.Group, server_name: str) -> None:
    """Start the MCP stdio server, exposing `click_group`'s commands as tools."""
    tools, commands = build_tools(click_group)
    tool_map = {t.name: t for t in tools}

    async def on_list_tools(ctx, params) -> types.ListToolsResult:
        return types.ListToolsResult(tools=tools)

    async def on_call_tool(ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
        if params.name not in tool_map:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=f"Unknown tool: {params.name}")],
                is_error=True,
            )
        ok, text = invoke_command(params.name, commands[params.name], params.arguments or {})
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=text)],
            is_error=not ok,
        )

    server = Server(server_name, on_list_tools=on_list_tools, on_call_tool=on_call_tool)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())
