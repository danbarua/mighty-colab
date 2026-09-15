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
from colab_cli.client import Accelerator, AssignmentVariant, Shape
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
        machine_shape=Shape.STANDARD,
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


def test_exec_async_is_exposed(tools_and_commands):
    """exec-async returns almost instantly regardless of the submitted
    script's runtime -- it's the right shape for MCP's single request/
    response model (unlike log -f, which wants genuinely incremental
    updates). Must stay a tool."""
    tools, _ = tools_and_commands
    names = {t.name for t in tools}
    assert "exec-async" in names


def test_log_follow_is_not_an_mcp_tool_parameter(tools_and_commands):
    """log -f blocks a single MCP call for the entire duration of a
    background job (potentially unbounded) and only returns output once,
    at the very end -- not incrementally. Claude Desktop (and MCP's plain
    request/response tool-call model generally) doesn't support that. The
    rest of `log` (session listing, structured history, -n/-t/-o) is fast
    and bounded, so only the `follow` parameter is excluded, not the whole
    command."""
    tools, _ = tools_and_commands
    by_name = {t.name: t for t in tools}
    assert "log" in by_name
    assert "follow" not in by_name["log"].input_schema["properties"]
    # The rest of `log` must still work over MCP.
    assert "session" in by_name["log"].input_schema["properties"]
    assert "lines" in by_name["log"].input_schema["properties"]


def test_log_tail_is_exposed_over_mcp(tools_and_commands):
    """--tail is the MCP-safe replacement for --follow: a single bounded
    synchronous file read, no polling, no liveness wait -- it needs no
    entry in EXCLUDED_PARAMS at all, unlike --follow."""
    tools, _ = tools_and_commands
    by_name = {t.name: t for t in tools}
    assert "tail" in by_name["log"].input_schema["properties"]
    assert by_name["log"].input_schema["properties"]["tail"]["type"] == "boolean"


def test_build_kwargs_ignores_follow_even_if_a_client_sends_it(click_group):
    """Defense in depth: even if some MCP client sends `follow` anyway
    (schemas aren't always strictly enforced), it must never reach the
    `log` command's callback -- it must fall back to the callback's own
    `False` default rather than blocking the MCP call indefinitely."""
    from colab_cli.mcp_server import _build_kwargs

    log_cmd = click_group.commands["log"]
    with click.Context(log_cmd, info_name="log") as ctx:
        kwargs = _build_kwargs(log_cmd, {"session": "s1", "follow": True}, ctx)

    assert "follow" not in kwargs


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
    with (
        patch("colab_cli.auto_update._is_editable_install", return_value=False),
        patch("colab_cli.auto_update.installed_version") as mock_version,
    ):
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


def _job_store(tmp_path):
    from colab_cli.job.store import JobStore

    return JobStore(tmp_path / "jobs")


def _done_envelope(job_id):
    from colab_cli.job.models import Cleanup, JobEnvelope, Offload, Supervisor, Workload

    return JobEnvelope(
        job_id=job_id,
        workload=Workload.SUCCEEDED,
        exit_code=0,
        offload=Offload.OK,
        cleanup=Cleanup.RELEASED,
        supervisor=Supervisor.FINISHED,
    )



def test_job_uri_roundtrip():
    from colab_cli.mcp_server import _job_id_from_uri, _job_uri

    assert _job_id_from_uri(_job_uri("my-job-123")) == "my-job-123"
    assert _job_id_from_uri("https://example.com") is None
    assert _job_id_from_uri("job://") is None


def test_list_job_resources_reports_status(tmp_path):
    from colab_cli.job.models import JobEnvelope, Workload
    from colab_cli.mcp_server import list_job_resources

    store = _job_store(tmp_path)
    (store.job_dir("planned-only")).mkdir(parents=True)
    store.write_envelope(JobEnvelope(job_id="still-running", workload=Workload.RUNNING))
    store.write_envelope(_done_envelope("finished"))

    resources = {r.name: r for r in list_job_resources(store)}

    assert set(resources) == {
        "jobs",
        "jobs (running)",
        "jobs (done)",
        "planned-only",
        "still-running",
        "finished",
    }
    assert resources["jobs"].uri == "jobs://"
    assert resources["jobs (running)"].uri == "jobs://running"
    assert resources["jobs (done)"].uri == "jobs://done"
    assert resources["planned-only"].uri == "job://planned-only"
    assert "planned, not applied" in resources["planned-only"].description
    assert "running" in resources["still-running"].description
    assert "done" in resources["finished"].description
    assert all(r.mime_type == "application/json" for r in resources.values())


def test_read_jobs_list_resource_filters_running_and_done(tmp_path):
    """`jobs list --running`/`--done` exist as CLI filters; the resources
    must offer the same filtering, not just the unfiltered aggregate --
    an agent checking on several in-flight jobs at once shouldn't have
    to fetch everything and filter client-side."""
    import json

    from colab_cli.job.models import JobEnvelope, Workload
    from colab_cli.mcp_server import read_jobs_list_resource

    store = _job_store(tmp_path)
    (store.job_dir("planned-only")).mkdir(parents=True)
    store.write_envelope(JobEnvelope(job_id="still-running", workload=Workload.RUNNING))
    store.write_envelope(_done_envelope("finished"))

    running = json.loads(
        read_jobs_list_resource(store, "jobs://running").contents[0].text
    )
    done = json.loads(read_jobs_list_resource(store, "jobs://done").contents[0].text)

    assert {r["job_id"] for r in running} == {"planned-only", "still-running"}
    assert {r["job_id"] for r in done} == {"finished"}


def test_read_jobs_list_resource_matches_job_list_rows(tmp_path):
    """One row builder for `jobs list --json` and `jobs://` -- this locks
    the two together so `jobs://` can never drift thinner than the CLI.
    """
    import json

    from colab_cli.commands.job import _job_list_rows
    from colab_cli.job.models import JobEnvelope, Workload
    from colab_cli.mcp_server import read_jobs_list_resource

    store = _job_store(tmp_path)
    (store.job_dir("planned-only")).mkdir(parents=True)
    store.write_envelope(JobEnvelope(job_id="still-running", workload=Workload.RUNNING))
    store.write_envelope(_done_envelope("finished"))

    result = read_jobs_list_resource(store)

    assert len(result.contents) == 1
    content = result.contents[0]
    assert content.uri == "jobs://"
    assert content.mime_type == "application/json"
    rows = json.loads(content.text)
    assert rows == _job_list_rows(store)
    by_id = {r["job_id"]: r for r in rows}
    assert by_id["planned-only"]["workload"] is None
    assert by_id["still-running"]["workload"] == "running"
    assert by_id["finished"]["done"] is True
    # The fields the plain-text `jobs list` already showed -- and the
    # JSON row previously didn't -- must be present.
    assert "offload" in by_id["finished"]
    assert "cleanup" in by_id["finished"]



def test_read_job_resource_returns_envelope_json(tmp_path):
    import json

    from colab_cli.mcp_server import read_job_resource

    store = _job_store(tmp_path)
    store.write_envelope(_done_envelope("readable"))

    result = read_job_resource(store, "job://readable")

    assert len(result.contents) == 1
    content = result.contents[0]
    assert content.uri == "job://readable"
    assert content.mime_type == "application/json"
    payload = json.loads(content.text)
    assert payload["job_id"] == "readable"
    assert payload["workload"] == "succeeded"


def test_read_job_resource_raises_for_unknown_job(tmp_path):
    from colab_cli.mcp_server import read_job_resource

    store = _job_store(tmp_path)

    with pytest.raises(ValueError, match="no envelope"):
        read_job_resource(store, "job://does-not-exist")


def test_read_job_resource_raises_for_non_job_uri(tmp_path):
    from colab_cli.mcp_server import read_job_resource

    store = _job_store(tmp_path)

    with pytest.raises(ValueError, match="not a job:// resource"):
        read_job_resource(store, "https://example.com")


def test_subscription_fires_exactly_once_when_job_becomes_done(tmp_path):
    import asyncio

    from colab_cli.job.models import JobEnvelope, Workload
    from colab_cli.mcp_server import JobResourceSubscriptions

    store = _job_store(tmp_path)
    store.write_envelope(JobEnvelope(job_id="will-finish", workload=Workload.RUNNING))
    session = MagicMock()
    session.send_resource_updated = MagicMock(
        side_effect=lambda uri: asyncio.sleep(0)
    )
    subs = JobResourceSubscriptions(store, poll_interval=0.01)

    async def scenario():
        await subs.subscribe(session, "job://will-finish")
        await asyncio.sleep(0.03)
        assert session.send_resource_updated.call_count == 0
        store.write_envelope(_done_envelope("will-finish"))
        await asyncio.sleep(0.05)
        assert session.send_resource_updated.call_count == 1
        session.send_resource_updated.assert_called_once_with("job://will-finish")
        # The watch task ends itself once it fires -- nothing left running.
        assert subs._tasks == {}

    asyncio.run(scenario())


def test_unsubscribe_cancels_the_watch_task_before_it_fires(tmp_path):
    import asyncio

    from colab_cli.job.models import JobEnvelope, Workload
    from colab_cli.mcp_server import JobResourceSubscriptions

    store = _job_store(tmp_path)
    store.write_envelope(JobEnvelope(job_id="never-finishes", workload=Workload.RUNNING))
    session = MagicMock()
    session.send_resource_updated = MagicMock(
        side_effect=lambda uri: asyncio.sleep(0)
    )
    subs = JobResourceSubscriptions(store, poll_interval=0.01)

    async def scenario():
        await subs.subscribe(session, "job://never-finishes")
        await subs.unsubscribe("job://never-finishes")
        store.write_envelope(_done_envelope("never-finishes"))
        await asyncio.sleep(0.05)
        assert session.send_resource_updated.call_count == 0
        assert subs._tasks == {}

    asyncio.run(scenario())


def test_resubscribing_the_same_uri_does_not_leak_a_second_task(tmp_path):
    import asyncio

    from colab_cli.job.models import JobEnvelope, Workload
    from colab_cli.mcp_server import JobResourceSubscriptions

    store = _job_store(tmp_path)
    store.write_envelope(JobEnvelope(job_id="resubscribed", workload=Workload.RUNNING))
    session = MagicMock()
    session.send_resource_updated = MagicMock(
        side_effect=lambda uri: asyncio.sleep(0)
    )
    subs = JobResourceSubscriptions(store, poll_interval=0.01)

    async def scenario():
        await subs.subscribe(session, "job://resubscribed")
        await subs.subscribe(session, "job://resubscribed")
        assert len(subs._tasks) == 1
        await subs.unsubscribe_all()

    asyncio.run(scenario())


def test_subscribing_to_jobs_list_resource_gives_a_clear_not_subscribable_error(
    tmp_path,
):
    """Caught live: a client subscribed to jobs:// because it's listed
    right alongside subscribable job://<id> resources with no way to
    know in advance which support it. Must not read like a malformed-URI
    complaint -- the resource is real, it just doesn't notify."""
    import asyncio

    from colab_cli.mcp_server import JobResourceSubscriptions

    store = _job_store(tmp_path)
    subs = JobResourceSubscriptions(store)
    session = MagicMock()

    for uri in ("jobs://", "jobs://running", "jobs://done"):
        with pytest.raises(ValueError, match="does not support subscription"):
            asyncio.run(subs.subscribe(session, uri))
        assert subs._tasks == {}

