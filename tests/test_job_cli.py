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
import os
import re
import signal
import subprocess
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
    Control,
    ControlChannel,
    JobSpec,
    Offload,
    Phase,
    RetryClass,
    Workload,
)

@pytest.fixture(autouse=True)
def isolated_job_store(tmp_path, mock_common_state, monkeypatch):
    """Point the job store at tmp_path.

    `state` is a MagicMock, so `state.config_path` auto-vivifies into
    something `Path()` accepts as the empty string -- which lands job
    directories in a stray `./jobs` inside the repo, and lets one test's
    junk envelope become the next test's input. Pin a real path instead.
    """
    mock_common_state.config_path = str(tmp_path / "cfg" / "sessions.json")
    # `status()` respawns keep-alive (issue #54) whenever a session's
    # `keep_alive_pid` doesn't read as alive -- true by default for every
    # MagicMock session in this file, including tests that don't care
    # about keep-alive at all. Without this, spawn_keep_alive's real,
    # unmocked subprocess.Popen would actually fire in the background
    # during unrelated tests. A safe, deterministic default; tests that
    # care about the respawn itself override this explicitly.
    monkeypatch.setattr(
        "colab_cli.commands.session.spawn_keep_alive", lambda *a, **k: 424242
    )
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


def test_signal_identities_skips_a_reused_pid(monkeypatch):
    from colab_cli.job.runtime_payload import ident

    killed = []
    monkeypatch.setattr(ident.os, "kill", lambda pid, sig: killed.append(pid))
    monkeypatch.setattr(
        ident, "alive", lambda pid, started, boot: False
    )
    signaled = ident.signal_identities(
        [(100, "old-start", "boot")], signal.SIGKILL
    )
    assert signaled == []
    assert killed == []


def test_signal_identities_kills_only_the_matching_process(monkeypatch):
    from colab_cli.job.runtime_payload import ident

    killed = []

    def fake_alive(pid, started, boot):
        return pid == 100 and started == "t" and boot == "b"

    monkeypatch.setattr(ident.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    monkeypatch.setattr(ident, "alive", fake_alive)
    signaled = ident.signal_identities(
        [(100, "t", "b"), (101, "t", "b")], signal.SIGTERM
    )
    assert signaled == [100]
    assert killed == [(100, signal.SIGTERM)]



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


def test_plan_out_redacts_signed_urls_and_apply_uses_protected_sidecar(
    tmp_path, monkeypatch, mock_common_state
):
    from colab_cli.job.orchestrator import Orchestrator
    from colab_cli.job.store import plan_secrets_path

    sentinel = "ISSUE18_PLAN_SENTINEL"
    (tmp_path / "train.py").write_text("print(1)")
    spec = tmp_path / "job.yaml"
    spec.write_text(
        f"""name: secrets
accelerator:
  prefer: []
  accept_cpu: true
code:
  kind: file
  entry: train.py
data:
  - url: https://storage.example/input?signature={sentinel}
    dest: input.bin
    size_bytes: 1
"""
    )
    out = tmp_path / "explicit-plan.json"

    planned = runner.invoke(
        app, ["job", "plan", str(spec), "--no-probe", "--out", str(out)]
    )

    assert planned.exit_code == 0
    assert sentinel not in planned.output
    assert sentinel not in out.read_text()
    sidecar = plan_secrets_path(out)
    assert sentinel in sidecar.read_text()
    assert sidecar.stat().st_mode & 0o777 == 0o600

    captured = {}

    def fail_after_loading(self):
        captured["url"] = self.spec.data[0].url
        raise RuntimeError("stop before provisioning")

    monkeypatch.setattr(Orchestrator, "provision", fail_after_loading)
    mock_common_state.debug = False
    applied = runner.invoke(app, ["job", "apply", str(out)])

    assert applied.exit_code == 1
    assert captured["url"].endswith(sentinel)
    assert sentinel not in applied.output


def test_plan_debug_error_does_not_echo_signed_query(tmp_path):
    sentinel = "ISSUE18_DEBUG_SENTINEL"
    spec = tmp_path / "bad.yaml"
    spec.write_text(f"name: [https://storage.example/x?signature={sentinel}")

    result = runner.invoke(app, ["--debug", "job", "plan", str(spec)])

    assert result.exit_code == 1
    assert sentinel not in result.output
    assert sentinel not in repr(result.exception)


def test_plan_validation_location_does_not_echo_a_signed_mapping_key(tmp_path):
    sentinel = "ISSUE18_LOCATION_SENTINEL"
    spec = tmp_path / "bad-map.yaml"
    spec.write_text(
        "name: bad-map\naccelerator:\n  prefer: []\n  accept_cpu: true\n"
        "code:\n  kind: file\n  entry: train.py\n"
        f"data:\n  - https://storage.example/x?signature={sentinel}: input.bin\n"
    )

    human = runner.invoke(app, ["job", "plan", str(spec), "--no-probe"])
    structured = runner.invoke(
        app, ["--json", "job", "plan", str(spec), "--no-probe"]
    )

    assert human.exit_code == 1
    assert structured.exit_code == 1
    assert sentinel not in human.output
    assert sentinel not in structured.output


def test_apply_rejects_a_plan_with_its_credential_marker_removed(
    tmp_path, monkeypatch, mock_common_state
):
    from colab_cli.job.orchestrator import Orchestrator
    from colab_cli.job.store import plan_secrets_path

    (tmp_path / "train.py").write_text("print(1)")
    spec = tmp_path / "job.yaml"
    spec.write_text(
        "name: marker\naccelerator:\n  prefer: []\n  accept_cpu: true\n"
        "code:\n  kind: file\n  entry: train.py\n"
        "data:\n  - url: https://storage.example/input?signature=secret\n"
        "    dest: input.bin\n    size_bytes: 1\n"
    )
    out = tmp_path / "plan.json"
    assert runner.invoke(
        app, ["job", "plan", str(spec), "--no-probe", "--out", str(out)]
    ).exit_code == 0
    payload = json.loads(out.read_text())
    payload["spec"]["data"][0]["url"] = "https://storage.example/input"
    out.write_text(json.dumps(payload))
    plan_secrets_path(out).unlink()

    monkeypatch.setattr(
        Orchestrator,
        "provision",
        lambda _self: pytest.fail("tampered plan reached provisioning"),
    )
    result = runner.invoke(app, ["job", "apply", str(out)])

    assert result.exit_code == 1
    assert "its own recorded hash" in _clean(result.output)



def test_unconfirmed_credential_cleanup_overrides_leave_up(
    tmp_path, monkeypatch, mock_common_state
):
    import colab_cli.commands.job as job_command
    from colab_cli.job.models import Cleanup, Phase, RetryClass
    from colab_cli.job.orchestrator import Orchestrator, PhaseError
    from types import SimpleNamespace

    (tmp_path / "train.py").write_text("print(1)")
    spec = tmp_path / "job.yaml"
    spec.write_text(
        "name: cleanup\naccelerator:\n  prefer: []\n  accept_cpu: true\n"
            "code:\n  kind: file\n  entry: train.py\n"
        "data:\n  - url: https://storage.example/input?signature=secret\n"
        "    dest: input.bin\n    size_bytes: 1\n"
    )
    out = tmp_path / "plan.json"
    assert runner.invoke(
        app, ["job", "plan", str(spec), "--no-probe", "--out", str(out)]
    ).exit_code == 0
    forced = {}

    def provision(self):
        self.session_state = SimpleNamespace(name="cleanup", url="https://vm", token="x")
        self.env.endpoint = "m-test"

    def launch(_self, _path):
        raise PhaseError(Phase.RUN, "launch failed", RetryClass.RETRY_SAME)

    def cleanup(self, force_leave_up=False):
        forced["leave_up"] = force_leave_up
        self.env.cleanup = Cleanup.FAILED

    monkeypatch.setattr(Orchestrator, "provision", provision)
    for name in ("install", "restart", "verify", "seal_secret_channel"):
        monkeypatch.setattr(Orchestrator, name, lambda _self: None)
    monkeypatch.setattr(job_command, "_stage_payload", lambda _orch, _plan: None)
    monkeypatch.setattr(Orchestrator, "launch", launch)
    monkeypatch.setattr(Orchestrator, "cleanup_secret_channel", lambda _self: False)
    monkeypatch.setattr(Orchestrator, "cleanup", cleanup)

    result = runner.invoke(app, ["job", "apply", str(out), "--leave-up"])

    assert result.exit_code == 1
    assert forced == {"leave_up": False}
    assert "transfer credential deletion could not be confirmed" in _clean(result.output)

def test_apply_refuses_a_plan_whose_spec_hash_no_longer_matches(
    tmp_path, mock_common_state
):
    """A durable plan must refuse a spec edited after planning."""
    from colab_cli.job.models import Plan

    plan = Plan(
        job_id="tampered",
        spec_hash="0" * 64,
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
    from colab_cli.job.payload_bundle import collect_source_files
    from colab_cli.job.spec_io import plan_hash

    (tmp_path / "train.py").write_text("print(1)\n")
    spec = JobSpec(
        name="honest",
        code=CodeSpec(kind="file", root=str(tmp_path), entry="train.py"),
        accelerator=Accelerator(prefer=[], accept_cpu=True),
        budgets=Budgets(wall_clock=60),
    )
    source_files = collect_source_files(spec)
    plan = Plan(
        job_id="honest",
        spec_hash=plan_hash(spec, None, source_files),
        created_at="now",
        spec=spec,
        source_files=source_files,
    )
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(plan.model_dump_json())

    result = runner.invoke(app, ["job", "apply", str(plan_file)])

    assert "its own recorded hash" not in _clean(result.output)
    assert "source files changed" not in _clean(result.output)


def test_second_apply_claim_fails_while_the_first_is_held(tmp_path):
    from colab_cli.job.store import ApplyInProgress, JobStore

    store = JobStore(tmp_path)
    first = store.claim_apply("held", pid=os.getpid(), starttime="s1", boot_id="b1")
    try:
        with pytest.raises(ApplyInProgress, match="already being applied"):
            store.claim_apply("held", pid=os.getpid() + 1, starttime="s2", boot_id="b1")
    finally:
        first.release()
    second = store.claim_apply("held", pid=os.getpid(), starttime="s3", boot_id="b1")
    second.release()


def test_stale_apply_lock_is_taken_over(tmp_path):
    from colab_cli.job.store import JobStore

    store = JobStore(tmp_path)
    first = store.claim_apply("stale", pid=1, starttime="dead", boot_id="b")
    os.close(first.fd)
    first.fd = -1
    second = store.claim_apply("stale", pid=2, starttime="live", boot_id="b")
    try:
        identity = store.supervisor_identity("stale")
        assert identity["pid"] == 2
    finally:
        second.release()


def test_apply_refuses_a_live_second_owner_before_assignment(
    tmp_path, mock_common_state
):
    from colab_cli.commands.job import _store
    from colab_cli.job.runtime_payload import ident

    plan_file = _locked_plan(tmp_path, "live-owner")
    store = _store()
    claim = store.claim_apply(
        "live-owner",
        pid=os.getpid(),
        starttime=ident.starttime(os.getpid()),
        boot_id=ident.boot_id(),
    )
    try:
        result = runner.invoke(app, ["job", "apply", str(plan_file)])
    finally:
        claim.release()

    assert result.exit_code == 1
    assert "already being applied" in _clean(result.output)
    mock_common_state.client.assign.assert_not_called()



def test_apply_refuses_a_job_that_already_has_an_endpoint(
    tmp_path, mock_common_state
):
    from colab_cli.commands.job import _store
    from colab_cli.job.models import JobEnvelope

    plan_file = _locked_plan(tmp_path, "has-endpoint")
    store = _store()
    store.write_envelope(
        JobEnvelope(job_id="has-endpoint", phase=Phase.RUN, endpoint="m-already")
    )

    result = runner.invoke(app, ["job", "apply", str(plan_file)])

    assert result.exit_code == 1
    assert "already has endpoint" in _clean(result.output)
def test_apply_refuses_oversized_source_before_assignment(
    tmp_path, mock_common_state, monkeypatch
):
    monkeypatch.setattr(
        "colab_cli.job.payload_bundle.CONTENTS_UPLOAD_CEILING", 1
    )
    plan_file = _locked_plan(tmp_path, "too-big")
    result = runner.invoke(app, ["job", "apply", str(plan_file)])
    assert result.exit_code == 1
    assert "Contents ceiling" in _clean(result.output)
    mock_common_state.client.assign.assert_not_called()



def _locked_plan(tmp_path, job_id, *, kind="file", files=None):
    from colab_cli.job.models import Plan
    from colab_cli.job.payload_bundle import collect_source_files
    from colab_cli.job.spec_io import plan_hash

    files = files or {"train.py": "print(1)\n"}
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    spec_path = tmp_path / "job.yaml"
    spec_path.write_text("name: lock\n")
    spec = JobSpec(
        name=job_id,
        code=CodeSpec(kind=kind, root=str(tmp_path), entry="train.py"),
        accelerator=Accelerator(prefer=[], accept_cpu=True),
        budgets=Budgets(wall_clock=60),
    )
    source_files = collect_source_files(spec, spec_path)
    plan = Plan(
        job_id=job_id,
        spec_hash=plan_hash(spec, str(spec_path), source_files),
        created_at="now",
        spec=spec,
        source_spec_path=str(spec_path),
        source_files=source_files,
    )
    plan_file = tmp_path.parent / f"{job_id}.plan.json"
    plan_file.write_text(plan.model_dump_json())
    return plan_file


def test_apply_refuses_a_modified_source_file_before_assignment(
    tmp_path, mock_common_state
):
    plan_file = _locked_plan(tmp_path, "changed-byte")
    (tmp_path / "train.py").write_text("print(2)\n")

    result = runner.invoke(app, ["job", "apply", str(plan_file)])

    assert result.exit_code == 1
    assert "changed train.py" in _clean(result.output)
    mock_common_state.client.assign.assert_not_called()


def test_apply_refuses_added_removed_and_renamed_bundle_files(
    tmp_path, mock_common_state
):
    files = {"train.py": "print(1)\n", "helper.py": "X = 1\n"}
    plan_file = _locked_plan(tmp_path, "bundle-drift", kind="bundle", files=files)

    (tmp_path / "extra.py").write_text("Y = 2\n")
    result = runner.invoke(app, ["job", "apply", str(plan_file)])
    assert result.exit_code == 1
    assert "added extra.py" in _clean(result.output)
    mock_common_state.client.assign.assert_not_called()

    (tmp_path / "extra.py").unlink()
    (tmp_path / "helper.py").unlink()
    result = runner.invoke(app, ["job", "apply", str(plan_file)])
    assert result.exit_code == 1
    assert "removed helper.py" in _clean(result.output)
    mock_common_state.client.assign.assert_not_called()

    (tmp_path / "helper.py").write_text("X = 1\n")
    (tmp_path / "helper.py").rename(tmp_path / "util.py")
    result = runner.invoke(app, ["job", "apply", str(plan_file)])
    out = _clean(result.output)
    assert "removed helper.py" in out
    assert "util.py" in out
    mock_common_state.client.assign.assert_not_called()


def test_apply_async_spawns_a_detached_child_and_returns_immediately(
    tmp_path, monkeypatch, mock_common_state
):
    plan_file = _locked_plan(tmp_path, "async-job")
    calls = []

    class FakeProc:
        pid = 4242

    real_popen = subprocess.Popen

    def fake_popen(cmd, **kwargs):
        if isinstance(cmd, list) and "colab_cli.cli" in cmd:
            calls.append((cmd, kwargs))
            return FakeProc()
        return real_popen(cmd, **kwargs)

    monkeypatch.setattr("colab_cli.commands.job.subprocess.Popen", fake_popen)

    result = runner.invoke(app, ["job", "apply", str(plan_file), "--async"])

    assert result.exit_code == 0
    assert len(calls) == 1, "must spawn exactly one child, never run apply itself"
    cmd, kwargs = calls[0]
    assert "job" in cmd and "apply" in cmd
    assert str(plan_file) in cmd
    assert "--async" not in cmd, "the child must run the real, blocking apply"
    assert kwargs.get("stdin") is not None  # detached: never inherits a TTY
    assert "4242" in _clean(result.output)
    assert "job status async-job --poll" in _clean(result.output)


def test_apply_async_with_job_id_propagates_it_and_never_reads_plan_file(
    tmp_path, monkeypatch, mock_common_state
):
    """--job-id is the whole point of not needing a plan file positionally
    -- the spawn path must not require one either."""
    calls = []

    class FakeProc:
        pid = 99

    real_popen = subprocess.Popen

    def fake_popen(cmd, **kwargs):
        if isinstance(cmd, list) and "colab_cli.cli" in cmd:
            calls.append(cmd)
            return FakeProc()
        return real_popen(cmd, **kwargs)

    monkeypatch.setattr("colab_cli.commands.job.subprocess.Popen", fake_popen)

    result = runner.invoke(
        app, ["job", "apply", "--job-id", "preplanned-job", "--async"]
    )

    assert result.exit_code == 0
    assert len(calls) == 1
    assert "--job-id" in calls[0] and "preplanned-job" in calls[0]
    assert "preplanned-job" in _clean(result.output)




def test_apply_async_unreadable_plan_fails_fast_without_spawning(
    tmp_path, monkeypatch, mock_common_state
):
    """A plan file that can't even be parsed shouldn't spawn a child
    destined to fail identically a moment later with no one watching."""
    bad_plan = tmp_path / "corrupt.plan.json"
    bad_plan.write_text("not json")
    calls = []
    real_popen = subprocess.Popen

    def fake_popen(cmd, **kwargs):
        if isinstance(cmd, list) and "colab_cli.cli" in cmd:
            calls.append((cmd, kwargs))
            return MagicMock()
        return real_popen(cmd, **kwargs)

    monkeypatch.setattr("colab_cli.commands.job.subprocess.Popen", fake_popen)

    result = runner.invoke(app, ["job", "apply", str(bad_plan), "--async"])

    assert result.exit_code == 1
    assert calls == []


def test_apply_async_json_emits_job_id_pid_and_log_path(
    tmp_path, monkeypatch, mock_common_state
):
    plan_file = _locked_plan(tmp_path, "async-json-job")

    class FakeProc:
        pid = 777

    real_popen = subprocess.Popen

    def fake_popen(cmd, **kwargs):
        if isinstance(cmd, list) and "colab_cli.cli" in cmd:
            return FakeProc()
        return real_popen(cmd, **kwargs)

    monkeypatch.setattr("colab_cli.commands.job.subprocess.Popen", fake_popen)
    _json_mode(mock_common_state)

    result = runner.invoke(app, ["job", "apply", str(plan_file), "--async"])

    payload = _job_json(result)
    assert result.exit_code == 0
    assert payload["job_id"] == "async-json-job"
    assert payload["pid"] == 777
    assert payload["log_path"].endswith("apply.log")


def test_plan_records_relative_path_size_and_sha256(tmp_path, mock_common_state):
    (tmp_path / "train.py").write_text("print(1)\n")
    spec = tmp_path / "job.yaml"
    spec.write_text(
        "name: lock-me\naccelerator:\n  prefer: []\n  accept_cpu: true\n"
        "code:\n  kind: file\n  entry: train.py\nbudgets:\n  wall_clock: 60\n"
    )

    result = runner.invoke(app, ["job", "plan", str(spec), "--no-probe"])

    assert result.exit_code == 0
    from colab_cli.commands.job import _store

    plans = list((_store().root).glob("*/plan.json"))
    assert len(plans) == 1
    payload = json.loads(plans[0].read_text())
    files = payload["source_files"]
    assert files[0]["path"] == "train.py"
    assert files[0]["size_bytes"] == len("print(1)\n")
    assert len(files[0]["sha256"]) == 64



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

    assert {"job_plan", "job_status", "job_destroy", "jobs_list", "jobs_prune"} <= names
    assert "job" not in names, "the bare group is a parameterless, useless tool"
    assert "jobs" not in names, "the bare group is a parameterless, useless tool"
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
    ok, text = invoke_command("jobs_list", commands["jobs_list"], {})

    assert ok, f"jobs_list failed to dispatch: {text}"


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



def test_stage_payload_keeps_signed_queries_only_in_private_channel(
    tmp_path, monkeypatch
):
    from pathlib import Path

    from colab_cli.job.models import Control, ControlChannel, DataItem

    sentinel = "ISSUE18_REMOTE_SENTINEL"
    entry = tmp_path / "train.py"
    entry.write_text("print('ok')")
    source_spec = tmp_path / "job.yaml"
    source_spec.write_text(f"url: https://storage.example/spec?sig={sentinel}")
    generated_secret = tmp_path / "plan.json.mighty-colab-secrets.json"
    generated_secret.write_text(sentinel)
    (tmp_path / ".mighty-colab-secret-tmp-orphan").write_text(sentinel)
    (tmp_path / "ordinary.json").write_text("{}")
    put_url = f"https://storage.example/result?sig={sentinel}-put"
    get_url = f"https://storage.example/result?sig={sentinel}-get"
    data_url = f"https://storage.example/input?sig={sentinel}-data"
    artifact_url = f"https://storage.example/output?sig={sentinel}-artifact"
    spec = JobSpec(
        name="staged-control",
        code=CodeSpec(kind="bundle", root=str(tmp_path), entry="train.py"),
        data=[DataItem(url=data_url, dest="input.bin", size_bytes=1)],
        artifacts=[ArtifactItem(path="output.bin", url=artifact_url)],
        control=Control(
            result=ControlChannel(put_url=put_url, get_url=get_url)
        ),
    )
    client = MagicMock()
    uploads = []

    def capture(local_path, remote_path):
        path = Path(local_path)
        uploads.append((remote_path, path.read_bytes(), path.stat().st_mode & 0o777))

    client.upload.side_effect = capture

    payload_bundle.stage_payload(
        spec=spec,
        job_id="staged-control",
        transport=client,
        remote_dir="/content/jobs/staged-control",
        source_spec_path=source_spec,
    )

    secret_upload = uploads[-1]
    assert secret_upload[0].endswith("/.secrets/transfer.json")
    assert secret_upload[2] == 0o600
    assert sentinel.encode() in secret_upload[1]
    for remote_path, content, _mode in uploads[:-1]:
        assert sentinel.encode() not in content, remote_path
    uploaded_paths = {path for path, _content, _mode in uploads}
    assert not any(path.endswith("/src/job.yaml") for path in uploaded_paths)
    assert not any("mighty-colab-secrets.json" in path for path in uploaded_paths)
    assert not any("mighty-colab-secret-tmp-" in path for path in uploaded_paths)
    assert any(path.endswith("/src/ordinary.json") for path in uploaded_paths)
    assert get_url.encode() not in secret_upload[1]


def test_stage_payload_rejects_a_sibling_job_spec_with_signed_queries(
    tmp_path, monkeypatch
):
    from pathlib import Path

    sentinel = "ISSUE18_SIBLING_SPEC_SENTINEL"
    entry = tmp_path / "train.py"
    entry.write_text("print('safe')")
    source_spec = tmp_path / "train.yaml"
    source_spec.write_text(f"url: https://storage.example/current?sig={sentinel}")
    (tmp_path / "eval.spec").write_text(
        f"data: [{{url: 'HTTPS://[2001:db8::1]/sibling?auth={sentinel}'}}]"
    )
    spec = JobSpec(
        name="sibling-spec",
        code=CodeSpec(kind="bundle", root=str(tmp_path), entry="train.py"),
    )
    client = MagicMock()
    uploaded = []

    def capture(local_path, remote_path):
        uploaded.append((remote_path, Path(local_path).read_bytes()))

    client.upload.side_effect = capture

    with pytest.raises(ValueError, match="credential-bearing URL"):
        payload_bundle.stage_payload(
            spec=spec,
            job_id="sibling-spec",
            transport=client,
            remote_dir="/content/jobs/sibling-spec",
            source_spec_path=source_spec,
        )

    assert all(sentinel.encode() not in content for _path, content in uploaded)
    assert not any(path.endswith("/src/train.yaml") for path, _content in uploaded)
    assert not any(path.endswith("/src/eval.spec") for path, _content in uploaded)


def test_stage_payload_allows_benign_http_query_parameters(tmp_path, monkeypatch):
    from pathlib import Path

    entry = tmp_path / "train.py"
    source = "import requests\nrequests.get('https://api.example/items?page=1')\n"
    entry.write_text(source)
    spec = JobSpec(
        name="benign-query",
        code=CodeSpec(kind="file", root=str(tmp_path), entry="train.py"),
    )
    client = MagicMock()
    uploaded = {}

    def capture(local_path, remote_path):
        uploaded[remote_path] = Path(local_path).read_text()

    client.upload.side_effect = capture

    payload_bundle.stage_payload(
        spec=spec,
        job_id="benign-query",
        transport=client,
        remote_dir="/content/jobs/benign-query",
    )

    assert uploaded["/content/jobs/benign-query/src/train.py"] == source


def test_stage_payload_refuses_an_undeclared_source_file(tmp_path, monkeypatch):
    from colab_cli.job.payload_bundle import collect_source_files

    (tmp_path / "train.py").write_text("print(1)\n")
    spec = JobSpec(
        name="locked-stage",
        code=CodeSpec(kind="bundle", root=str(tmp_path), entry="train.py"),
    )
    locked = collect_source_files(spec)
    (tmp_path / "sneak.py").write_text("print(2)\n")
    with pytest.raises(ValueError, match="undeclared source file: sneak.py"):
        payload_bundle.stage_payload(
            spec=spec,
            job_id="locked-stage",
            transport=MagicMock(),
            remote_dir="/content/jobs/locked-stage",
            source_files=locked,
        )



@pytest.mark.parametrize("kind", ["file", "bundle"])
def test_payload_rejects_symlink_aliases_to_local_secrets(tmp_path, kind):
    root = tmp_path / "src"
    root.mkdir()
    secret = tmp_path / "plan.json.mighty-colab-secrets.json"
    secret.write_text("SIGNED_QUERY_SENTINEL")
    alias = root / "leak.json"
    alias.symlink_to(secret)
    entry = "leak.json"
    if kind == "bundle":
        entry = "train.py"
        (root / entry).write_text("print('safe')")
    spec = JobSpec(name="links", code=CodeSpec(kind=kind, root=str(root), entry=entry))

    with pytest.raises(ValueError, match="symbolic links"):
        list(payload_bundle._iter_user_files(spec))

def _persist_running_job(mock_common_state, job_id="destroy-me", control=None):
    from colab_cli.commands.job import _store
    from colab_cli.job.models import JobEnvelope, Plan, Supervisor
    from colab_cli.job.spec_io import spec_hash

    spec = JobSpec(
        name=job_id,
        code=CodeSpec(kind="file", entry="train.py"),
        accelerator=Accelerator(prefer=[], accept_cpu=True),
        budgets=Budgets(wall_clock=60),
        control=control or Control(),
    )
    plan = Plan(
        job_id=job_id,
        spec_hash=spec_hash(spec),
        created_at="now",
        spec=spec,
    )
    store = _store()
    store.write_plan(plan)
    store.write_envelope(
        JobEnvelope(
            schema_version="1",
            job_id=job_id,
            phase=Phase.RUN,
            workload=Workload.RUNNING,
            supervisor=Supervisor.INTERRUPTED,
            session="job-session",
            endpoint="m-s-endpoint",
        )
    )
    session = MagicMock()
    session.keep_alive_pid = None
    mock_common_state.store.get.return_value = session
    return store


def test_destroy_reconciles_remote_success_before_unassign(
    monkeypatch, mock_common_state
):
    from colab_cli.job.transport import ReadStatus

    store = _persist_running_job(mock_common_state)
    (store.job_dir("destroy-me") / "plan.json").unlink()
    transport = MagicMock()
    events = []
    transport.read_json.side_effect = lambda _path: (
        events.append("read")
        or (
            {
                "schema_version": "2",
                "cli_version": "producer-cli",
                "runtime_payload_version": "sha256:producer-payload",
                "workload": "succeeded",
                "exit_code": 0,
            },
            ReadStatus.OK,
        )
    )
    transport.write_json.return_value = ReadStatus.OK
    transport.remove.return_value = ReadStatus.OK
    mock_common_state.client.unassign.side_effect = lambda _endpoint: events.append(
        "unassign"
    )
    monkeypatch.setattr(
        "colab_cli.job.transport.JobTransport", lambda *_args: transport
    )

    result = runner.invoke(app, ["job", "destroy", "destroy-me"])

    assert result.exit_code == 0
    env = store.read_envelope("destroy-me")
    assert events[:2] == ["read", "unassign"]
    assert env.workload is Workload.SUCCEEDED
    assert env.exit_code == 0
    assert env.schema_version == "2"
    assert env.cli_version == "producer-cli"
    assert env.runtime_payload_version == "sha256:producer-payload"
    transport.write_json.assert_not_called()


def test_destroy_recovers_terminal_result_from_control_get_url(
    monkeypatch, mock_common_state
):
    from colab_cli.job.transport import ReadStatus

    get_url = "https://storage.googleapis.com/results/destroy.json?signature=get"
    control = Control(
        result=ControlChannel(
            put_url="https://storage.googleapis.com/results/destroy.json?signature=put",
            get_url=get_url,
        )
    )
    store = _persist_running_job(mock_common_state, control=control)
    transport = MagicMock()
    transport.read_json.return_value = (None, ReadStatus.NOT_FOUND)
    transport.remove.return_value = ReadStatus.OK
    monkeypatch.setattr(
        "colab_cli.job.transport.JobTransport", lambda *_args: transport
    )

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _size):
            return json.dumps(
                {
                    "schema_version": "2",
                    "cli_version": "producer-cli",
                    "runtime_payload_version": "sha256:producer-payload",
                    "workload": "succeeded",
                    "exit_code": 0,
                }
            ).encode()

    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: Response())
    _json_mode(mock_common_state)

    result = runner.invoke(app, ["job", "destroy", "destroy-me"])
    assert result.exit_code == 0
    env = store.read_envelope("destroy-me")
    assert env.workload is Workload.SUCCEEDED
    assert env.exit_code == 0
    assert env.cli_version == "producer-cli"
    assert env.runtime_payload_version == "sha256:producer-payload"
    emitted = json.loads(result.stdout)["job"]
    assert emitted["schema_version"] == "2"
    assert emitted["cli_version"] == "producer-cli"
    assert emitted["runtime_payload_version"] == "sha256:producer-payload"
    mock_common_state.client.unassign.assert_called_once_with("m-s-endpoint")


def test_destroy_still_unassigns_when_control_result_is_malformed(
    monkeypatch, mock_common_state
):
    from colab_cli.job.transport import ReadStatus

    control = Control(
        result=ControlChannel(
            put_url="https://storage.googleapis.com/results/bad.json?signature=put",
            get_url="https://storage.googleapis.com/results/bad.json?signature=get",
        )
    )
    store = _persist_running_job(mock_common_state, control=control)
    transport = MagicMock()
    transport.read_json.return_value = (None, ReadStatus.NOT_FOUND)
    transport.write_json.return_value = ReadStatus.OK
    transport.remove.return_value = ReadStatus.OK
    monkeypatch.setattr(
        "colab_cli.job.transport.JobTransport", lambda *_args: transport
    )

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _size):
            return b'{"workload":"succeeded","artifacts":[{"unexpected":1}]}'

    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: Response())

    result = runner.invoke(app, ["job", "destroy", "destroy-me"])

    assert result.exit_code == 0
    assert store.read_envelope("destroy-me").workload is Workload.UNKNOWN
    mock_common_state.client.unassign.assert_called_once_with("m-s-endpoint")


def test_cancel_only_preserves_terminal_control_result_during_forced_release(
    monkeypatch, mock_common_state
):
    control = Control(
        result=ControlChannel(
            put_url="https://storage.googleapis.com/results/cancel.json?signature=put",
            get_url="https://storage.googleapis.com/results/cancel.json?signature=get",
        )
    )
    store = _persist_running_job(mock_common_state, control=control)
    mock_common_state.store.get.return_value = None

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _size):
            return b'{"workload":"succeeded","exit_code":0}'

    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: Response())

    result = runner.invoke(
        app, ["job", "destroy", "destroy-me", "--cancel-only"]
    )

    assert result.exit_code == 1
    env = store.read_envelope("destroy-me")
    assert env.workload is Workload.SUCCEEDED
    assert env.exit_code == 0
    mock_common_state.client.unassign.assert_called_once_with("m-s-endpoint")


def test_destroy_without_remote_verdict_records_unknown(
    monkeypatch, mock_common_state
):
    from colab_cli.job.transport import ReadStatus

    store = _persist_running_job(mock_common_state)
    transport = MagicMock()
    transport.read_json.return_value = (None, ReadStatus.NOT_FOUND)
    transport.write_json.return_value = ReadStatus.OK
    transport.remove.return_value = ReadStatus.OK
    monkeypatch.setattr(
        "colab_cli.job.transport.JobTransport", lambda *_args: transport
    )

    result = runner.invoke(app, ["job", "destroy", "destroy-me"])

    assert result.exit_code == 0
    env = store.read_envelope("destroy-me")
    assert env.workload is Workload.UNKNOWN
    assert "verdict" in env.reason
    mock_common_state.client.unassign.assert_called_once_with("m-s-endpoint")


def test_cancel_only_writes_intent_without_unassign(monkeypatch, mock_common_state):
    from colab_cli.job.transport import ReadStatus

    store = _persist_running_job(mock_common_state)
    transport = MagicMock()
    transport.read_json.return_value = (None, ReadStatus.NOT_FOUND)
    transport.write_json.return_value = ReadStatus.OK
    transport.remove.return_value = ReadStatus.OK
    monkeypatch.setattr(
        "colab_cli.job.transport.JobTransport", lambda *_args: transport
    )

    result = runner.invoke(
        app, ["job", "destroy", "destroy-me", "--cancel-only"]
    )

    assert result.exit_code == 0
    env = store.read_envelope("destroy-me")
    assert env.workload is Workload.RUNNING
    assert "intent written" in env.reason
    transport.write_json.assert_called_once()
    transport.remove.assert_called_once_with(
        "/content/jobs/destroy-me/mighty_runtime/.secrets/transfer.json"
    )
    mock_common_state.client.unassign.assert_not_called()


def test_cancel_only_reports_when_intent_write_failed(monkeypatch, mock_common_state):
    from colab_cli.job.transport import ReadStatus

    store = _persist_running_job(mock_common_state)
    transport = MagicMock()
    transport.read_json.return_value = (None, ReadStatus.DEGRADED)
    transport.write_json.return_value = ReadStatus.DEGRADED
    transport.remove.return_value = ReadStatus.OK
    monkeypatch.setattr(
        "colab_cli.job.transport.JobTransport", lambda *_args: transport
    )

    result = runner.invoke(
        app, ["job", "destroy", "destroy-me", "--cancel-only"]
    )

    assert result.exit_code == 1
    assert "could not be confirmed" in store.read_envelope("destroy-me").reason
    mock_common_state.client.unassign.assert_not_called()



def test_status_scrubs_an_interrupted_prelaunch_secret(monkeypatch, mock_common_state):
    from colab_cli.job.transport import ReadStatus

    _persist_running_job(mock_common_state, job_id="interrupted")
    transport = MagicMock()
    transport.remove.return_value = ReadStatus.OK
    transport.read_json.return_value = (None, ReadStatus.NOT_FOUND)
    monkeypatch.setattr(
        "colab_cli.job.transport.JobTransport", lambda *_args: transport
    )

    result = runner.invoke(app, ["job", "status", "interrupted"])

    assert result.exit_code == 0
    transport.remove.assert_called_once_with(
        "/content/jobs/interrupted/mighty_runtime/.secrets/transfer.json"
    )


def test_status_respawns_keep_alive_when_the_daemon_has_died(
    monkeypatch, mock_common_state
):
    """Issue #54: the daemon has been observed dying even though
    `spawn_keep_alive` starts it genuinely detached. Self-heal from
    `status` regardless of why it died -- don't rely on the cause never
    recurring."""
    import os

    from colab_cli.job.models import Supervisor
    from colab_cli.job.runtime_payload import ident
    from colab_cli.job.transport import ReadStatus

    store = _persist_running_job(mock_common_state, job_id="dead-keepalive")
    env = store.read_envelope("dead-keepalive")
    env.supervisor = Supervisor.RUNNING
    store.write_envelope(env)
    store.write_supervisor_identity(
        "dead-keepalive",
        pid=os.getpid(),
        starttime=ident.starttime(os.getpid()),
        boot_id=ident.boot_id(),
    )
    session = mock_common_state.store.get.return_value
    session.keep_alive_pid = None  # explicit, matches the fixture default
    session.endpoint = "gpu-a100-s-example"
    session.name = "job-dead-keepalive"
    spawn_calls = []
    monkeypatch.setattr(
        "colab_cli.commands.session.spawn_keep_alive",
        lambda endpoint, name, **kw: spawn_calls.append((endpoint, name, kw))
        or os.getpid(),
    )
    transport = MagicMock()
    transport.read_json.return_value = (None, ReadStatus.NOT_FOUND)
    monkeypatch.setattr(
        "colab_cli.job.transport.JobTransport", lambda *_args: transport
    )

    result = runner.invoke(app, ["job", "status", "dead-keepalive"])

    assert result.exit_code == 0
    assert len(spawn_calls) == 1
    endpoint, name, kw = spawn_calls[0]
    assert endpoint == "gpu-a100-s-example"
    assert name == "job-dead-keepalive"
    assert session.keep_alive_pid == os.getpid()
    mock_common_state.store.add.assert_called_with(session)
    env = store.read_envelope("dead-keepalive")
    assert any("respawned" in h for h in env.hints)


def test_status_does_not_respawn_a_healthy_keep_alive(monkeypatch, mock_common_state):
    import os

    from colab_cli.job.models import Supervisor
    from colab_cli.job.runtime_payload import ident
    from colab_cli.job.transport import ReadStatus

    store = _persist_running_job(mock_common_state, job_id="healthy-keepalive")
    env = store.read_envelope("healthy-keepalive")
    env.supervisor = Supervisor.RUNNING
    store.write_envelope(env)
    store.write_supervisor_identity(
        "healthy-keepalive",
        pid=os.getpid(),
        starttime=ident.starttime(os.getpid()),
        boot_id=ident.boot_id(),
    )
    session = mock_common_state.store.get.return_value
    session.keep_alive_pid = os.getpid()  # alive for the duration of this test
    spawn_calls = []
    monkeypatch.setattr(
        "colab_cli.commands.session.spawn_keep_alive",
        lambda *a, **k: spawn_calls.append((a, k)) or 1,
    )
    transport = MagicMock()
    transport.read_json.return_value = (None, ReadStatus.NOT_FOUND)
    monkeypatch.setattr(
        "colab_cli.job.transport.JobTransport", lambda *_args: transport
    )

    result = runner.invoke(app, ["job", "status", "healthy-keepalive"])

    assert result.exit_code == 0
    assert spawn_calls == []



def test_status_does_not_scrub_a_live_supervisor_before_launch(
    monkeypatch, mock_common_state
):
    import os

    from colab_cli.job.models import Supervisor
    from colab_cli.job.runtime_payload import ident
    from colab_cli.job.transport import ReadStatus

    store = _persist_running_job(mock_common_state, job_id="live-supervisor")
    env = store.read_envelope("live-supervisor")
    env.supervisor = Supervisor.RUNNING
    store.write_envelope(env)
    store.write_supervisor_identity(
        "live-supervisor",
        pid=os.getpid(),
        starttime=ident.starttime(os.getpid()),
        boot_id=ident.boot_id(),
    )
    transport = MagicMock()
    transport.read_json.return_value = (None, ReadStatus.NOT_FOUND)
    monkeypatch.setattr(
        "colab_cli.job.transport.JobTransport", lambda *_args: transport
    )

    result = runner.invoke(app, ["job", "status", "live-supervisor"])

    assert result.exit_code == 0
    transport.remove.assert_not_called()


def test_status_poll_keeps_waiting_while_runner_has_not_launched_yet(
    monkeypatch, mock_common_state
):
    """`--poll` must keep polling through install/restart/verify/stage --
    every phase before the runner actually launches reads as
    `_observe_remote`'s "never_started" kind, since `launch.json` doesn't
    exist yet. Previously the loop broke on the very first "never_started"
    observation regardless of `--poll`, so a job still installing (the
    common case right after `apply` starts) got exactly one status
    snapshot and returned -- never waiting for `--interval` at all.
    """
    import os

    from colab_cli.job.models import Supervisor
    from colab_cli.job.runtime_payload import ident
    from colab_cli.job.transport import ReadStatus

    store = _persist_running_job(mock_common_state, job_id="still-installing")
    env = store.read_envelope("still-installing")
    env.supervisor = Supervisor.RUNNING
    store.write_envelope(env)
    store.write_supervisor_identity(
        "still-installing",
        pid=os.getpid(),
        starttime=ident.starttime(os.getpid()),
        boot_id=ident.boot_id(),
    )
    transport = MagicMock()
    calls = {"n": 0}

    def read_json(path):
        calls["n"] += 1
        # First two polls: runner hasn't launched (no launch.json). Third:
        # it has, and the workload finished.
        if path.endswith("result.json") and calls["n"] >= 5:
            return {"workload": "succeeded", "exit_code": 0}, ReadStatus.OK
        if path.endswith("launch.json") and calls["n"] >= 5:
            return {"pid": 7, "starttime": "1", "boot_id": "b"}, ReadStatus.OK
        return None, ReadStatus.NOT_FOUND

    transport.read_json.side_effect = read_json
    monkeypatch.setattr(
        "colab_cli.job.transport.JobTransport", lambda *_args: transport
    )
    sleeps = []
    monkeypatch.setattr(
        "colab_cli.commands.job.time.sleep", lambda s: sleeps.append(s)
    )

    result = runner.invoke(
        app, ["job", "status", "still-installing", "--poll", "--interval", "1"]
    )

    assert result.exit_code == 0
    # Must have actually waited across multiple never_started observations
    # rather than returning after the first one.
    assert len(sleeps) >= 1, "status --poll returned after a single observation"
    env = store.read_envelope("still-installing")
    assert env.workload is Workload.SUCCEEDED


def test_status_poll_pulls_runner_log_locally(monkeypatch, mock_common_state):
    """Same guarantee as exec-async: as long as the VM is alive, the log
    is pulled locally on every healthy poll tick, not just at the end.
    """
    import os

    from colab_cli.job.models import Supervisor
    from colab_cli.job.runtime_payload import ident
    from colab_cli.job.transport import ReadStatus

    store = _persist_running_job(mock_common_state, job_id="log-pull")
    env = store.read_envelope("log-pull")
    env.supervisor = Supervisor.RUNNING
    store.write_envelope(env)
    store.write_supervisor_identity(
        "log-pull",
        pid=os.getpid(),
        starttime=ident.starttime(os.getpid()),
        boot_id=ident.boot_id(),
    )
    transport = MagicMock()
    transport.read_json.return_value = (None, ReadStatus.NOT_FOUND)
    transport.read_text.return_value = ("step 500: loss=0.1\n", ReadStatus.OK)
    monkeypatch.setattr(
        "colab_cli.job.transport.JobTransport", lambda *_args: transport
    )

    result = runner.invoke(app, ["job", "status", "log-pull"])

    assert result.exit_code == 0
    assert (store.job_dir("log-pull") / "runner.log").read_text() == (
        "step 500: loss=0.1\n"
    )


def _remote_files(mapping):
    from colab_cli.job.transport import ReadStatus

    def read_json(path):
        for suffix, payload in mapping.items():
            if path.endswith(suffix):
                if payload is None:
                    return None, ReadStatus.NOT_FOUND
                return payload, ReadStatus.OK
        return None, ReadStatus.NOT_FOUND

    return read_json


def test_status_recovers_a_complete_result_and_releases(monkeypatch, mock_common_state):
    from colab_cli.job.models import Cleanup, Offload
    from colab_cli.job.transport import ReadStatus

    store = _persist_running_job(mock_common_state, job_id="orphan-result")
    transport = MagicMock()
    transport.remove.return_value = ReadStatus.OK
    transport.read_json.side_effect = _remote_files(
        {
            "result.json": {
                "workload": "succeeded",
                "exit_code": 0,
                "phase": "offload",
                "artifacts": [],
                "offload": "ok",
            }
        }
    )
    monkeypatch.setattr(
        "colab_cli.job.transport.JobTransport", lambda *_args: transport
    )

    result = runner.invoke(app, ["job", "status", "orphan-result"])

    assert result.exit_code == 0
    mock_common_state.client.unassign.assert_called_once_with("m-s-endpoint")
    env = store.read_envelope("orphan-result")
    assert env.workload is Workload.SUCCEEDED
    assert env.offload is Offload.NOT_REQUIRED
    assert env.cleanup is Cleanup.RELEASED


def test_status_keeps_the_remote_result_when_cleanup_fails(
    monkeypatch, mock_common_state
):
    from colab_cli.job.models import Cleanup
    from colab_cli.job.transport import ReadStatus

    store = _persist_running_job(mock_common_state, job_id="orphan-leak")
    transport = MagicMock()
    transport.remove.return_value = ReadStatus.OK
    transport.read_json.side_effect = _remote_files(
        {"result.json": {"workload": "succeeded", "exit_code": 0}}
    )
    mock_common_state.client.unassign.side_effect = RuntimeError("boom")
    monkeypatch.setattr(
        "colab_cli.job.transport.JobTransport", lambda *_args: transport
    )

    result = runner.invoke(app, ["job", "status", "orphan-leak"])

    assert result.exit_code == 0
    env = store.read_envelope("orphan-leak")
    assert env.workload is Workload.SUCCEEDED
    assert env.exit_code == 0
    assert env.cleanup is Cleanup.FAILED


def test_status_uses_launch_identity_before_declaring_a_dead_runner(
    monkeypatch, mock_common_state
):
    from colab_cli.job.models import Cleanup
    from colab_cli.job.transport import ReadStatus

    store = _persist_running_job(mock_common_state, job_id="dead-runner")
    transport = MagicMock()
    transport.remove.return_value = ReadStatus.OK
    transport.read_json.side_effect = _remote_files(
        {
            "result.json": None,
            "launch.json": {
                "pid": 99,
                "starttime": "1234",
                "boot_id": "boot",
            },
            "watchdog.json": {"runner_alive": False},
        }
    )
    monkeypatch.setattr(
        "colab_cli.job.transport.JobTransport", lambda *_args: transport
    )

    result = runner.invoke(app, ["job", "status", "dead-runner"])

    assert result.exit_code == 0
    mock_common_state.client.unassign.assert_called_once_with("m-s-endpoint")
    env = store.read_envelope("dead-runner")
    assert env.workload is Workload.UNKNOWN
    assert env.cleanup is Cleanup.RELEASED
    assert "dead" in (env.reason or "")


def test_status_does_not_release_while_runner_identity_is_alive(
    monkeypatch, mock_common_state
):
    from colab_cli.job.transport import ReadStatus

    _persist_running_job(mock_common_state, job_id="live-runner")
    transport = MagicMock()
    transport.remove.return_value = ReadStatus.OK
    transport.read_json.side_effect = _remote_files(
        {
            "result.json": None,
            "launch.json": {
                "pid": 99,
                "starttime": "1234",
                "boot_id": "boot",
            },
            "watchdog.json": {"runner_alive": True},
        }
    )
    monkeypatch.setattr(
        "colab_cli.job.transport.JobTransport", lambda *_args: transport
    )

    result = runner.invoke(app, ["job", "status", "live-runner"])

    assert result.exit_code == 0
    mock_common_state.client.unassign.assert_not_called()



def test_status_forces_teardown_when_interrupted_session_state_is_missing(
    monkeypatch, mock_common_state
):
    from colab_cli.job.models import Cleanup
    store = _persist_running_job(mock_common_state, job_id="lost-session")
    mock_common_state.store.get.return_value = None

    result = runner.invoke(app, ["job", "status", "lost-session"])

    assert result.exit_code == 1
    mock_common_state.client.unassign.assert_called_once_with("m-s-endpoint")
    env = store.read_envelope("lost-session")
    assert env.cleanup is Cleanup.RELEASED
    assert "credential deletion" in env.reason


def test_status_recovers_terminal_result_from_control_get_url(
    monkeypatch, mock_common_state
):
    get_url = "https://storage.googleapis.com/results/lost.json?signature=get-secret"
    control = Control(
        result=ControlChannel(
            put_url="https://storage.googleapis.com/results/lost.json?signature=put-secret",
            get_url=get_url,
        )
    )
    store = _persist_running_job(
        mock_common_state, job_id="lost-result", control=control
    )
    original = store.read_envelope("lost-result")
    assert original is not None
    mock_common_state.store.get.return_value = None
    requests = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _size):
            return json.dumps({"workload": "succeeded", "exit_code": 0}).encode()

    def urlopen(request, timeout):
        requests.append((request.full_url, request.get_method(), timeout))
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", urlopen)

    result = runner.invoke(app, ["job", "status", "lost-result"])

    assert result.exit_code == 1
    assert requests == [(get_url, "GET", 10)]
    env = store.read_envelope("lost-result")
    assert env.workload is Workload.SUCCEEDED
    assert env.exit_code == 0
    assert env.cli_version == original.cli_version
    assert env.runtime_payload_version == original.runtime_payload_version


def test_status_ignores_control_result_placeholder(monkeypatch, mock_common_state):
    control = Control(
        result=ControlChannel(
            put_url="https://storage.googleapis.com/results/pending.json?signature=put",
            get_url="https://storage.googleapis.com/results/pending.json?signature=get",
        )
    )
    store = _persist_running_job(
        mock_common_state, job_id="pending-result", control=control
    )
    mock_common_state.store.get.return_value = None

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _size):
            return b"{}"

    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: Response())

    result = runner.invoke(app, ["job", "status", "pending-result"])

    assert result.exit_code == 1
    env = store.read_envelope("pending-result")
    assert env.workload is Workload.UNKNOWN
    assert "credential deletion" in env.reason
    mock_common_state.client.unassign.assert_called_once_with("m-s-endpoint")


def test_destroy_scrubs_secret_before_a_failed_unassign(
    monkeypatch, mock_common_state
):
    from colab_cli.job.transport import ReadStatus

    _persist_running_job(mock_common_state)
    transport = MagicMock()
    events = []
    transport.read_json.return_value = (None, ReadStatus.NOT_FOUND)
    transport.write_json.return_value = ReadStatus.OK
    transport.remove.side_effect = lambda _path: events.append("scrub") or ReadStatus.OK
    mock_common_state.client.unassign.side_effect = lambda _endpoint: (
        events.append("unassign") or (_ for _ in ()).throw(RuntimeError("failed"))
    )
    monkeypatch.setattr(
        "colab_cli.job.transport.JobTransport", lambda *_args: transport
    )

    result = runner.invoke(app, ["job", "destroy", "destroy-me"])

    assert result.exit_code == 1
    assert events == ["scrub", "unassign"]


def test_destroy_stops_keep_alive_before_unassign(monkeypatch, mock_common_state):
    from colab_cli.job.transport import ReadStatus

    _persist_running_job(mock_common_state)
    session = mock_common_state.store.get.return_value
    session.keep_alive_pid = 4242
    killed = []
    monkeypatch.setattr(
        "colab_cli.common.kill_process", lambda pid: killed.append(pid)
    )
    transport = MagicMock()
    events = []
    transport.read_json.return_value = (None, ReadStatus.NOT_FOUND)
    transport.write_json.return_value = ReadStatus.OK
    transport.remove.return_value = ReadStatus.OK
    mock_common_state.client.unassign.side_effect = lambda _endpoint: events.append(
        "unassign"
    )
    monkeypatch.setattr(
        "colab_cli.job.transport.JobTransport", lambda *_args: transport
    )

    result = runner.invoke(app, ["job", "destroy", "destroy-me"])

    assert result.exit_code == 0
    assert killed == [4242]
    assert events == ["unassign"]


def test_cancel_only_preserves_keep_alive(monkeypatch, mock_common_state):
    from colab_cli.job.transport import ReadStatus

    _persist_running_job(mock_common_state)
    session = mock_common_state.store.get.return_value
    session.keep_alive_pid = 4242
    killed = []
    monkeypatch.setattr(
        "colab_cli.common.kill_process", lambda pid: killed.append(pid)
    )
    transport = MagicMock()
    transport.read_json.return_value = (None, ReadStatus.NOT_FOUND)
    transport.write_json.return_value = ReadStatus.OK
    transport.remove.return_value = ReadStatus.OK
    monkeypatch.setattr(
        "colab_cli.job.transport.JobTransport", lambda *_args: transport
    )

    result = runner.invoke(app, ["job", "destroy", "destroy-me", "--cancel-only"])

    assert result.exit_code == 0
    assert killed == []
    mock_common_state.client.unassign.assert_not_called()
    mock_common_state.store.remove.assert_not_called()

def test_unexpected_apply_exception_emits_a_terminal_envelope(
    tmp_path, monkeypatch, mock_common_state
):
    from colab_cli.commands.job import _store
    from colab_cli.job.models import Plan
    from colab_cli.job.orchestrator import Orchestrator
    from colab_cli.job.payload_bundle import collect_source_files
    from colab_cli.job.spec_io import plan_hash

    (tmp_path / "train.py").write_text("print(1)\n")
    spec = JobSpec(
        name="unexpected",
        code=CodeSpec(kind="file", root=str(tmp_path), entry="train.py"),
        accelerator=Accelerator(prefer=[], accept_cpu=True),
        budgets=Budgets(wall_clock=60),
    )
    source_files = collect_source_files(spec)
    plan = Plan(
        job_id="unexpected",
        spec_hash=plan_hash(spec, None, source_files),
        created_at="now",
        spec=spec,
        source_files=source_files,
    )
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(plan.model_dump_json())
    monkeypatch.setattr(
        Orchestrator,
        "provision",
        lambda _self: (_ for _ in ()).throw(RuntimeError("supervisor boom")),
    )
    mock_common_state.debug = False
    _json_mode(mock_common_state)

    result = runner.invoke(app, ["job", "apply", str(plan_file)])

    assert result.exit_code == 1
    payload = json.loads(_clean(result.output).strip().splitlines()[-1])
    assert payload["job"]["workload"] == "failed"
    assert payload["job"]["supervisor"] == "finished"
    assert payload["done"] is True
    env = _store().read_envelope("unexpected")
    assert env.done
    assert env.retry_class is RetryClass.DO_NOT_RETRY
    assert env.reason == "internal supervisor failure (RuntimeError)"
    # Regression: the old hint ("re-run with --debug") was a dead end once
    # the job is terminal -- `job apply` on the same --job-id refuses, and
    # `job status --debug` never re-enters this except-clause. The hint
    # must point at where the traceback actually landed instead.
    assert env.hints == ["local traceback logged to ~/.config/colab-cli/colab.log"]


def test_status_poll_help_describes_recovery_cleanup():
    result = runner.invoke(
        app, ["job", "status", "--help"], env={"COLUMNS": "160"}
    )
    output = _clean(result.output)
    assert result.exit_code == 0
    assert "dead runner" in output
    assert "cleanup" in output


def test_status_poll_finishes_cleanup_after_the_result_arrives(
    monkeypatch, mock_common_state
):
    from colab_cli.job.models import Cleanup
    from colab_cli.job.transport import ReadStatus

    store = _persist_running_job(mock_common_state, job_id="poll-orphan")
    transport = MagicMock()
    transport.remove.return_value = ReadStatus.OK
    calls = {"n": 0}

    def read_json(path):
        calls["n"] += 1
        if path.endswith("result.json") and calls["n"] > 2:
            return {"workload": "failed", "exit_code": 1}, ReadStatus.OK
        if path.endswith("launch.json"):
            return {"pid": 7, "starttime": "1", "boot_id": "b"}, ReadStatus.OK
        if path.endswith("watchdog.json"):
            return {"runner_alive": True}, ReadStatus.OK
        return None, ReadStatus.NOT_FOUND

    transport.read_json.side_effect = read_json
    monkeypatch.setattr(
        "colab_cli.job.transport.JobTransport", lambda *_args: transport
    )
    monkeypatch.setattr("colab_cli.commands.job.time.sleep", lambda _s: None)

    result = runner.invoke(
        app, ["job", "status", "poll-orphan", "--poll", "--interval", "1"]
    )

    assert result.exit_code == 0
    env = store.read_envelope("poll-orphan")
    assert env.workload is Workload.FAILED
    assert env.exit_code == 1
    assert env.cleanup is Cleanup.RELEASED
    mock_common_state.client.unassign.assert_called_once_with("m-s-endpoint")


def test_status_clears_stale_hints_from_a_prior_call(mock_common_state):
    """A hint that was true at an earlier poll (e.g. "still billing" before
    teardown completed) must not survive into a later call's envelope once
    the job is fully released -- contradicting its own `cleanup` field.
    """
    from colab_cli.commands.job import _store
    from colab_cli.job.models import Cleanup, JobEnvelope, Offload, Supervisor, Workload

    store = _store()
    store.write_envelope(
        JobEnvelope(
            job_id="stale-hint",
            workload=Workload.FAILED,
            offload=Offload.FAILED,
            cleanup=Cleanup.RELEASED,
            supervisor=Supervisor.FINISHED,
            hints=[
                "VM left running deliberately and is still billing: "
                "`mighty-colab job destroy stale-hint` when done"
            ],
        )
    )

    result = runner.invoke(app, ["job", "status", "stale-hint"])

    assert result.exit_code == 0
    env = store.read_envelope("stale-hint")
    assert env.hints == [], (
        "a released, terminal job must not still claim it's 'still billing'"
    )


def test_status_keeps_permanent_diagnostic_hints_while_dropping_billing_ones(
    mock_common_state,
):
    from colab_cli.commands.job import _store
    from colab_cli.job.models import Cleanup, JobEnvelope, Offload, Supervisor, Workload

    store = _store()
    store.write_envelope(
        JobEnvelope(
            job_id="mixed-hints",
            workload=Workload.FAILED,
            offload=Offload.SKIPPED,
            cleanup=Cleanup.RELEASED,
            supervisor=Supervisor.FINISHED,
            reason="staging failed: a declared input could not be fetched",
            hints=[
                "check, in order: the URL has not expired; the object exists",
                "VM left running deliberately and is still billing: "
                "`mighty-colab job destroy mixed-hints` when done",
            ],
        )
    )

    result = runner.invoke(app, ["job", "status", "mixed-hints"])

    assert result.exit_code == 0
    env = store.read_envelope("mixed-hints")
    assert env.hints == [
        "check, in order: the URL has not expired; the object exists"
    ]

def test_destroy_does_not_carry_stale_billing_hints_into_a_second_call(mock_common_state):
    """Not a blanket hints reset (that would also wipe permanent diagnostic
    hints like "check the URL has not expired") -- only hints claiming the
    VM is still up/billing get dropped, and only once cleanup confirms it
    genuinely isn't.
    """
    from colab_cli.commands.job import _store
    from colab_cli.job.models import Cleanup, JobEnvelope, Offload, Supervisor, Workload

    store = _store()
    store.write_envelope(
        JobEnvelope(
            job_id="stale-destroy",
            workload=Workload.FAILED,
            offload=Offload.FAILED,
            cleanup=Cleanup.LEFT_UP,
            supervisor=Supervisor.FINISHED,
            hints=[
                "VM left running deliberately and is still billing: "
                "`mighty-colab job destroy stale-destroy` when done",
                "check, in order: the URL has not expired; the object exists",
            ],
        )
    )

    result = runner.invoke(app, ["job", "destroy", "stale-destroy"])

    assert result.exit_code == 0
    env = store.read_envelope("stale-destroy")
    assert env.cleanup is Cleanup.ALREADY_ABSENT
    assert not any("still billing" in h for h in env.hints)
    # Non-billing diagnostic hints survive -- this isn't a blanket reset.
    assert "check, in order: the URL has not expired; the object exists" in env.hints

def _job_json(result):
    return json.loads(_clean(result.output).strip().splitlines()[-1])


def test_job_help_summary_names_every_subcommand():
    job_result = runner.invoke(app, ["job", "--help"], env={"COLUMNS": "200"})
    jobs_result = runner.invoke(app, ["jobs", "--help"], env={"COLUMNS": "200"})

    assert job_result.exit_code == 0
    assert "plan, apply, status, destroy" in _clean(job_result.output)
    assert jobs_result.exit_code == 0
    assert "list, prune" in _clean(jobs_result.output)


@pytest.mark.parametrize(
    ("args", "command"),
    [
        (["job", "plan", "missing.yaml"], "job plan"),
        (["job", "apply", "missing-plan.json"], "job apply"),
        (["job", "status", "missing-job"], "job status"),
    ],
)
def test_expected_job_errors_emit_valid_json(args, command, mock_common_state):
    _json_mode(mock_common_state)

    result = runner.invoke(app, args)

    payload = _job_json(result)
    assert result.exit_code == 1
    assert payload["command"] == command
    assert payload["status"] == "error"
    assert payload["exit_code"] == 1
    assert payload["message"]


def test_destroy_missing_job_json_is_a_successful_noop(mock_common_state):
    _json_mode(mock_common_state)

    result = runner.invoke(app, ["job", "destroy", "missing-job"])

    payload = _job_json(result)
    assert result.exit_code == 0
    assert payload["command"] == "job destroy"
    assert payload["status"] == "ok"
    assert payload["exit_code"] == 0


def test_failed_apply_json_matches_process_status(
    tmp_path, monkeypatch, mock_common_state
):
    from colab_cli.job.orchestrator import Orchestrator, PhaseError

    plan_file = _locked_plan(tmp_path, "json-failed")
    _json_mode(mock_common_state)

    def fail_provision(_self):
        raise PhaseError(Phase.PROVISION, "assignment rejected", RetryClass.RETRY_SAME)

    monkeypatch.setattr(Orchestrator, "provision", fail_provision)

    result = runner.invoke(app, ["job", "apply", str(plan_file)])

    payload = _job_json(result)
    assert result.exit_code == 1
    assert payload["status"] == "error"
    assert payload["exit_code"] == 1
    assert payload["job"]["workload"] == "failed"
    assert payload["ok"] is False


def test_status_json_keeps_query_success_distinct_from_failed_workload(
    mock_common_state,
):
    from colab_cli.commands.job import _store
    from colab_cli.job.models import Cleanup, JobEnvelope, Supervisor

    _store().write_envelope(
        JobEnvelope(
            job_id="failed-job",
            workload=Workload.FAILED,
            offload=Offload.NOT_REQUIRED,
            cleanup=Cleanup.RELEASED,
            supervisor=Supervisor.FINISHED,
            exit_code=1,
        )
    )
    _json_mode(mock_common_state)

    result = runner.invoke(app, ["job", "status", "failed-job"])

    payload = _job_json(result)
    assert result.exit_code == 0
    assert payload["status"] == "ok"
    assert payload["exit_code"] == 0
    assert payload["job"]["exit_code"] == 1
    assert payload["ok"] is False


def test_destroy_cleanup_failure_json_matches_process_status(mock_common_state):
    from colab_cli.commands.job import _store
    from colab_cli.job.models import Cleanup, JobEnvelope, Supervisor

    _store().write_envelope(
        JobEnvelope(
            job_id="cleanup-failed",
            endpoint="m-endpoint",
            workload=Workload.SUCCEEDED,
            offload=Offload.NOT_REQUIRED,
            cleanup=Cleanup.PENDING,
            supervisor=Supervisor.FINISHED,
            exit_code=0,
        )
    )
    mock_common_state.client.unassign.side_effect = RuntimeError("backend unavailable")
    _json_mode(mock_common_state)

    result = runner.invoke(app, ["job", "destroy", "cleanup-failed"])

    payload = _job_json(result)
    assert result.exit_code == 1
    assert payload["status"] == "error"
    assert payload["exit_code"] == 1
    assert payload["job"]["cleanup"] == "failed"


def test_list_and_nonterminal_status_json_are_query_successes(mock_common_state):
    from colab_cli.commands.job import _store
    from colab_cli.job.models import JobEnvelope

    _store().write_envelope(JobEnvelope(job_id="still-running"))
    _json_mode(mock_common_state)

    listed = runner.invoke(app, ["jobs", "list"])
    status_result = runner.invoke(app, ["job", "status", "still-running"])

    list_payload = _job_json(listed)
    status_payload = _job_json(status_result)
    assert listed.exit_code == 0
    assert list_payload["command"] == "jobs list"
    assert list_payload["jobs"][0]["job_id"] == "still-running"
    assert status_result.exit_code == 0
    assert status_payload["command"] == "job status"
    assert status_payload["status"] == "ok"
    assert status_payload["done"] is False



def test_jobs_list_json_does_not_hide_offload_cleanup_phase_or_reason(
    mock_common_state,
):
    """The JSON row previously carried only job_id/workload/done/endpoint --
    thinner than the plain-text rendering, which already showed
    workload/offload/cleanup/done. A JSON consumer (including the
    `jobs://` MCP resource, which reuses this same row builder) must see
    at least as much as a human reading the plain-text output does.
    """
    from colab_cli.commands.job import _store
    from colab_cli.job.models import Cleanup, JobEnvelope, Offload, Supervisor, Workload

    store = _store()
    (store.job_dir("unapplied")).mkdir(parents=True)
    store.write_envelope(
        JobEnvelope(
            job_id="rich-row",
            workload=Workload.FAILED,
            offload=Offload.SKIPPED,
            cleanup=Cleanup.RELEASED,
            supervisor=Supervisor.FINISHED,
            reason="staging failed: inputs/x.npz: http_error (403)",
            endpoint="gpu-a100-example",
        )
    )
    _json_mode(mock_common_state)

    result = runner.invoke(app, ["jobs", "list"])

    payload = _job_json(result)
    rows = {r["job_id"]: r for r in payload["jobs"]}
    assert result.exit_code == 0
    rich = rows["rich-row"]
    assert rich["workload"] == "failed"
    assert rich["offload"] == "skipped"
    assert rich["cleanup"] == "released"
    assert rich["endpoint"] == "gpu-a100-example"
    assert rich["reason"] == "staging failed: inputs/x.npz: http_error (403)"
    unapplied = rows["unapplied"]
    assert unapplied["workload"] is None
    assert unapplied["reason"] == "planned, not applied"


def test_jobs_list_running_and_done_filters(mock_common_state):
    from colab_cli.commands.job import _store
    from colab_cli.job.models import Cleanup, JobEnvelope, Offload, Supervisor, Workload

    store = _store()
    (store.job_dir("planned-only")).mkdir(parents=True)
    store.write_envelope(JobEnvelope(job_id="still-running", workload=Workload.RUNNING))
    store.write_envelope(
        JobEnvelope(
            job_id="done-job",
            workload=Workload.SUCCEEDED,
            offload=Offload.OK,
            cleanup=Cleanup.RELEASED,
            supervisor=Supervisor.FINISHED,
        )
    )
    _json_mode(mock_common_state)

    running_result = runner.invoke(app, ["jobs", "list", "--running"])
    done_result = runner.invoke(app, ["jobs", "list", "--done"])
    both_result = runner.invoke(app, ["jobs", "list", "--running", "--done"])

    running_ids = {r["job_id"] for r in _job_json(running_result)["jobs"]}
    done_ids = {r["job_id"] for r in _job_json(done_result)["jobs"]}
    assert running_result.exit_code == 0
    assert running_ids == {"planned-only", "still-running"}
    assert done_result.exit_code == 0
    assert done_ids == {"done-job"}
    assert both_result.exit_code == 1

def test_prune_dry_run_reports_without_deleting(mock_common_state):
    from colab_cli.commands.job import _store
    from colab_cli.job.models import Cleanup, JobEnvelope, Offload, Supervisor, Workload

    store = _store()
    # Unapplied plan: `job plan` writes plan.json/spec.json but never an
    # envelope; the directory existing with no envelope is what matters.
    (store.job_dir("planned-only")).mkdir(parents=True)
    store.write_envelope(
        JobEnvelope(
            job_id="done-released",
            workload=Workload.SUCCEEDED,
            offload=Offload.OK,
            cleanup=Cleanup.RELEASED,
            supervisor=Supervisor.FINISHED,
        )
    )

    result = runner.invoke(app, ["jobs", "prune", "--dry-run"])

    assert result.exit_code == 0
    assert "would remove: planned-only" in result.output
    assert "would remove: done-released" in result.output
    # Nothing actually deleted.
    assert set(store.list_jobs()) == {"planned-only", "done-released"}


def test_prune_removes_safe_and_keeps_running_or_left_up(mock_common_state):
    from colab_cli.commands.job import _store
    from colab_cli.job.models import Cleanup, JobEnvelope, Offload, Supervisor, Workload

    store = _store()
    (store.job_dir("planned-only")).mkdir(parents=True)
    store.write_envelope(
        JobEnvelope(
            job_id="done-released",
            workload=Workload.SUCCEEDED,
            offload=Offload.OK,
            cleanup=Cleanup.RELEASED,
            supervisor=Supervisor.FINISHED,
        )
    )
    store.write_envelope(
        JobEnvelope(
            job_id="done-already-absent",
            workload=Workload.FAILED,
            offload=Offload.SKIPPED,
            cleanup=Cleanup.ALREADY_ABSENT,
            supervisor=Supervisor.FINISHED,
        )
    )
    store.write_envelope(
        JobEnvelope(job_id="still-running", workload=Workload.RUNNING)
    )
    store.write_envelope(
        JobEnvelope(
            job_id="left-up-billing",
            workload=Workload.SUCCEEDED,
            offload=Offload.OK,
            cleanup=Cleanup.LEFT_UP,
            supervisor=Supervisor.FINISHED,
        )
    )

    result = runner.invoke(app, ["jobs", "prune"])

    assert result.exit_code == 0
    remaining = set(store.list_jobs())
    # Deliberately-billing and still-running records must survive a prune
    # unconditionally -- deleting "left-up-billing"'s record would destroy
    # the only local pointer to a VM that is still running and billing.
    assert remaining == {"still-running", "left-up-billing"}
    assert "skipped: still-running" in result.output
    assert "skipped: left-up-billing" in result.output
    assert "left_up" in result.output


def test_prune_cleanup_failed_is_kept_not_silently_removed(mock_common_state):
    from colab_cli.commands.job import _store
    from colab_cli.job.models import Cleanup, JobEnvelope, Offload, Supervisor, Workload

    store = _store()
    store.write_envelope(
        JobEnvelope(
            job_id="cleanup-failed",
            workload=Workload.FAILED,
            offload=Offload.SKIPPED,
            cleanup=Cleanup.FAILED,
            supervisor=Supervisor.FINISHED,
        )
    )

    result = runner.invoke(app, ["jobs", "prune"])

    assert result.exit_code == 0
    assert "cleanup-failed" in store.list_jobs()
    assert "skipped: cleanup-failed" in result.output
    assert "mighty-colab sessions" in result.output


def test_prune_json_reports_removed_and_skipped(mock_common_state):
    from colab_cli.commands.job import _store
    from colab_cli.job.models import Cleanup, JobEnvelope, Offload, Supervisor, Workload

    store = _store()
    store.write_envelope(
        JobEnvelope(
            job_id="done-released",
            workload=Workload.SUCCEEDED,
            offload=Offload.OK,
            cleanup=Cleanup.RELEASED,
            supervisor=Supervisor.FINISHED,
        )
    )
    store.write_envelope(
        JobEnvelope(job_id="still-running", workload=Workload.RUNNING)
    )
    _json_mode(mock_common_state)

    result = runner.invoke(app, ["jobs", "prune"])

    payload = _job_json(result)
    assert result.exit_code == 0
    assert payload["command"] == "jobs prune"
    assert payload["dry_run"] is False
    assert [r["job_id"] for r in payload["removed"]] == ["done-released"]
    assert [s["job_id"] for s in payload["skipped"]] == ["still-running"]