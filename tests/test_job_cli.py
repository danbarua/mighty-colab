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

"""Regression tests for defects that only live runs exposed.

Every test below pins something that a green unit suite happily allowed
while the feature was broken on a real VM. They are cheap; the bugs were
not -- each of the first three cost a provisioned VM to find.
"""

import json
import re
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from colab_cli.cli import app
from colab_cli.job import payload_bundle
from colab_cli.job.models import (
    Accelerator,
    ArtifactItem,
    Budgets,
    CodeSpec,
    JobSpec,
    Offload,
    Phase,
    RetryClass,
    Workload,
)

@pytest.fixture(autouse=True)
def isolated_job_store(tmp_path, mock_common_state):
    """Point the job store at tmp_path.

    `state` is a MagicMock, so `state.config_path` auto-vivifies into
    something `Path()` accepts as the empty string -- which lands job
    directories in a stray `./jobs` inside the repo, and lets one test's
    junk envelope become the next test's input. Pin a real path instead.
    """
    mock_common_state.config_path = str(tmp_path / "cfg" / "sessions.json")
    return mock_common_state


def _json_mode(state_mock):
    """`--json` is read from the patched singleton, not the CLI flag.

    `cli.py` binds `state` at import time, so its callback writes the flag
    onto the real object while the command reads the mock. Tests set the
    flag where the command will look for it -- the same convention
    conftest documents when it pins `json_output = False`.
    """
    state_mock.json_output = True


runner = CliRunner()
ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def _clean(text: str) -> str:
    return ANSI.sub("", text)


# --------------------------------------------------------------------------
# Staging: the Contents API does not create parent directories
# --------------------------------------------------------------------------


def test_upload_creates_the_parent_directory_first(tmp_path):
    """A PUT into a directory that does not exist returns a bare HTTP 500,
    which the client attributes to the upload-size limit -- so this failed
    live on a 222-byte file with the message "consider reducing the file
    size". The parent must be created first."""
    local = tmp_path / "x.py"
    local.write_text("pass\n")
    client = MagicMock()
    calls = []
    client.makedirs.side_effect = lambda p, **kw: calls.append(("mkdir", p))
    client.upload.side_effect = lambda lp, rp, **kw: calls.append(("upload", rp))

    payload_bundle._upload_checked(client, local, "/content/jobs/j/pkg/x.py")

    assert calls == [
        ("mkdir", "/content/jobs/j/pkg"),
        ("upload", "/content/jobs/j/pkg/x.py"),
    ], "makedirs must precede the upload, on the file's own parent"


def test_directory_cache_does_not_leak_between_staging_passes(tmp_path):
    """The cache must be per-pass. A module-level one would skip `makedirs`
    for a *second* VM later in the same process -- and a retry provisions a
    new VM, so the skip resurfaces as that same misleading 500."""
    local = tmp_path / "x.py"
    local.write_text("pass\n")

    first, second = MagicMock(), MagicMock()
    pass_one: set = set()
    payload_bundle._upload_checked(first, local, "/content/jobs/j/pkg/x.py", pass_one)
    payload_bundle._upload_checked(first, local, "/content/jobs/j/pkg/y.py", pass_one)
    # Same pass, same parent: created once.
    assert first.makedirs.call_count == 1

    pass_two: set = set()
    payload_bundle._upload_checked(second, local, "/content/jobs/j/pkg/x.py", pass_two)
    assert second.makedirs.call_count == 1, (
        "a fresh staging pass against a different VM must re-create the "
        "directory, not trust a cache from the previous one"
    )


# --------------------------------------------------------------------------
# Escapee detection: the watchdog is ours, not an escapee
# --------------------------------------------------------------------------


def test_escapee_sweep_excludes_our_own_watchdog(monkeypatch, tmp_path):
    """The watchdog inherits MIGHTY_JOB_ID by design and is alive when the
    verdict is written. Before this exclusion every single job reported a
    surviving descendant -- and an alarm that fires every time is one
    operators learn to ignore, which costs the real escapee it exists to
    catch."""
    from colab_cli.job.runtime_payload import ident

    monkeypatch.setattr(ident, "_LINUX", True)
    proc = tmp_path / "proc"
    for pid, tag in ((100, "job-A"), (200, "job-A"), (300, "other")):
        d = proc / str(pid)
        d.mkdir(parents=True)
        (d / "environ").write_bytes(f"MIGHTY_JOB_ID={tag}\0".encode())

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ident.os, "listdir", lambda p: ["100", "200", "300"])
    monkeypatch.setattr(ident.os, "getpid", lambda: 999)

    real_open = open

    def fake_open(path, *a, **kw):
        return real_open(str(path).replace("/proc", str(proc)), *a, **kw)

    monkeypatch.setattr("builtins.open", fake_open)

    assert sorted(ident.tagged_processes("job-A")) == [100, 200]
    assert ident.tagged_processes("job-A", exclude={200}) == [100]


# --------------------------------------------------------------------------
# Envelope: `--json` must validate, or the diagnostics never reach anyone
# --------------------------------------------------------------------------


def test_plan_json_on_an_invalid_spec_emits_diagnostics(tmp_path, mock_common_state):
    """`EnvelopeBase` forbids extra keys, so an emitter without its own
    model raises inside the error handler and the caller gets a generic
    envelope instead of the diagnostics -- which are the entire value of
    `plan`."""
    spec = tmp_path / "bad.yaml"
    spec.write_text("name: x\ncode:\n  entry: /absolute/train.py\n")
    _json_mode(mock_common_state)

    result = runner.invoke(app, ["job", "plan", str(spec)])

    assert result.exit_code == 1
    payload = json.loads(_clean(result.output).strip().splitlines()[-1])
    assert payload["command"] == "job plan"
    assert payload["diagnostics"], "the diagnostics must survive into the envelope"
    assert payload["diagnostics"][0]["code"] == "spec_invalid"


def test_plan_json_carries_the_job_id_so_apply_can_be_chained(
    tmp_path, mock_common_state
):
    """Without this an agent has to scrape the human-readable line to learn
    the id of the thing `plan` just created."""
    (tmp_path / "train.py").write_text("print(1)\n")
    spec = tmp_path / "ok.yaml"
    spec.write_text(
        "name: ok\naccelerator:\n  prefer: []\n  accept_cpu: true\n"
        "code:\n  kind: file\n  entry: train.py\n"
    )

    _json_mode(mock_common_state)

    result = runner.invoke(app, ["job", "plan", str(spec), "--no-probe"])

    assert result.exit_code == 0
    payload = json.loads(_clean(result.output).strip().splitlines()[-1])
    assert payload["job_id"].startswith("ok-")
    assert payload["spec_hash"]


# --------------------------------------------------------------------------
# Apply refuses a plan whose spec was edited after planning
# --------------------------------------------------------------------------


def test_apply_refuses_a_plan_whose_spec_hash_no_longer_matches(
    tmp_path, mock_common_state
):
    """A plan is a durable file that can be hand-edited between `plan` and
    `apply`. `apply` does not re-run the plan-time gates, so a tampered
    plan would otherwise smuggle an unknown accelerator or an escaping
    path straight past them."""
    from colab_cli.job.models import Plan

    plan = Plan(
        job_id="tampered",
        spec_hash="0" * 64,  # not the hash of the spec below
        created_at="now",
        spec=JobSpec(
            name="tampered",
            code=CodeSpec(kind="file", entry="train.py"),
            accelerator=Accelerator(prefer=[], accept_cpu=True),
            budgets=Budgets(wall_clock=60),
        ),
    )
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(plan.model_dump_json())

    result = runner.invoke(app, ["job", "apply", str(plan_file)])

    assert result.exit_code == 1
    out = _clean(result.output)
    assert "its own recorded hash" in out
    assert "job plan" in out, "must tell the caller how to recover"


def test_apply_accepts_a_plan_whose_spec_is_untouched(tmp_path, mock_common_state):
    """The guard must not reject honest plans -- otherwise the first thing
    anyone does is delete it."""
    from colab_cli.job.models import Plan
    from colab_cli.job.spec_io import spec_hash

    spec = JobSpec(
        name="honest",
        code=CodeSpec(kind="file", entry="train.py"),
        accelerator=Accelerator(prefer=[], accept_cpu=True),
        budgets=Budgets(wall_clock=60),
    )
    plan = Plan(
        job_id="honest",
        spec_hash=spec_hash(spec),
        created_at="now",
        spec=spec,
    )
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(plan.model_dump_json())

    result = runner.invoke(app, ["job", "apply", str(plan_file)])

    assert "its own recorded hash" not in _clean(result.output)


# --------------------------------------------------------------------------
# Stage failures are not code failures
# --------------------------------------------------------------------------


def _orch(tmp_path, spec):
    from colab_cli.job.models import Plan
    from colab_cli.job.orchestrator import Orchestrator
    from colab_cli.job.store import JobStore

    return Orchestrator(
        plan=Plan(job_id="j", spec_hash="h", created_at="now", spec=spec),
        store=JobStore(tmp_path / "jobs"),
        client=MagicMock(),
        runtime_factory=lambda u, t: MagicMock(),
        transport_factory=lambda s: MagicMock(),
        session_store=MagicMock(),
    )


def test_a_stage_failure_is_not_classified_as_a_code_bug(tmp_path):
    """Telling an agent to `fix_code` when a signed URL expired sends it
    editing a script that was never wrong."""
    spec = JobSpec(
        name="s",
        code=CodeSpec(kind="file", entry="t.py"),
        accelerator=Accelerator(prefer=[], accept_cpu=True),
    )
    orch = _orch(tmp_path, spec)

    orch._absorb_result({"workload": "failed", "exit_code": 1, "phase": "stage"})

    # `fix_human`, not `refresh_urls`: the runner redacts the error text
    # (it can embed a signed query string), so the surviving evidence
    # cannot separate an expired signature from a checksum mismatch --
    # and re-signing does not fix the latter.
    assert orch.env.retry_class is RetryClass.FIX_HUMAN
    assert orch.env.phase is Phase.STAGE
    assert "consumer never started" in orch.env.reason


def test_a_run_failure_is_still_a_code_bug(tmp_path):
    spec = JobSpec(
        name="s",
        code=CodeSpec(kind="file", entry="t.py"),
        accelerator=Accelerator(prefer=[], accept_cpu=True),
    )
    orch = _orch(tmp_path, spec)

    orch._absorb_result({"workload": "failed", "exit_code": 1, "phase": "run"})

    assert orch.env.retry_class is RetryClass.FIX_CODE


def test_offload_failure_keeps_its_own_retry_class_over_the_generic_one(tmp_path):
    spec = JobSpec(
        name="s",
        code=CodeSpec(kind="file", entry="t.py"),
        accelerator=Accelerator(prefer=[], accept_cpu=True),
        artifacts=[ArtifactItem(path="/content/out/m.pt", url="https://x/m.pt")],
    )
    orch = _orch(tmp_path, spec)

    orch._absorb_result(
        {
            "workload": "succeeded",
            "exit_code": 0,
            "phase": "offload",
            "artifacts": [
                {"path": "/content/out/m.pt", "url_id": "https://x/m.pt#a1", "status": "missing"}
            ],
        }
    )

    assert orch.env.offload is Offload.FAILED
    assert orch.env.workload is Workload.SUCCEEDED
    assert not orch.env.ok


# --------------------------------------------------------------------------
# MCP: a flattened leaf must actually dispatch, not merely be listed
# --------------------------------------------------------------------------


def test_mcp_exposes_job_leaves_and_excludes_the_blocking_one():
    import typer.main

    from colab_cli.mcp_server import build_tools

    tools, commands = build_tools(typer.main.get_command(app))
    names = {t.name for t in tools}

    assert {"job_plan", "job_status", "job_destroy", "job_list"} <= names
    assert "job" not in names, "the bare group is a parameterless, useless tool"
    assert "job_apply" not in names, (
        "apply blocks for the job's whole wall_clock -- hours -- which is the "
        "same failure mode `log --follow` is excluded for"
    )


def test_mcp_can_actually_invoke_a_nested_leaf(mock_common_state):
    """Listing a tool is not the same as dispatching it: a nested leaf binds
    its parameters through a Context built on a foreign (vendored) Click
    class, and that had never been exercised."""
    import typer.main

    from colab_cli.mcp_server import build_tools, invoke_command

    _, commands = build_tools(typer.main.get_command(app))
    ok, text = invoke_command("job_list", commands["job_list"], {})

    assert ok, f"job_list failed to dispatch: {text}"


# --------------------------------------------------------------------------
# Spec format traps
# --------------------------------------------------------------------------


def test_retry_when_is_usable_from_yaml(tmp_path):
    """YAML 1.1 resolves a bare `on:` key to boolean True, so a field named
    `on` cannot be written in the format we ship. Pins the rename."""
    from colab_cli.job.spec_io import load_spec

    spec_file = tmp_path / "s.yaml"
    (tmp_path / "train.py").write_text("print(1)\n")
    spec_file.write_text(
        "name: r\naccelerator:\n  prefer: []\n  accept_cpu: true\n"
        "code:\n  kind: file\n  entry: train.py\n"
        "retry:\n  when: [retry_same]\n  max_attempts: 2\n"
    )

    spec = load_spec(str(spec_file))

    assert spec.retry.when == [RetryClass.RETRY_SAME]
    assert spec.retry.max_attempts == 2


def test_the_shipped_example_spec_still_plans_clean():
    """The first thing the labkit team copies. It planned with three
    errors once, then with three warnings once -- and warnings are not
    cosmetic here: `apply` refuses them without `ignore_warnings: true`,
    so a warning in the example is as blocking as an error. Assert both."""
    from colab_cli.job.planner import build_plan
    from colab_cli.job.spec_io import load_spec

    spec = load_spec("examples/job/train_cls.yaml")
    plan = build_plan(spec, "example-check", probe=False)

    assert not plan.diagnostics, (
        "the shipped example must plan with no errors AND no warnings: "
        f"{[(d.severity, d.code) for d in plan.diagnostics]}"
    )


def test_the_suite_does_not_write_job_records_into_the_repo():
    """`state` is a MagicMock and `state.config_path` satisfies
    `os.fspath`, so an unpinned value lands job directories in the repo
    root -- and one test's leftover envelope becomes the next test's
    input. conftest pins it; this fails loudly if that pin is ever lost."""
    from pathlib import Path as _Path

    assert not _Path("MagicMock").exists()
    assert not _Path("jobs").exists()
