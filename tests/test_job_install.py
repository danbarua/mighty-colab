"""Dependency install: uv first, pip as the fallback, and failure
classification built from real Colab output.

tests/fixtures/installer_failures.json is captured from a Colab VM by
integration/capture_installer_failures; re-capture it when Colab's uv or
pip version changes.
"""

from __future__ import annotations

import json
import subprocess
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from colab_cli.job.models import RetryClass

FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "installer_failures.json").read_text()
)


def _fixture_attempt(case: str, installer: str) -> dict:
    run = FIXTURES["cases"][case][installer]
    return {
        "installer": installer,
        "version": FIXTURES[f"{installer}_version"],
        "command": run["command"],
        "index": {},
        "exit_code": run["exit_code"],
        "timed_out": False,
        "seconds": run["seconds"],
        "output_head": run["output_head"],
        "output_tail": run["output_tail"],
    }


@pytest.mark.parametrize(
    "case,installer,failure",
    [
        ("missing_package", "uv", "resolution"),
        ("missing_package", "pip", "resolution"),
        ("missing_version", "uv", "resolution"),
        ("missing_version", "pip", "resolution"),
        ("conflict", "uv", "resolution"),
        ("conflict", "pip", "resolution"),
        ("index_unresolvable", "uv", "transient"),
        ("index_unresolvable", "pip", "transient"),
        ("index_refused", "uv", "transient"),
        ("index_refused", "pip", "transient"),
        ("index_401", "uv", "auth"),
        ("index_401", "pip", "auth"),
        ("index_403", "uv", "auth"),
        # pip reports an index 403/429/500 only as "No matching distribution".
        ("index_403", "pip", "resolution"),
        ("index_429", "uv", "transient"),
        ("index_429", "pip", "resolution"),
        ("index_500", "uv", "transient"),
        ("index_500", "pip", "resolution"),
        ("build_failure", "uv", "build"),
        ("build_failure", "pip", "build"),
    ],
)
def test_classify_real_installer_output(case, installer, failure):
    from colab_cli.job.install import classify_attempt

    classified, key_lines = classify_attempt(_fixture_attempt(case, installer))

    assert classified == failure
    assert key_lines, "an operator needs the installer's own error lines"


@pytest.mark.parametrize(
    "case,retry_class,failure",
    [
        ("missing_package", RetryClass.FIX_CODE, "resolution"),
        ("conflict", RetryClass.FIX_CODE, "resolution"),
        ("index_unresolvable", RetryClass.RETRY_SAME, "transient"),
        ("index_401", RetryClass.FIX_HUMAN, "auth"),
        # uv names the status; pip's "no matching distribution" must not win.
        ("index_403", RetryClass.FIX_HUMAN, "auth"),
        ("index_429", RetryClass.RETRY_SAME, "transient"),
        ("index_500", RetryClass.RETRY_SAME, "transient"),
        ("build_failure", RetryClass.FIX_CODE, "build"),
    ],
)
def test_verdict_combines_uv_and_pip(case, retry_class, failure):
    from colab_cli.job.install import classify_attempt, install_verdict

    attempts = []
    for installer in ("uv", "pip"):
        attempt = _fixture_attempt(case, installer)
        attempt["failure"], attempt["key_lines"] = classify_attempt(attempt)
        attempts.append(attempt)

    assert install_verdict(attempts) == (retry_class, failure)


def test_a_pip_retry_that_recovered_is_not_transient():
    from colab_cli.job.install import classify_attempt

    attempt = _fixture_attempt("missing_package", "pip")
    attempt["output_tail"] = (
        "WARNING: Retrying (Retry(total=4, connect=None, read=None, redirect=None, "
        "status=None)) after connection broken by 'ReadTimeoutError(...)': /simple/x/\n"
        + attempt["output_tail"]
    )

    assert classify_attempt(attempt)[0] == "resolution"


def test_an_attempt_past_its_budget_is_a_timeout():
    from colab_cli.job.install import classify_attempt, install_verdict

    attempt = _fixture_attempt("build_failure", "uv")
    attempt.update(exit_code=None, timed_out=True, output_tail="Building flash-attn==2.6.3\n")
    attempt["failure"], attempt["key_lines"] = classify_attempt(attempt)

    assert attempt["failure"] == "timeout"
    assert install_verdict([attempt]) == (RetryClass.FIX_CODE, "timeout")


def test_the_kernel_call_outlasts_every_attempt():
    """A kernel reply timeout is classified as a transport failure
    (retry_same); an installer timeout is fix_code. The installers' own
    budgets must expire first."""
    from colab_cli.job import install

    assert install.INSTALL_KERNEL_TIMEOUT > (
        2 * install.INSTALL_ATTEMPT_TIMEOUT + 3 * install.INSTALL_PROBE_TIMEOUT
    )


class _FakeInstallers:
    """Stands in for subprocess.run inside the generated kernel code.
    `results` maps installer name to an exit code, or to "timeout"."""

    def __init__(self, results):
        self.results = results
        self.ran = []

    def __call__(self, cmd, stdin=None, stdout=None, stderr=None, timeout=None, **kw):
        assert stdin is subprocess.DEVNULL, "installers must never wait for input"
        name = "uv" if "uv" in Path(cmd[0]).name else "pip"
        if "--version" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout=f"{name} 9.9\n", stderr="")
        if cmd[-2:] == ["config", "list"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="global.index-url='https://ci:TOKEN2@mirror.example/simple'\n", stderr=""
            )
        self.ran.append(name)
        stdout.write(f"{name} output line\n".encode() if "b" in getattr(stdout, "mode", "") else f"{name} output line\n")
        stdout.flush()
        outcome = self.results[name]
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(cmd, timeout)
        return subprocess.CompletedProcess(cmd, outcome)


def _run_kernel_code(tmp_path, monkeypatch, results, uv_present=True, deps=None):
    import shutil

    from colab_cli.job.install import install_code

    log_path = tmp_path / "vm" / "install.log"
    code = install_code(str(log_path), deps or ["torch==2.4.1"])
    fake = _FakeInstallers(results)
    monkeypatch.setattr(subprocess, "run", fake)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/local/bin/uv" if uv_present and name == "uv" else None)
    printed = StringIO()
    with redirect_stdout(printed):
        exec(code, {"__name__": "__main__"})
    line = next(out for out in printed.getvalue().splitlines() if out.startswith("INSTALL_RESULT="))
    return json.loads(line.split("=", 1)[1]), log_path.read_text(), fake


def test_kernel_code_falls_back_to_pip_when_uv_fails(tmp_path, monkeypatch):
    attempts, log, fake = _run_kernel_code(tmp_path, monkeypatch, {"uv": 1, "pip": 0})

    assert fake.ran == ["uv", "pip"]
    assert [a["installer"] for a in attempts] == ["uv", "pip"]
    assert [a["exit_code"] for a in attempts] == [1, 0]
    assert log.count("=== mighty-colab install attempt ") == 2
    assert log.count("=== mighty-colab install result ") == 2
    assert "uv output line" in attempts[0]["output_tail"]
    assert attempts[1]["version"] == "pip 9.9"


def test_kernel_code_records_both_failures(tmp_path, monkeypatch):
    attempts, _log, fake = _run_kernel_code(tmp_path, monkeypatch, {"uv": 1, "pip": 1})

    assert fake.ran == ["uv", "pip"]
    assert [a["exit_code"] for a in attempts] == [1, 1]


def test_kernel_code_uses_pip_when_uv_is_absent(tmp_path, monkeypatch):
    attempts, _log, fake = _run_kernel_code(
        tmp_path, monkeypatch, {"pip": 0}, uv_present=False
    )

    assert fake.ran == ["pip"]
    assert [a["installer"] for a in attempts] == ["pip"]


def test_kernel_code_runs_pip_after_a_uv_timeout(tmp_path, monkeypatch):
    attempts, log, fake = _run_kernel_code(tmp_path, monkeypatch, {"uv": "timeout", "pip": 0})

    assert fake.ran == ["uv", "pip"]
    assert attempts[0]["timed_out"] is True
    assert attempts[0]["exit_code"] is None
    assert '"timed_out": true' in log


def test_kernel_code_redacts_credentials_in_the_log_header(tmp_path, monkeypatch):
    """A direct-reference dep can carry a token as userinfo, and pip.conf
    can name an index with one."""
    attempts, log, _fake = _run_kernel_code(
        tmp_path,
        monkeypatch,
        {"uv": 1, "pip": 1},
        deps=["pkg @ https://bot:TOKEN1@pkgs.example/pkg-1.0-py3-none-any.whl"],
    )

    assert "TOKEN1" not in log and "TOKEN2" not in log
    assert "https://***@pkgs.example/pkg-1.0-py3-none-any.whl" in log
    assert attempts[0]["command"][-1] == "pkg @ https://***@pkgs.example/pkg-1.0-py3-none-any.whl"
    assert attempts[1]["index"]["pip.conf global.index-url"] == "https://***@mirror.example/simple"
    assert "pip.conf global.index-url" not in attempts[0]["index"], "uv does not read pip.conf"


def _spec(**kw):
    from colab_cli.job.models import Accelerator, Budgets, CodeSpec, JobSpec

    base = dict(
        name="unit",
        code=CodeSpec(kind="file", entry="train.py"),
        accelerator=Accelerator(prefer=["T4"], accept_cpu=False),
        budgets=Budgets(wall_clock=60),
    )
    base.update(kw)
    return JobSpec(**base)


def _orch(tmp_path, spec, runtime):
    from colab_cli.job.models import Plan
    from colab_cli.job.orchestrator import Orchestrator
    from colab_cli.job.store import JobStore

    return Orchestrator(
        plan=Plan(job_id="unit-job", spec_hash="deadbeef", created_at="now", spec=spec),
        store=JobStore(tmp_path / "jobs"),
        client=MagicMock(),
        runtime_factory=lambda url, token: runtime,
        session_store=MagicMock(),
        transport_factory=lambda _s: MagicMock(),
    )


def _orch_with_install_result(tmp_path, attempts):
    rt = MagicMock()
    rt.execute_code.return_value = [
        {"output_type": "stream", "text": "INSTALL_RESULT=" + json.dumps(attempts) + "\n"}
    ]
    orch = _orch(tmp_path, spec=_spec(deps=["mighty-colab-no-such-package==0.0.1"]), runtime=rt)
    orch.session_state = SimpleNamespace(url="https://u", token="t")
    return orch


def test_install_failure_reports_each_attempt_and_its_cause(tmp_path):
    from colab_cli.job.orchestrator import PhaseError

    attempts = [_fixture_attempt("missing_package", "uv"), _fixture_attempt("missing_package", "pip")]
    orch = _orch_with_install_result(tmp_path, attempts)

    with pytest.raises(PhaseError) as exc:
        orch.install()

    assert exc.value.retry_class is RetryClass.FIX_CODE
    reason = exc.value.reason
    assert "mighty-colab-no-such-package==0.0.1" in reason
    assert "uv 0.12.15" in reason and "pip 24.1.2" in reason
    assert "No solution found when resolving dependencies" in reason
    assert "No matching distribution found" in reason
    assert len(reason) <= 1500
    assert any("install.log" in h for h in exc.value.hints)
    recorded = orch.env.install_attempts
    assert [a.installer for a in recorded] == ["uv", "pip"]
    assert all(a.failure == "resolution" for a in recorded)


def test_install_failure_from_an_unreachable_index_is_retry_same(tmp_path):
    from colab_cli.job.orchestrator import PhaseError

    attempts = [_fixture_attempt("index_429", "uv"), _fixture_attempt("index_429", "pip")]
    orch = _orch_with_install_result(tmp_path, attempts)

    with pytest.raises(PhaseError) as exc:
        orch.install()

    assert exc.value.retry_class is RetryClass.RETRY_SAME
    assert "429 Too Many Requests" in exc.value.reason


def test_recorded_attempts_never_keep_index_credentials(tmp_path):
    from colab_cli.job.orchestrator import PhaseError

    attempt = _fixture_attempt("missing_package", "pip")
    attempt["command"] = attempt["command"] + ["pkg @ https://bot:TOKEN@pkgs.example/pkg.whl"]
    attempt["index"] = {"PIP_INDEX_URL": "https://bot:TOKEN@pkgs.example/simple"}
    attempt["output_tail"] += "\nLooking in indexes: https://bot:TOKEN@pkgs.example/simple\n"
    orch = _orch_with_install_result(tmp_path, [attempt])

    with pytest.raises(PhaseError) as exc:
        orch.install()

    assert "TOKEN" not in exc.value.reason
    assert "TOKEN" not in orch.env.model_dump_json()


def test_a_uv_success_is_a_hint_not_an_attempt_record(tmp_path):
    attempt = _fixture_attempt("missing_package", "uv")
    attempt.update(exit_code=0, output_tail="Installed 1 package in 12ms\n")
    orch = _orch_with_install_result(tmp_path, [attempt])

    orch.install()

    assert orch.env.install_attempts == []
    assert "install_attempts" not in orch.env.model_dump_json()
    assert any("installed with uv 0.12.15" in h for h in orch.env.hints)


def test_a_pip_fallback_success_keeps_both_attempts(tmp_path):
    failed_uv = _fixture_attempt("index_500", "uv")
    pip_ok = _fixture_attempt("missing_package", "pip")
    pip_ok.update(exit_code=0, output_tail="Successfully installed x-1.0\n")
    orch = _orch_with_install_result(tmp_path, [failed_uv, pip_ok])

    orch.install()

    assert [a.installer for a in orch.env.install_attempts] == ["uv", "pip"]
    assert orch.env.install_attempts[0].failure == "transient"
    assert orch.env.install_attempts[1].failure is None
    assert any("uv failed (transient)" in h and "pip" in h for h in orch.env.hints)


def test_install_step_that_fails_before_any_installer_ran(tmp_path):
    """The kernel code itself raised (a full disk, a broken VM): not the
    user's pins."""
    from colab_cli.job.orchestrator import PhaseError
    rt = MagicMock()
    rt.execute_code.return_value = [
        {"output_type": "error", "ename": "OSError", "evalue": "[Errno 28] No space left on device", "traceback": []}
    ]
    orch = _orch(tmp_path, spec=_spec(deps=["torch==2.4.1"]), runtime=rt)
    orch.session_state = SimpleNamespace(url="https://u", token="t")

    with pytest.raises(PhaseError) as exc:
        orch.install()

    assert exc.value.retry_class is RetryClass.DO_NOT_RETRY
    assert "No space left on device" in exc.value.reason



def test_a_pip_build_failure_reason_names_the_builds_own_error(tmp_path):
    """pip -v puts generic boilerplate at the end of a failed build; the
    build's own error is near the top, above
    `error: subprocess-exited-with-error`. It must reach the reason."""
    from colab_cli.job.orchestrator import PhaseError

    attempts = [_fixture_attempt("build_failure", "uv"), _fixture_attempt("build_failure", "pip")]
    orch = _orch_with_install_result(tmp_path, attempts)

    with pytest.raises(PhaseError) as exc:
        orch.install()

    assert exc.value.retry_class is RetryClass.FIX_CODE
    assert "mc_build_fails: build failed on purpose" in exc.value.reason
    pip_lines = orch.env.install_attempts[1].key_lines
    assert "mc_build_fails: build failed on purpose" in pip_lines


def test_envelope_file_omits_install_attempts_when_there_are_none(tmp_path):
    """Older CLIs read envelope.json from disk with extra fields forbidden.
    A job whose install succeeded on the first try must stay readable."""
    from colab_cli.job.models import InstallAttempt, JobEnvelope
    from colab_cli.job.store import JobStore

    store = JobStore(tmp_path / "jobs")
    store.write_envelope(JobEnvelope(job_id="first-try"))
    assert "install_attempts" not in (store.job_dir("first-try") / "envelope.json").read_text()

    attempt = InstallAttempt(installer="uv", version="uv 0.12.15", command=["uv"], seconds=1.0, exit_code=1, failure="resolution")
    store.write_envelope(JobEnvelope(job_id="failed", install_attempts=[attempt]))
    assert store.read_envelope("failed").install_attempts == [attempt]
