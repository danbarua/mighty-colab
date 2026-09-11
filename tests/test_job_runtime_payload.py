"""Known-answer tests for the VM-side runtime package.

Each test extracts the runtime under its production name, ``mighty_runtime``,
and executes the real runner in a subprocess. No Colab or external network is
used.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import shutil

import subprocess
import sys
import time
from pathlib import Path

import pytest

RUNTIME_SOURCE = Path(__file__).parents[1] / "src/colab_cli/job/runtime_payload"


def _runtime_env(root: Path):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root)
    return env


def _prepare(tmp_path: Path, source: str):
    package = tmp_path / "mighty_runtime"
    shutil.copytree(RUNTIME_SOURCE, package)
    entry = tmp_path / "entry.py"
    entry.write_text(source)
    job_dir = tmp_path / "job"
    return package, entry, job_dir


def _run(tmp_path: Path, source: str, *extra: str, timeout=30):
    _package, entry, job_dir = _prepare(tmp_path, source)
    proc, result, job_dir = _run_prepared(tmp_path, entry, job_dir, *extra, timeout=timeout)
    return proc, result, job_dir


def _run_prepared(tmp_path: Path, entry: Path, job_dir: Path, *extra: str, timeout=30):
    extra_args = list(extra)
    if "--" in extra_args:
        separator = extra_args.index("--")
        command_args = extra_args[:separator] + ["--"] + extra_args[separator + 1 :]
        if "--entry" not in extra_args:
            command_args = extra_args[:separator] + [str(entry), "--"] + extra_args[separator + 1 :]
    else:
        command_args = extra_args + [str(entry)]
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "mighty_runtime.runner",
            "--job-dir",
            str(job_dir),
            *command_args,
        ],
        cwd=tmp_path,
        env=_runtime_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return proc, json.loads((job_dir / "result.json").read_text()), job_dir


def test_exit_zero_is_succeeded(tmp_path):
    proc, result, job_dir = _run(tmp_path, "raise SystemExit(0)\n")
    assert proc.returncode == 0
    assert result["workload"] == "succeeded"
    assert result["exit_code"] == 0
    assert result["offload"] == "not_required"
    assert not (job_dir / "exception.json").exists()


def test_uncaught_exception_is_failed_with_exception(tmp_path):
    proc, result, job_dir = _run(tmp_path, "raise ValueError('known failure')\n")
    assert proc.returncode == 0
    assert result["workload"] == "failed"
    assert result["exit_code"] == 1
    assert json.loads((job_dir / "exception.json").read_text())["type"] == "ValueError"


def test_os_exit_has_failed_code_without_exception(tmp_path):
    _proc, result, job_dir = _run(tmp_path, "import os; os._exit(7)\n")
    assert result["workload"] == "failed"
    assert result["exit_code"] == 7
    assert not (job_dir / "exception.json").exists()


def test_sigkill_is_failed_signal_nine(tmp_path):
    _proc, result, job_dir = _run(
        tmp_path,
        "import os, signal; os.kill(os.getpid(), signal.SIGKILL)\n",
    )
    assert result["workload"] == "failed"
    assert result["signal"] == 9
    assert not (job_dir / "exception.json").exists()


def test_sibling_import_and_real_file_work(tmp_path):
    _package, entry, job_dir = _prepare(
        tmp_path,
        "from sibling import VALUE\nfrom pathlib import Path\n"
        "assert __file__ == str(Path(__file__).resolve())\n"
        "Path('sibling-result').write_text(VALUE)\n",
    )
    (tmp_path / "sibling.py").write_text("VALUE = 'imported'\n")
    _proc, result, _job_dir = _run_prepared(tmp_path, entry, job_dir)
    assert result["workload"] == "succeeded"
    assert (tmp_path / "sibling-result").read_text() == "imported"


def test_duplicate_live_launch_is_noop(tmp_path):
    _package, entry, job_dir = _prepare(tmp_path, "import time; time.sleep(5)\n")
    first = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "mighty_runtime.runner",
            "--job-dir",
            str(job_dir),
            str(entry),
        ],
        cwd=tmp_path,
        env=_runtime_env(tmp_path),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        launch = job_dir / "launch.json"
        for _ in range(200):
            if launch.exists() and launch.stat().st_size:
                break
            time.sleep(0.01)
        assert launch.exists() and launch.stat().st_size
        duplicate = subprocess.run(
            [
                sys.executable,
                "-m",
                "mighty_runtime.runner",
                "--job-dir",
                str(job_dir),
                str(entry),
            ],
            cwd=tmp_path,
            env=_runtime_env(tmp_path),
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert duplicate.returncode == 0
        assert "duplicate launch" in duplicate.stdout
    finally:
        first.wait(timeout=20)
def test_external_cancel_terminates_the_running_consumer(tmp_path):
    _package, entry, job_dir = _prepare(tmp_path, "import time; time.sleep(60)\n")
    runner = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "mighty_runtime.runner",
            "--job-dir",
            str(job_dir),
            str(entry),
        ],
        cwd=tmp_path,
        env=_runtime_env(tmp_path),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        launch = job_dir / "launch.json"
        for _ in range(200):
            if launch.exists() and launch.stat().st_size:
                break
            time.sleep(0.01)
        assert launch.exists() and launch.stat().st_size

        (job_dir / "cancel.json").write_text(
            json.dumps({"intent": "cancelled", "by": "job destroy"})
        )
        runner.wait(timeout=10)

        result = json.loads((job_dir / "result.json").read_text())
        assert result["workload"] == "cancelled"
        assert result["signal"] == signal.SIGTERM
    finally:
        if runner.poll() is None:
            (job_dir / "cancel.json").write_text(
                json.dumps({"intent": "cancelled", "by": "test cleanup"})
            )
            runner.wait(timeout=10)


def test_watchdog_forwards_a_preexisting_cancel_intent(tmp_path, monkeypatch):
    from colab_cli.job.runtime_payload import watchdog

    job_dir = tmp_path / "job"
    job_dir.mkdir()
    (job_dir / "cancel.json").write_text('{"intent":"cancelled"}')
    killed = []
    records = 0

    monkeypatch.setattr(
        watchdog,
        "_runner_identity",
        lambda _job_dir: (os.getpid(), "", "", None, time.time()),
    )

    def record(*_args):
        nonlocal records
        records += 1
        if records == 2:
            (job_dir / "result.json").write_text("{}")

    monkeypatch.setattr(watchdog, "_record", record)
    monkeypatch.setattr(watchdog.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        watchdog,
        "_safe_killpg",
        lambda pgid, sig: killed.append((pgid, sig)),
    )

    assert watchdog.main(
        ["--job-dir", str(job_dir), "--shim-pgid", "4321", "--interval", "0.01"]
    ) == 0
    assert killed == [(4321, signal.SIGTERM)]


def test_stage_hash_mismatch_does_not_run_consumer(tmp_path):
    sentinel = tmp_path / "ran"
    source = tmp_path / "source.bin"
    source.write_bytes(b"actual")
    _package, entry, job_dir = _prepare(
        tmp_path,
        f"from pathlib import Path; Path({str(sentinel)!r}).write_text('ran')\n",
    )
    manifest = tmp_path / "stage.json"
    manifest.write_text(
        json.dumps(
            [
                {
                    "url": source.as_uri(),
                    "dest": "input.bin",
                    "sha256": hashlib.sha256(b"wrong").hexdigest(),
                }
            ]
        )
    )
    _proc, result, _job_dir = _run_prepared(
        tmp_path, entry, job_dir, "--stage-manifest", str(manifest)
    )
    assert result["phase"] == "stage"
    assert result["workload"] == "failed"
    assert not sentinel.exists()


def test_missing_optional_artifact_does_not_fail_offload(tmp_path):
    _package, entry, job_dir = _prepare(tmp_path, "raise RuntimeError('run failed')\n")
    manifest = tmp_path / "offload.json"
    manifest.write_text(
        json.dumps(
            [
                {
                    "path": "missing.bin",
                    "url": "file:///tmp/not-used",
                    "required": False,
                }
            ]
        )
    )
    _proc, result, _job_dir = _run_prepared(
        tmp_path, entry, job_dir, "--offload-manifest", str(manifest)
    )
    assert result["workload"] == "failed"
    assert result["phase"] == "run"
    assert result["offload"] == "ok"
    assert result["artifacts"][0]["status"] == "missing"


@pytest.mark.skipif(
    not (sys.platform.startswith("linux") and Path("/proc").is_dir()),
    reason="escapee detection requires Linux /proc",
)
def test_escapee_detection_is_available_on_linux(tmp_path):
    _proc, result, _job_dir = _run(tmp_path, "print('no escape')\n")
    assert result["escapee_detection_available"] is True
def test_consumer_args_after_separator_are_verbatim(tmp_path):
    _package, entry, job_dir = _prepare(
        tmp_path,
        "import json\nfrom pathlib import Path\n"
        "Path('args.json').write_text(json.dumps(__import__('sys').argv[1:]))\n",
    )
    _proc, result, _job_dir = _run_prepared(
        tmp_path,
        entry,
        job_dir,
        "--entry",
        str(entry),
        "--deadline",
        "30",
        "--",
        "--job-dir",
        "/tmp/evil",
        "--deadline",
        "1",
    )
    assert result["workload"] == "succeeded"
    assert json.loads((tmp_path / "args.json").read_text()) == [
        "--job-dir",
        "/tmp/evil",
        "--deadline",
        "1",
    ]