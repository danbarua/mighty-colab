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
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import shutil

import subprocess
import sys
import time
from pathlib import Path

import pytest

RUNTIME_SOURCE = Path(__file__).parents[1] / "src/colab_cli/job/runtime_payload"


def _clean_workload():
    from colab_cli.job.runtime_payload import ident

    return "succeeded" if ident.can_detect_escapees() else "unknown"


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


@pytest.mark.parametrize(
    "url",
    [
        "https://EXAMPLE.com/object?sig=x",
        "https://user:password@Example.COM:8443/object?sig=x",
        "https://[2001:db8::1]:9443/object?sig=x",
    ],
)
def test_runtime_url_identity_matches_safe_planner_identity(url):
    from colab_cli.job.runtime_payload.runner import _url_id
    from colab_cli.job.spec_io import url_id

    identity = _url_id(url)
    assert identity == url_id(url)
    assert "user" not in identity
    assert "password" not in identity
    assert "?" not in identity


def test_hashing_reader_bounds_default_reads(tmp_path):
    from colab_cli.job.runtime_payload.runner import _HashingReader

    path = tmp_path / "blob.bin"
    payload = b"x" * (2 * 65536 + 17)
    path.write_bytes(payload)
    with path.open("rb") as fh:
        reader = _HashingReader(fh)
        chunks = []
        while True:
            chunk = reader.read()
            if not chunk:
                break
            chunks.append(chunk)
    assert [len(chunk) for chunk in chunks] == [65536, 65536, 17]
    assert b"".join(chunks) == payload
    assert reader.size == len(payload)
    assert reader.hasher.hexdigest() == hashlib.sha256(payload).hexdigest()


def test_http_get_streams_in_bounded_chunks(tmp_path, monkeypatch):
    from colab_cli.job.runtime_payload import runner

    payload = b"x" * (2 * 1024 * 1024 + 17)

    class Response:
        def __init__(self):
            self.offset = 0
            self.read_sizes = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, size):
            self.read_sizes.append(size)
            chunk = payload[self.offset : self.offset + size]
            self.offset += len(chunk)
            return chunk

    response = Response()
    monkeypatch.setattr(runner, "urlopen_public", lambda *_args, **_kwargs: response)
    target = tmp_path / "download.bin"

    size, digest = runner._http_get_to_file(
        "https://example.com/object",
        str(target),
        expected_size=len(payload),
        expected_hash=hashlib.sha256(payload).hexdigest(),
    )

    assert target.read_bytes() == payload
    assert size == len(payload)
    assert digest == hashlib.sha256(payload).hexdigest()
    assert response.read_sizes == [1024 * 1024] * 4
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
    assert result["workload"] == _clean_workload()
    assert result["exit_code"] == 0
    assert result["offload"] == "not_required"
    assert not (job_dir / "exception.json").exists()
def test_terminal_result_identifies_cli_and_exact_runtime_payload(tmp_path):
    package, entry, first_job = _prepare(tmp_path, "raise SystemExit(0)\n")
    _proc, first, _job_dir = _run_prepared(
        tmp_path, entry, first_job, "--cli-version", "1.2.3"
    )

    shim = package / "shim.py"
    shim.write_text(shim.read_text() + "\n")
    _proc, second, _job_dir = _run_prepared(
        tmp_path, entry, tmp_path / "second-job", "--cli-version", "1.2.3"
    )

    assert first["schema_version"] == second["schema_version"] == "2"
    assert first["cli_version"] == second["cli_version"] == "1.2.3"
    assert first["runtime_payload_version"].startswith("sha256:")
    assert first["runtime_payload_version"] != second["runtime_payload_version"]


def test_required_secret_channel_missing_fails_before_consumer(tmp_path):
    marker = tmp_path / "consumer-started"
    source = f"from pathlib import Path; Path({str(marker)!r}).write_text('started')\n"

    _proc, result, job_dir = _run(tmp_path, source, "--secrets-required")

    assert result["workload"] == "failed"
    assert result["schema_version"] == "2"
    assert result["cli_version"] == "unknown"
    assert result["runtime_payload_version"].startswith("sha256:")
    assert not marker.exists()
    assert result["exception"]["message"] == "stage failed"
    assert result["runner_error"] == "stage failed"


def test_invalid_secret_channel_writes_provenanced_stage_failure(tmp_path):
    _package, entry, job_dir = _prepare(tmp_path, "raise SystemExit(0)\n")
    secret_path = tmp_path / "invalid-transfer.json"
    secret_path.write_text("{")
    secret_fd = os.open(secret_path, os.O_RDONLY)
    try:
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "mighty_runtime.runner",
                "--job-dir",
                str(job_dir),
                "--cli-version",
                "9.8.7",
                "--secrets-fd",
                str(secret_fd),
                str(entry),
            ],
            cwd=tmp_path,
            env=_runtime_env(tmp_path),
            pass_fds=(secret_fd,),
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        os.close(secret_fd)

    result = json.loads((job_dir / "result.json").read_text())
    assert proc.returncode == 0
    assert result["schema_version"] == "2"
    assert result["cli_version"] == "9.8.7"
    assert result["runtime_payload_version"].startswith("sha256:")
    assert result["workload"] == "failed"
    assert result["phase"] == "stage"
    assert "invalid private transfer configuration" not in proc.stderr


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
    assert result["workload"] == _clean_workload()
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


def test_staged_payload_and_runner_share_a_secret_channel_without_persisting_it(
    tmp_path, monkeypatch
):
    from colab_cli.job import payload_bundle
    from colab_cli.job.models import (
        ArtifactItem,
        CodeSpec,
        Control,
        ControlChannel,
        DataItem,
        JobSpec,
    )

    sentinel = "ISSUE18_RUNNER_SENTINEL"
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append(("GET", self.path, None))
            body = b"staged payload"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_PUT(self):
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            received.append(("PUT", self.path, body))
            self.send_response(200)
            self.end_headers()

        def log_message(self, _format, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    data_url = f"{base}/input?sig={sentinel}-data"
    artifact_url = f"{base}/artifact?sig={sentinel}-artifact"
    result_url = f"{base}/result?sig={sentinel}-result"
    recovery_sentinel = "ISSUE18_RECOVERY_ONLY_SENTINEL"
    recovery_url = f"{base}/result?sig={recovery_sentinel}"

    source_root = tmp_path / "source"
    source_root.mkdir()
    remote_dir = tmp_path / "remote"
    entry_source = source_root / "train.py"
    entry_source.write_text(
        "import os, sys\n"
        "from pathlib import Path\n"
        "marker = 'ISSUE18_' + 'RUNNER_SENTINEL'\n"
        "assert marker not in repr((sys.argv, dict(os.environ)))\n"
        "parent_env = Path('/proc/%d/environ' % os.getppid())\n"
        "if parent_env.exists(): assert marker.encode() not in parent_env.read_bytes()\n"
        f"job = Path({str(remote_dir)!r})\n"
        "assert not (job / 'mighty_runtime/.secrets/transfer.json').exists()\n"
        "assert (job / 'input.bin').read_bytes() == b'staged payload'\n"
        "(job / 'output.bin').write_bytes(b'artifact')\n"
        "assert not any(marker.encode() in p.read_bytes() "
        "for p in job.rglob('*') if p.is_file())\n"
    )
    spec = JobSpec(
        name="producer-consumer",
        code=CodeSpec(kind="bundle", root=str(source_root), entry="train.py"),
        data=[DataItem(url=data_url, dest="input.bin", size_bytes=14)],
        artifacts=[ArtifactItem(path="output.bin", url=artifact_url)],
        control=Control(
            result=ControlChannel(put_url=result_url, get_url=recovery_url)
        ),
    )

    class LocalContents:
        def makedirs(self, path):
            Path(path).mkdir(parents=True, exist_ok=True)

        def upload(self, source, destination):
            Path(destination).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)

    payload_bundle.stage_payload(
        spec=spec,
        job_id="producer-consumer",
        transport=LocalContents(),
        remote_dir=str(remote_dir),
    )
    # This harness is a loopback HTTP server, not a public destination.
    # Production runner policy would refuse 127.0.0.1; the secret-channel
    # contract is what this test is proving.
    staged_policy = remote_dir / "mighty_runtime" / "netpolicy.py"
    staged_policy.write_text(
        staged_policy.read_text()
        + "\ndef urlopen_public(req, timeout):\n"
        + "    import urllib.request as _ur\n"
        + "    return _ur.urlopen(req, timeout=timeout)\n"
    )


    secret_path = remote_dir / "mighty_runtime/.secrets/transfer.json"
    secret_path.chmod(0o600)  # production seal_secret_channel step
    secret_fd = os.open(secret_path, os.O_RDONLY)
    secret_path.unlink()
    bootstrap = (
        f"import runpy,sys;sys.path.insert(0,{str(remote_dir)!r});"
        "runpy.run_module('mighty_runtime.runner',run_name='__main__')"
    )
    try:
        proc = subprocess.run(
            [
                sys.executable,
                "-I",
                "-S",
                "-c",
                bootstrap,
                "--job-dir",
                str(remote_dir),
                "--cli-version",
                "installed-1.2.3",
                "--stage-manifest",
                str(remote_dir / "stage.manifest.json"),
                "--offload-manifest",
                str(remote_dir / "offload.manifest.json"),
                "--secrets-fd",
                str(secret_fd),
                str(remote_dir / "src/train.py"),
            ],
            cwd=remote_dir,
            pass_fds=(secret_fd,),
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        os.close(secret_fd)
        server.shutdown()
        thread.join(timeout=5)

    result = json.loads((remote_dir / "result.json").read_text())
    assert proc.returncode == 0
    assert result["workload"] == _clean_workload()
    assert result["offload"] == "ok"
    requests = {(method, path): body for method, path, body in received}
    assert set(requests) == {
        ("GET", f"/input?sig={sentinel}-data"),
        ("PUT", f"/artifact?sig={sentinel}-artifact"),
        ("PUT", f"/result?sig={sentinel}-result"),
    }
    assert requests[("PUT", f"/artifact?sig={sentinel}-artifact")] == b"artifact"
    uploaded_result = json.loads(requests[("PUT", f"/result?sig={sentinel}-result")])
    assert uploaded_result["workload"] == _clean_workload()
    assert uploaded_result["schema_version"] == "2"
    assert uploaded_result["cli_version"] == "installed-1.2.3"
    assert uploaded_result["runtime_payload_version"] == result["runtime_payload_version"]
    assert sentinel not in proc.stdout
    assert recovery_sentinel not in repr(received)
    for path in remote_dir.rglob("*"):
        if path.is_file():
            assert recovery_sentinel.encode() not in path.read_bytes(), path
    assert sentinel not in proc.stderr
    for path in remote_dir.rglob("*"):
        if path.is_file():
            assert sentinel.encode() not in path.read_bytes(), path


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


def test_succeeded_is_refused_when_escapee_detection_is_unavailable():
    from colab_cli.job.runtime_payload.runner import _classify_workload

    workload, reason = _classify_workload(
        "succeeded", tagged=[], detect_ok=False
    )
    assert workload == "unknown"
    assert reason == "escapee detection unavailable"


def test_succeeded_is_refused_while_a_tagged_descendant_survives():
    from colab_cli.job.runtime_payload.runner import _classify_workload

    workload, reason = _classify_workload(
        "succeeded", tagged=[4242], detect_ok=True
    )
    assert workload == "failed"
    assert "survived" in reason
    cancelled, _reason = _classify_workload(
        "cancelled", tagged=[4242], detect_ok=True
    )
    assert cancelled == "cancelled"


@pytest.mark.skipif(
    not (sys.platform.startswith("linux") and Path("/proc").is_dir()),
    reason="setsid containment requires Linux /proc",
)
def test_setsid_grandchild_is_dead_before_the_terminal_result(tmp_path):
    marker = tmp_path / "escapee.pid"
    source = (
        "import os, time\n"
        "from pathlib import Path\n"
        f"marker = Path({str(marker)!r})\n"
        "if os.fork() == 0:\n"
        "    os.setsid()\n"
        "    if os.fork() == 0:\n"
        "        marker.write_text(str(os.getpid()))\n"
        "        time.sleep(30)\n"
        "        os._exit(0)\n"
        "    os._exit(0)\n"
        "for _ in range(50):\n"
        "    if marker.exists():\n"
        "        break\n"
        "    time.sleep(0.05)\n"
    )
    _proc, result, _job_dir = _run(tmp_path, source, timeout=40)
    assert marker.exists()
    pid = int(marker.read_text())
    with pytest.raises(OSError):
        os.kill(pid, 0)
    assert result["surviving_descendants"] == []
    assert result["workload"] == "succeeded"
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
    assert result["workload"] == _clean_workload()
    assert json.loads((tmp_path / "args.json").read_text()) == [
        "--job-dir",
        "/tmp/evil",
        "--deadline",
        "1",
    ]