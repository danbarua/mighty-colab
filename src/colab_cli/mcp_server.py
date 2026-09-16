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

import asyncio
import contextlib
import io
import json
from typing import Any, Dict, List, Optional, Tuple

import click
import typer
import mcp.types as types
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.stdio import stdio_server

from colab_cli.common import _strip_ansi
from colab_cli.job.models import Workload

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
    "pay",  # user-facing accounts + billing
    # `job apply` blocks for the job's entire wall_clock -- hours -- and
    # returns output only at the very end. That is exactly the failure
    # mode `log --follow` is excluded for below. An agent drives a job
    # through `job_plan` then polls `job_status`, which is the shape the
    # supervisor was designed for anyway.
    "job_apply",
}


def _is_exposable(name: str, cmd: click.Command) -> bool:
    return name not in EXCLUDED_COMMANDS and not cmd.hidden


# Parameters excluded from specific, otherwise-exposed commands -- unlike
# EXCLUDED_COMMANDS, the rest of the command is still a perfectly good tool.
# `log --follow` is the motivating case: it blocks the single MCP call for
# the entire (potentially unbounded) duration of a background exec-async
# job and only returns output once, at the very end, not incrementally.
# That's not "streaming" so much as "one call that might never return" --
# and MCP's plain request/response tool-call model (which Claude Desktop
# expects) has no way to represent it. `exec-async` itself stays exposed;
# it's the non-blocking way to kick off exactly this kind of long job.
EXCLUDED_PARAMS: Dict[str, set] = {
    "log": {"follow"},
}


def _command_params(cmd: click.Command) -> List[click.Parameter]:
    # Click auto-adds an eager `--help`/`-h` option to every command; it's
    # not a real tool argument.
    excluded = EXCLUDED_PARAMS.get(cmd.name or "", ())
    return [p for p in cmd.params if p.name and p.name != "help" and p.name not in excluded]


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


def _iter_exposable(
    click_group: click.Group, prefix: str = ""
) -> List[Tuple[str, click.Command]]:
    """Walk the command tree, flattening sub-groups into `group_sub` names.

    MCP has no notion of a command hierarchy: a tool name is a flat string.
    A `click.Group` exposed directly would be a tool with no parameters and
    no way to say which subcommand you meant, so groups are flattened into
    one tool per leaf (`job` + `plan` -> `job_plan`).

    Dispatch needs no special handling for these: `invoke_command` builds a
    `click.Context` around whichever `Command` object it is handed, and a
    leaf inside a group is an ordinary `Command`.
    """
    found: List[Tuple[str, click.Command]] = []
    for name, cmd in sorted(click_group.commands.items()):
        if not _is_exposable(name, cmd):
            continue
        # Duck-typed, NOT `isinstance(cmd, click.Group)`: Typer's
        # `TyperGroup` does not subclass `click.Group` in the pinned
        # version (its MRO is TyperGroup -> typer._click.core.Command),
        # so the isinstance check silently matches nothing and the group
        # ships as one useless parameterless tool. Verified against the
        # actual MRO rather than assumed.
        sub_commands = getattr(cmd, "commands", None)
        if sub_commands:
            for sub_name, sub_cmd in sorted(sub_commands.items()):
                flat = f"{prefix}{name}_{sub_name}"
                # Exclusions are matched against the FLATTENED name: the
                # leaf's own name is ambiguous ("apply", "list") and would
                # either miss the exclusion or blanket-exclude an unrelated
                # top-level command that happens to share it.
                if flat in EXCLUDED_COMMANDS or sub_cmd.hidden:
                    continue
                found.append((flat, sub_cmd))
            continue
        found.append((f"{prefix}{name}", cmd))
    return found


def build_tools(click_group: click.Group) -> Tuple[List[types.Tool], Dict[str, click.Command]]:
    """Scan a Click group and build an MCP tool per exposable leaf command.

    Returns the tool list (for `tools/list`) alongside a name -> Command
    lookup used to dispatch `tools/call` requests.
    """
    tools: List[types.Tool] = []
    commands: Dict[str, click.Command] = {}

    for name, cmd in _iter_exposable(click_group):
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



# `job://<job_id>` resources -- one per local job record, content is the
# JobEnvelope JSON already used by `job status --json`. Terminal-only
# subscriptions (issue #55): a client that subscribes gets exactly one
# `notifications/resources/updated` when the job's envelope reaches `done`,
# not one per phase transition -- that's what an agent actually blocks on,
# and matches the protocol version this server negotiates (<= 2025-11-25,
# `resources/subscribe`/`resources/unsubscribe`, not the 2026-07-28
# `subscriptions/listen` streaming form).
JOB_URI_PREFIX = "job://"


def _job_uri(job_id: str) -> str:
    return f"{JOB_URI_PREFIX}{job_id}"


def _job_id_from_uri(uri: str) -> Optional[str]:
    if not uri.startswith(JOB_URI_PREFIX):
        return None
    job_id = uri[len(JOB_URI_PREFIX):]
    return job_id or None


JOB_LOGS_SUFFIX = "/logs"


def _job_logs_uri(job_id: str) -> str:
    return f"{_job_uri(job_id)}{JOB_LOGS_SUFFIX}"


def _job_id_from_logs_uri(uri: str) -> Optional[str]:
    if not uri.startswith(JOB_URI_PREFIX) or not uri.endswith(JOB_LOGS_SUFFIX):
        return None
    job_id = uri[len(JOB_URI_PREFIX) : -len(JOB_LOGS_SUFFIX)]
    return job_id or None


JOBS_LIST_URI = "jobs://"
JOBS_RUNNING_URI = "jobs://running"
JOBS_DONE_URI = "jobs://done"


def list_job_resources(store) -> List[types.Resource]:
    resources = [
        types.Resource(
            uri=JOBS_LIST_URI,
            name="jobs",
            description="All local job records (same rows as `mighty-colab jobs list --json`)",
            mime_type="application/json",
        ),
        types.Resource(
            uri=JOBS_RUNNING_URI,
            name="jobs (running)",
            description="Local job records not yet done (same rows as `mighty-colab jobs list --running --json`)",
            mime_type="application/json",
        ),
        types.Resource(
            uri=JOBS_DONE_URI,
            name="jobs (done)",
            description="Local job records that have finished (same rows as `mighty-colab jobs list --done --json`)",
            mime_type="application/json",
        ),
    ]
    for job_id in store.list_jobs():
        env = store.read_envelope(job_id)
        if env is None:
            status = "planned, not applied"
        elif env.done:
            status = "done"
        else:
            status = "running"
        resources.append(
            types.Resource(
                uri=_job_uri(job_id),
                name=job_id,
                description=f"mighty-colab job ({status})",
                mime_type="application/json",
            )
        )
        resources.append(
            types.Resource(
                uri=_job_logs_uri(job_id),
                name=f"{job_id} (logs)",
                description="Locally synced runner.log for this job (pulled on every "
                "healthy poll tick while apply/status --poll is active)",
                mime_type="text/plain",
            )
        )
    return resources


def read_jobs_list_resource(store, uri: str = JOBS_LIST_URI) -> types.ReadResourceResult:
    """`jobs://`, `jobs://running`, `jobs://done` -- every local job
    record, or filtered to just the ones not yet done / just the ones
    that have finished. Same rows and same source (`_job_list_rows`) as
    `jobs list [--running|--done] --json`: one row builder and one
    filter, so these can never drift thinner than the CLI or from each
    other.

    Not subscribable, unlike `job://<id>`: this resource's content
    changes on every job's every phase transition plus every prune,
    far too often to sensibly notify on. `job://<id>`'s single terminal
    `done` transition is the thing worth pushing; these are for an
    agent to read on demand.
    """
    from colab_cli.commands.job import _job_list_rows

    rows = _job_list_rows(store)
    if uri == JOBS_RUNNING_URI:
        rows = [r for r in rows if not r["done"]]
    elif uri == JOBS_DONE_URI:
        rows = [r for r in rows if r["done"]]
    return types.ReadResourceResult(
        contents=[
            types.TextResourceContents(
                uri=uri,
                mime_type="application/json",
                text=json.dumps(rows),
            )
        ]
    )



def read_job_resource(store, uri: str) -> types.ReadResourceResult:
    job_id = _job_id_from_uri(uri)
    if job_id is None:
        raise ValueError(f"not a job:// resource: {uri}")
    env = store.read_envelope(job_id)
    if env is None:
        raise ValueError(
            f"no envelope for job {job_id!r} -- planned but never applied, "
            f"or the job_id doesn't exist"
        )
    return types.ReadResourceResult(
        contents=[
            types.TextResourceContents(
                uri=uri,
                mime_type="application/json",
                text=env.model_dump_json(),
            )
        ]
    )


def read_job_logs_resource(store, uri: str) -> types.ReadResourceResult:
    """`job://<id>/logs` -- the locally synced `runner.log` for one job.

    Reads the same file `Orchestrator.poll()` / `status --poll` already
    pull to `store.job_dir(job_id) / RUNNER_LOG_FILE` on every healthy
    poll tick -- no new sync mechanism, this just exposes what's already
    on disk. Empty text (not an error) if the job exists but nothing has
    synced yet, e.g. the job was only just applied.
    """
    from colab_cli.job.store import RUNNER_LOG_FILE

    job_id = _job_id_from_logs_uri(uri)
    if job_id is None:
        raise ValueError(f"not a job://<id>/logs resource: {uri}")
    if store.read_envelope(job_id) is None and not store.job_dir(job_id).exists():
        raise ValueError(
            f"no envelope for job {job_id!r} -- planned but never applied, "
            f"or the job_id doesn't exist"
        )
    log_path = store.job_dir(job_id) / RUNNER_LOG_FILE
    text = log_path.read_text() if log_path.exists() else ""
    return types.ReadResourceResult(
        contents=[
            types.TextResourceContents(uri=uri, mime_type="text/plain", text=text)
        ]
    )


class JobResourceSubscriptions:
    """One background task per subscribed `job://` URI, polling for `done`.

    `JobStore.write_envelope` writes via atomic rename (see store.py), so
    polling `read_envelope` mid-write is safe -- readers only ever see a
    complete prior version or a complete new one, never a partial file.
    """

    def __init__(self, store, poll_interval: float = 2.0):
        self._store = store
        self._poll_interval = poll_interval
        self._tasks: Dict[str, "asyncio.Task"] = {}

    async def subscribe(self, session, uri: str) -> None:
        # Caught live: a client subscribed to `jobs://` because it's
        # listed right alongside subscribable `job://<id>` resources with
        # no way to know in advance which support it. Raising here was a
        # dead end in practice: the observed client marks the
        # subscription "succeeded" in its own bookkeeping regardless of
        # whether the server actually confirmed it, so an error was just
        # log noise with no visible effect. Accept it silently instead --
        # no watch task, no notification ever, but no error either. It's
        # still true that content changing on every job's every phase
        # transition and every prune is too often to sensibly notify on;
        # this just declines quietly rather than loudly.
        if uri in (JOBS_LIST_URI, JOBS_RUNNING_URI, JOBS_DONE_URI) or uri.endswith(
            JOB_LOGS_SUFFIX
        ):
            return
        job_id = _job_id_from_uri(uri)
        if job_id is None:
            raise ValueError(f"not a job:// resource: {uri}")
        # Once done=True it never changes again (envelopes are immutable
        # once terminal) -- if the job was already done before this
        # subscribe, there is no future "change" to report. Firing
        # anyway forces every client through the same "is this actually
        # new, or just a reconnect echo?" disambiguation on every single
        # reconnect, for every already-known-terminal job it happens to
        # be subscribed to (observed live: a client burning several
        # turns re-confirming "old data" on reconnect instead of
        # tracking the one thing that actually changed). A client that
        # wants the current state of an already-done job can just read()
        # it -- that's what the notification handler already points to.
        existing = self._store.read_envelope(job_id)
        if existing is not None and existing.done:
            return
        # Baseline for the workload-transition notification below: only
        # fire it for a transition observed *after* subscribing, not for
        # a job that was already past pending before this subscribe
        # existed (same "only transitions, never discovery" rule as the
        # done check above).
        initial_workload = existing.workload if existing is not None else Workload.PENDING
        # Idempotent: a re-subscribe on an already-watched URI restarts
        # cleanly rather than leaking a second task racing the first.
        await self.unsubscribe(uri)
        self._tasks[uri] = asyncio.create_task(
            self._watch(session, uri, job_id, initial_workload)
        )

    async def unsubscribe(self, uri: str) -> None:
        task = self._tasks.pop(uri, None)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def unsubscribe_all(self) -> None:
        for uri in list(self._tasks):
            await self.unsubscribe(uri)

    async def _watch(
        self, session, uri: str, job_id: str, initial_workload: Workload
    ) -> None:
        # A poll loop is exactly what a subscription exists to replace --
        # an agent burning tool-calls/turns on `jobs list --running` just
        # to learn whether staging finished, before `run` has even
        # started, is the same "sleeping while waiting" problem `--poll`
        # already had for the pre-launch phases (see the never_started
        # fix). workload leaving pending answers the actually load-
        # bearing question -- staging succeeded (now running) or failed
        # outright -- without waiting for the job's own, possibly
        # hours-later, terminal `done`.
        # One notification per meaningful, separately-observed event --
        # not one per condition. Checking `done` first and returning
        # immediately means a job that goes straight from pending to a
        # terminal state in a single tick (e.g. staging fails before the
        # consumer ever ran) fires exactly once, not twice: the workload-
        # transition check below is only ever reached on a tick where the
        # job is *not yet* done, so pending -> running -> done (the
        # normal path, observed as two separate ticks) still fires twice.
        workload_notified = initial_workload is not Workload.PENDING
        try:
            while True:
                env = self._store.read_envelope(job_id)
                if env is not None:
                    if env.done:
                        await session.send_resource_updated(uri)
                        return
                    if not workload_notified and env.workload is not Workload.PENDING:
                        await session.send_resource_updated(uri)
                        workload_notified = True
                await asyncio.sleep(self._poll_interval)
        finally:
            self._tasks.pop(uri, None)


class JobListWatcher:
    """Fires `notifications/resources/list_changed` whenever the set of
    local job records changes (a new job appears, or one is pruned).

    Closes a real gap: a client that auto-subscribes to every resource it
    discovers via `resources/list` only ever discovers the jobs that
    existed at connect time. A job created mid-session (a fresh `job
    apply`, run by a completely separate process this server has no
    other visibility into) would otherwise run to completion with nobody
    watching it -- observed live: a job finished with `done=True` and no
    notification ever fired, because nothing told the client a new
    subscribable resource had appeared for it to subscribe to.

    One instance per server connection, not per-URI like
    `JobResourceSubscriptions` -- there is exactly one job *list* to
    watch, however many individual jobs exist within it.
    """

    def __init__(self, store, poll_interval: float = 5.0):
        self._store = store
        self._poll_interval = poll_interval
        self._task: Optional["asyncio.Task"] = None
        self._known: Optional[set] = None

    def start(self, session) -> None:
        if self._task is not None:
            return
        self._known = set(self._store.list_jobs())
        self._task = asyncio.create_task(self._watch(session))

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _watch(self, session) -> None:
        while True:
            await asyncio.sleep(self._poll_interval)
            current = set(self._store.list_jobs())
            if current != self._known:
                self._known = current
                await session.send_resource_list_changed()



async def run_stdio_server(click_group: click.Group, server_name: str) -> None:
    """Start the MCP stdio server, exposing `click_group`'s commands as tools."""
    tools, commands = build_tools(click_group)
    tool_map = {t.name: t for t in tools}

    from colab_cli.commands.job import _store

    job_store = _store()
    subscriptions = JobResourceSubscriptions(job_store)
    job_list_watcher = JobListWatcher(job_store)

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

    async def on_list_resources(ctx, params) -> types.ListResourcesResult:
        # Started lazily on first list, not at server startup: this is
        # the first point a real ctx.session (and its live send_*
        # methods) is available at all.
        job_list_watcher.start(ctx.session)
        return types.ListResourcesResult(resources=list_job_resources(job_store))

    async def on_read_resource(ctx, params) -> types.ReadResourceResult:
        if params.uri in (JOBS_LIST_URI, JOBS_RUNNING_URI, JOBS_DONE_URI):
            return read_jobs_list_resource(job_store, params.uri)
        if params.uri.endswith(JOB_LOGS_SUFFIX):
            return read_job_logs_resource(job_store, params.uri)
        return read_job_resource(job_store, params.uri)

    async def on_subscribe_resource(ctx, params) -> types.EmptyResult:
        await subscriptions.subscribe(ctx.session, params.uri)
        return types.EmptyResult()

    async def on_unsubscribe_resource(ctx, params) -> types.EmptyResult:
        await subscriptions.unsubscribe(params.uri)
        return types.EmptyResult()

    server = Server(
        server_name,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
        on_list_resources=on_list_resources,
        on_read_resource=on_read_resource,
        on_subscribe_resource=on_subscribe_resource,
        on_unsubscribe_resource=on_unsubscribe_resource,
    )
    try:
        async with stdio_server() as (read_stream, write_stream):
            init_options = server.create_initialization_options(
                notification_options=NotificationOptions(resources_changed=True)
            )
            await server.run(read_stream, write_stream, init_options)
    finally:
        # Every background task -- per-job watches and the job-list
        # watcher alike -- must die with the server, or an asyncio.run()
        # that returns with orphaned tasks still scheduled logs "Task was
        # destroyed but it is pending" noise at minimum, and holds the
        # event loop open at worst.
        await subscriptions.unsubscribe_all()
        await job_list_watcher.stop()
