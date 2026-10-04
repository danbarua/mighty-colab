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
    message = "ManifestError: required transfer credential channel is missing"
    assert result["exception"]["message"] == message
    assert result["runner_error"] == message
    assert result["inputs"] == []


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
        lambda _job_dir: (os.getpid(), "", "", None, time.time(), None),
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
    # `file://` never reaches the hash check: urlopen_public's HTTPS-only
    # policy rejects it first. What this validates: a failed stage never
    # runs the consumer, and the input's record says why.
    [record] = result["inputs"]
    assert record["dest"] == "input.bin"
    assert record["status"] == "failed"
    assert record["error"]["category"] == "blocked"
    assert record["error"]["reason"] == "URL scheme must be https"
    assert result["exception"]["message"] == "input.bin: URL scheme must be https"
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
        + "\ndef _public_connection(url, timeout):\n"
        + "    import http.client as _hc\n"
        + "    from urllib.parse import urlsplit as _us\n"
        + "    _p = _us(url)\n"
        + "    return _hc.HTTPConnection(_p.hostname, _p.port, timeout=timeout), _p.hostname\n"
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


def _serve_bytes(monkeypatch, payload):
    from colab_cli.job.runtime_payload import runner

    class Response:
        def __init__(self):
            self.offset = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, size):
            chunk = payload[self.offset : self.offset + size]
            self.offset += len(chunk)
            return chunk

    monkeypatch.setattr(runner, "urlopen_public", lambda *_a, **_k: Response())


@pytest.mark.parametrize(
    "size, digest, kind, message",
    [
        (5, None, "size", "received more than the planned 5 bytes"),
        (7, None, "size", "received 6 bytes, planned 7"),
        (
            None,
            "0" * 64,
            "checksum",
            f"received sha256 {hashlib.sha256(b'actual').hexdigest()}, planned {'0' * 64}",
        ),
    ],
)
def test_a_staged_mismatch_says_what_was_planned_and_received(
    tmp_path, monkeypatch, size, digest, kind, message
):
    from colab_cli.job.runtime_payload import runner

    _serve_bytes(monkeypatch, b"actual")
    with pytest.raises(runner.StagedMismatch) as caught:
        runner._http_get_to_file(
            "https://x.example/o", str(tmp_path / "o"),
            expected_size=size, expected_hash=digest,
        )
    assert caught.value.kind == kind
    assert str(caught.value) == message
    assert not (tmp_path / "o").exists()


def test_error_category_separates_response_network_local_and_setup():
    import socket
    import urllib.error

    from colab_cli.job.runtime_payload import runner
    from colab_cli.job.runtime_payload.netpolicy import (
        BlockedDestination,
        HTTPStatusError,
        UploadCutShort,
    )

    cases = [
        (HTTPStatusError(403, "Forbidden", b""), "http"),
        (urllib.error.HTTPError("https://x", 404, "Not Found", {}, None), "http"),
        (urllib.error.URLError(ConnectionRefusedError(61, "refused")), "network"),
        (socket.gaierror(-2, "Name or service not known"), "network"),
        (TimeoutError("timed out"), "network"),
        (UploadCutShort(BrokenPipeError(32, "Broken pipe"), "then nothing"), "network"),
        (OSError(28, "No space left on device"), "local"),
        (PermissionError(13, "Permission denied"), "local"),
        (BlockedDestination("non-public address for x: 10.0.0.1"), "blocked"),
        (runner.StagedMismatch("checksum", "m"), "checksum"),
        (runner.StagedMismatch("size", "m"), "size"),
        (runner.ManifestError("m"), "setup"),
        (RuntimeError("m"), "error"),
    ]
    for error, category in cases:
        assert runner._error_category(error) == category, error


def test_a_staging_http_error_keeps_its_body_without_the_signed_query(
    tmp_path, monkeypatch
):
    import io
    import urllib.error

    from colab_cli.job.runtime_payload import runner

    url = "https://storage.example/bucket/obj?X-Goog-Signature=SECRET"

    def reject(*_a, **_k):
        body = io.BytesIO(
            b"<Error><Code>ExpiredToken</Code><Details>" + url.encode() + b"</Details></Error>"
            + b"x" * 1000
        )
        raise urllib.error.HTTPError(url, 400, "Bad Request", {}, body)

    monkeypatch.setattr(runner, "urlopen_public", reject)
    item = {"url_ref": hashlib.sha256(url.encode()).hexdigest(),
            "url_id": runner._url_id(url), "dest": "inputs/obj"}

    with pytest.raises(runner.StageItemError) as caught:
        runner._stage_one(str(tmp_path), item, {item["url_ref"]: url})

    record = caught.value.record
    assert record["dest"] == "inputs/obj"
    assert record["status"] == "failed"
    assert record["url_id"] == runner._url_id(url)
    assert record["error"]["http_status"] == 400
    assert record["error"]["category"] == "http"
    assert "ExpiredToken" in record["error"]["body"]
    assert len(record["error"]["body"].encode()) <= runner.ERROR_BODY_BYTES
    text = json.dumps(record) + str(caught.value)
    assert "SECRET" not in text
    assert "X-Goog-Signature" not in text


def test_staging_records_each_input_it_consumed(tmp_path, monkeypatch, capsys):
    from colab_cli.job.runtime_payload import runner

    _serve_bytes(monkeypatch, b"actual")
    records = []
    manifest = tmp_path / "stage.json"
    manifest.write_text(json.dumps([{"url": "https://x.example/a", "dest": "a.bin"}]))

    runner._stage(str(tmp_path), str(manifest), {}, records)

    digest = hashlib.sha256(b"actual").hexdigest()
    assert records == [
        {"dest": "a.bin", "url_id": runner._url_id("https://x.example/a"),
         "status": "ok", "sha256": digest, "bytes": 6}
    ]
    assert f"[runner] staged dest=a.bin bytes=6 sha256={digest}" in capsys.readouterr().out


def test_an_unusable_stage_item_is_a_setup_error_with_its_dest(tmp_path):
    from colab_cli.job.runtime_payload import runner

    records = []
    manifest = tmp_path / "stage.json"
    manifest.write_text(json.dumps([{"url_ref": "0" * 64, "url_id": "u", "dest": "a.bin"}]))

    with pytest.raises(runner.StageItemError):
        runner._stage(str(tmp_path), str(manifest), {}, records)

    [record] = records
    assert record["dest"] == "a.bin"
    assert record["error"]["category"] == "setup"
    assert record["error"]["reason"] == "manifest credential reference is unavailable"


def test_sync_artifacts_once_uploads_a_changed_file(tmp_path, monkeypatch, capsys):
    from colab_cli.job.runtime_payload import runner as runner_module

    monkeypatch.setattr(runner_module.time, "sleep", lambda _s: None)
    ckpt = tmp_path / "checkpoint.pt"
    ckpt.write_bytes(b"v1")
    calls = []
    monkeypatch.setattr(
        runner_module,
        "_http_put_file",
        lambda url, path: calls.append((url, path)) or (2, "digest"),
    )
    manifest = [{"path": "checkpoint.pt", "url": "https://x.example/ckpt"}]
    last_uploaded = {}

    runner_module._sync_artifacts_once(str(tmp_path), manifest, {}, last_uploaded)

    assert len(calls) == 1
    assert calls[0][0] == "https://x.example/ckpt"
    assert "checkpoint.pt" in last_uploaded
    # Free per-revision timing info once runner.log is synced locally --
    # the only place a successful periodic sync is ever observable.
    out = capsys.readouterr().out
    assert "artifact synced" in out
    assert "path=checkpoint.pt" in out
    assert "bytes=2" in out


def test_sync_artifacts_once_dedups_an_unchanged_file(tmp_path, monkeypatch):
    """A checkpoint written every 30s against a 5-minute sync interval
    should not be re-uploaded on every tick if nothing changed since the
    last successful sync."""
    from colab_cli.job.runtime_payload import runner as runner_module

    monkeypatch.setattr(runner_module.time, "sleep", lambda _s: None)
    ckpt = tmp_path / "checkpoint.pt"
    ckpt.write_bytes(b"v1")
    calls = []
    monkeypatch.setattr(
        runner_module,
        "_http_put_file",
        lambda url, path: calls.append((url, path)) or (2, "digest"),
    )
    manifest = [{"path": "checkpoint.pt", "url": "https://x.example/ckpt"}]
    last_uploaded = {}

    runner_module._sync_artifacts_once(str(tmp_path), manifest, {}, last_uploaded)
    runner_module._sync_artifacts_once(str(tmp_path), manifest, {}, last_uploaded)

    assert len(calls) == 1, "unchanged file must not be re-uploaded"


def test_sync_artifacts_once_logs_nothing_for_a_deduped_skip(tmp_path, monkeypatch, capsys):
    """The log line is a per-revision marker, not a heartbeat -- a tick
    that uploaded nothing new must not print anything either, or the log
    stops meaning "this is when the checkpoint actually changed"."""
    from colab_cli.job.runtime_payload import runner as runner_module

    monkeypatch.setattr(runner_module.time, "sleep", lambda _s: None)
    ckpt = tmp_path / "checkpoint.pt"
    ckpt.write_bytes(b"v1")
    monkeypatch.setattr(
        runner_module, "_http_put_file", lambda url, path: (2, "digest")
    )
    manifest = [{"path": "checkpoint.pt", "url": "https://x.example/ckpt"}]
    last_uploaded = {}
    runner_module._sync_artifacts_once(str(tmp_path), manifest, {}, last_uploaded)
    capsys.readouterr()  # discard the first (real) sync's log line

    runner_module._sync_artifacts_once(str(tmp_path), manifest, {}, last_uploaded)

    assert capsys.readouterr().out == ""


def test_sync_artifacts_once_uploads_again_after_a_real_change(tmp_path, monkeypatch):
    from colab_cli.job.runtime_payload import runner as runner_module

    monkeypatch.setattr(runner_module.time, "sleep", lambda _s: None)
    ckpt = tmp_path / "checkpoint.pt"
    ckpt.write_bytes(b"v1")
    calls = []
    monkeypatch.setattr(
        runner_module,
        "_http_put_file",
        lambda url, path: calls.append((url, path)) or (2, "digest"),
    )
    manifest = [{"path": "checkpoint.pt", "url": "https://x.example/ckpt"}]
    last_uploaded = {}

    runner_module._sync_artifacts_once(str(tmp_path), manifest, {}, last_uploaded)
    ckpt.write_bytes(b"v2-longer-content")
    os.utime(ckpt, (time.time() + 5, time.time() + 5))
    runner_module._sync_artifacts_once(str(tmp_path), manifest, {}, last_uploaded)

    assert len(calls) == 2


def test_sync_artifacts_once_skips_a_file_still_being_written(tmp_path, monkeypatch):
    """torch.save does not write atomically by default -- a snapshot taken
    mid-write would upload a torn file. If size/mtime change across the
    stability window, skip this tick and try again next interval."""
    from colab_cli.job.runtime_payload import runner as runner_module

    ckpt = tmp_path / "checkpoint.pt"
    ckpt.write_bytes(b"v1")

    def unstable_sleep(_seconds):
        # Simulate the file still being written during the stability
        # window: it grows between the two stat() calls.
        ckpt.write_bytes(b"v1-still-writing-more-bytes")

    monkeypatch.setattr(runner_module.time, "sleep", unstable_sleep)
    calls = []
    monkeypatch.setattr(
        runner_module,
        "_http_put_file",
        lambda url, path: calls.append((url, path)) or (2, "digest"),
    )
    manifest = [{"path": "checkpoint.pt", "url": "https://x.example/ckpt"}]

    runner_module._sync_artifacts_once(str(tmp_path), manifest, {}, {})

    assert calls == [], "a file that changed mid-check must not be uploaded"


def test_sync_artifacts_once_skips_a_missing_file(tmp_path, monkeypatch):
    from colab_cli.job.runtime_payload import runner as runner_module

    monkeypatch.setattr(runner_module.time, "sleep", lambda _s: None)
    calls = []
    monkeypatch.setattr(
        runner_module,
        "_http_put_file",
        lambda url, path: calls.append((url, path)) or (2, "digest"),
    )
    manifest = [{"path": "never-written.pt", "url": "https://x.example/ckpt"}]

    runner_module._sync_artifacts_once(str(tmp_path), manifest, {}, {})

    assert calls == []


def test_sync_artifacts_once_tolerates_one_bad_item_and_continues(tmp_path, monkeypatch):
    from colab_cli.job.runtime_payload import runner as runner_module

    monkeypatch.setattr(runner_module.time, "sleep", lambda _s: None)
    (tmp_path / "a.pt").write_bytes(b"a")
    (tmp_path / "b.pt").write_bytes(b"b")
    calls = []

    def fake_put(url, path):
        if "a.pt" in path:
            raise RuntimeError("boom")
        calls.append((url, path))
        return (1, "digest")

    monkeypatch.setattr(runner_module, "_http_put_file", fake_put)
    manifest = [
        {"path": "a.pt", "url": "https://x.example/a"},
        {"path": "b.pt", "url": "https://x.example/b"},
    ]

    runner_module._sync_artifacts_once(str(tmp_path), manifest, {}, {})

    assert len(calls) == 1
    assert calls[0][0] == "https://x.example/b"

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

# --------------------------------------------------------------------------
# Artifact upload failures carry their cause
# --------------------------------------------------------------------------


def _reject_without_reading_body(status_line: bytes, body_for):
    """Serve one response per connection as soon as the request headers
    arrive, then close without reading the request body -- what Cloudflare
    does with a body over its upload limit. `body_for(request_head)` builds
    the response body."""
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(5)

    def serve():
        while True:
            try:
                conn, _ = sock.accept()
            except OSError:
                return
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                head += chunk
            body = body_for(head)
            conn.sendall(
                status_line
                + b"\r\nContent-Type: text/html\r\nConnection: close\r\n"
                + b"Content-Length: %d\r\n\r\n" % len(body)
                + body
            )
            conn.close()

    threading.Thread(target=serve, daemon=True).start()
    return sock


def _loopback_put(monkeypatch):
    """Production policy refuses loopback; these tests prove what the
    runner records, not the address policy."""
    import http.client
    from urllib.parse import urlsplit

    from colab_cli.job.runtime_payload import netpolicy

    def connection(url, timeout):
        parts = urlsplit(url)
        return (
            http.client.HTTPConnection(parts.hostname, parts.port, timeout=timeout),
            parts.hostname,
        )

    monkeypatch.setattr(netpolicy, "_public_connection", connection)


def test_artifact_record_keeps_a_413_sent_before_the_body_was_read(
    tmp_path, monkeypatch
):
    """A server that rejects an upload from its headers and closes makes
    the client's send fail with a broken pipe. The record must still carry
    the server's status and body, not only the broken pipe."""
    from colab_cli.job.runtime_payload import runner

    _loopback_put(monkeypatch)
    body = b"<html><title>413 Request Entity Too Large</title>cloudflare</html>"
    sock = _reject_without_reading_body(
        b"HTTP/1.1 413 Payload Too Large", lambda _head: body
    )
    (tmp_path / "adapter.tar").write_bytes(os.urandom(4 * 1024 * 1024))
    url = f"http://127.0.0.1:{sock.getsockname()[1]}/drop/adapter.tar"
    try:
        record = runner._artifact_record(
            str(tmp_path), {"path": "adapter.tar", "url": url}, {}
        )
    finally:
        sock.close()

    assert record["status"] == "failed"
    assert record["bytes"] == 4 * 1024 * 1024
    error = record["error"]
    assert error["exception"] == "HTTPStatusError"
    assert error["http_status"] == 413
    assert "413" in error["reason"]
    assert "upload cut short" in error["reason"]
    assert "Errno" in error["reason"]
    assert error["body"] == body.decode()


def test_artifact_record_keeps_at_most_300_bytes_of_the_response_body(
    tmp_path, monkeypatch
):
    from colab_cli.job.runtime_payload import runner

    _loopback_put(monkeypatch)
    sock = _reject_without_reading_body(
        b"HTTP/1.1 403 Forbidden", lambda _head: b"x" * 5000
    )
    (tmp_path / "out.bin").write_bytes(b"small")
    url = f"http://127.0.0.1:{sock.getsockname()[1]}/out.bin"
    try:
        record = runner._artifact_record(
            str(tmp_path), {"path": "out.bin", "url": url}, {}
        )
    finally:
        sock.close()

    assert record["error"]["http_status"] == 403
    assert record["error"]["body"] == "x" * 300


def test_artifact_failure_detail_never_contains_the_signed_query(
    tmp_path, monkeypatch
):
    """A server may echo the request target in its error body. The query
    string carries the signature and must not reach the record."""
    from colab_cli.job.runtime_payload import runner

    _loopback_put(monkeypatch)
    sentinel = "ARTIFACT_ERROR_SENTINEL"
    sock = _reject_without_reading_body(
        b"HTTP/1.1 403 Forbidden", lambda head: head.split(b"\r\n", 1)[0]
    )
    (tmp_path / "out.bin").write_bytes(b"small")
    url = f"http://127.0.0.1:{sock.getsockname()[1]}/out.bin?sig={sentinel}"
    reference = hashlib.sha256(url.encode()).hexdigest()
    item = {"path": "out.bin", "url_ref": reference, "url_id": runner._url_id(url)}
    try:
        record = runner._artifact_record(str(tmp_path), item, {reference: url})
    finally:
        sock.close()

    assert record["error"]["http_status"] == 403
    assert "PUT /out.bin" in record["error"]["body"]
    assert sentinel not in json.dumps(record)


def test_artifact_record_keeps_a_transport_error_without_a_response(
    tmp_path, monkeypatch
):
    from colab_cli.job.runtime_payload import runner

    def reset(_url, _path):
        raise ConnectionResetError(104, "Connection reset by peer")

    monkeypatch.setattr(runner, "_http_put_file", reset)
    (tmp_path / "out.bin").write_bytes(b"small")

    record = runner._artifact_record(
        str(tmp_path), {"path": "out.bin", "url": "https://x.example/out.bin"}, {}
    )

    assert record["status"] == "failed"
    assert record["error"] == {
        "exception": "ConnectionResetError",
        "reason": "[Errno 104] Connection reset by peer",
        "http_status": None,
        "body": None,
        "category": "network",
    }


def test_offload_records_why_an_artifact_url_could_not_be_resolved(tmp_path):
    from colab_cli.job.runtime_payload import runner

    manifest = tmp_path / "offload.json"
    manifest.write_text(
        json.dumps(
            [
                {
                    "path": "out.bin",
                    "url_ref": "0" * 64,
                    "url_id": "https://x.example/out.bin#000000000000",
                }
            ]
        )
    )

    records, failed, error = runner._offload(str(tmp_path), str(manifest), {})

    assert failed is True
    assert error is None
    assert records[0]["status"] == "failed"
    assert records[0]["error"]["exception"] == "ManifestError"
    assert records[0]["error"]["category"] == "setup"
    assert records[0]["error"]["reason"] == (
        "manifest credential reference is unavailable"
    )


def test_offload_logs_one_line_per_failed_artifact(tmp_path, monkeypatch, capsys):
    from colab_cli.job.runtime_payload import runner
    from colab_cli.job.runtime_payload.netpolicy import HTTPStatusError

    def reject(_url, _path):
        raise HTTPStatusError(413, "Payload Too Large", b"too big")

    monkeypatch.setattr(runner, "_http_put_file", reject)
    for name in ("a.bin", "b.bin"):
        (tmp_path / name).write_bytes(b"x")
    manifest = tmp_path / "offload.json"
    manifest.write_text(
        json.dumps(
            [
                {"path": "a.bin", "url": "https://x.example/a.bin"},
                {"path": "b.bin", "url": "https://x.example/b.bin"},
            ]
        )
    )

    runner._offload(str(tmp_path), str(manifest), {})

    lines = [
        line
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("[runner] artifact upload failed")
    ]
    assert len(lines) == 2
    assert "path=a.bin" in lines[0]
    assert "http_status=413" in lines[0]
    assert "exception=HTTPStatusError" in lines[0]
    assert "path=b.bin" in lines[1]


def test_offload_logs_an_unreadable_manifest(tmp_path, capsys):
    from colab_cli.job.runtime_payload import runner

    manifest = tmp_path / "offload.json"
    manifest.write_text("{not json")

    records, failed, error = runner._offload(str(tmp_path), str(manifest), {})

    assert (records, failed) == ([], True)
    assert error.startswith("offload manifest unreadable: ManifestError: manifest ")
    assert "JSONDecodeError" in error
    assert "[runner] offload manifest unreadable" in capsys.readouterr().out


def test_consumer_runs_with_unbuffered_output(tmp_path):
    """runner.log is the consumer's stdout. Buffered output is lost when the
    consumer is killed, and lags behind every mid-run pull."""
    proc, _result, _job_dir = _run(
        tmp_path, "import os\nprint('UNBUFFERED=' + os.environ.get('PYTHONUNBUFFERED', ''))\n"
    )
    assert "UNBUFFERED=1" in proc.stdout


def test_artifact_put_sends_the_headers_urllib_sent(tmp_path, monkeypatch):
    """A CDN in front of the destination can challenge a request that has
    no User-Agent."""
    import urllib.request

    from colab_cli.job.runtime_payload import runner

    _loopback_put(monkeypatch)
    heads = []
    sock = _reject_without_reading_body(
        b"HTTP/1.1 200 OK", lambda head: heads.append(head) or b""
    )
    (tmp_path / "out.bin").write_bytes(b"abc")
    url = f"http://127.0.0.1:{sock.getsockname()[1]}/out.bin"
    try:
        runner._http_put_file(url, str(tmp_path / "out.bin"))
    finally:
        sock.close()

    lines = heads[0].decode().split("\r\n")
    assert f"User-Agent: Python-urllib/{urllib.request.__version__}" in lines
    assert "Accept-Encoding: identity" in lines
    assert "Content-Type: application/octet-stream" in lines
    assert "Content-Length: 3" in lines



def test_artifact_record_keeps_why_the_response_could_not_be_read(
    tmp_path, monkeypatch
):
    """The send fails and no response arrives: both failures belong in the
    record, not only the broken pipe."""
    import socket

    from colab_cli.job.runtime_payload import runner

    _loopback_put(monkeypatch)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(5)

    def close_after_headers():
        conn, _ = sock.accept()
        head = b""
        while b"\r\n\r\n" not in head:
            head += conn.recv(4096)
        conn.close()

    threading.Thread(target=close_after_headers, daemon=True).start()
    (tmp_path / "out.bin").write_bytes(os.urandom(4 * 1024 * 1024))
    url = f"http://127.0.0.1:{sock.getsockname()[1]}/out.bin"
    try:
        record = runner._artifact_record(
            str(tmp_path), {"path": "out.bin", "url": url}, {}
        )
    finally:
        sock.close()

    error = record["error"]
    assert error["http_status"] is None
    assert error["exception"] == "UploadCutShort"
    assert error["reason"].split(":")[0] in {"BrokenPipeError", "ConnectionResetError"}
    assert "then reading the response failed" in error["reason"]


def test_redact_credentials_removes_every_query_string():
    from colab_cli.job.runtime_payload.redact import redact_credentials

    text = (
        "HTTPSConnectionPool(host='x', port=443): Max retries exceeded with url: "
        "/api/contents/content/jobs/j/runner.log?colab-runtime-proxy-token=SECRET1 "
        "and https://storage.example/o?X-Goog-Signature=SECRET2&x=1, no query: https://a/b"
    )
    redacted = redact_credentials(text)
    assert "SECRET1" not in redacted
    assert "SECRET2" not in redacted
    assert "/api/contents/content/jobs/j/runner.log?<redacted>" in redacted
    assert "https://storage.example/o?<redacted>" in redacted
    assert "no query: https://a/b" in redacted


def test_transfer_error_redacts_queries_of_urls_it_was_not_given():
    from colab_cli.job.runtime_payload import runner

    error = runner._transfer_error(
        ValueError("redirected to https://other.example/x?sig=SECRET"), None
    )
    assert "SECRET" not in error["reason"]
    assert "https://other.example/x?<redacted>" in error["reason"]



def test_runner_argument_error_names_the_option_and_value(tmp_path):
    _package, entry, job_dir = _prepare(tmp_path, "print('never runs')\n")
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "mighty_runtime.runner",
            "--job-dir",
            str(job_dir),
            "--deadline",
            "soon",
            str(entry),
        ],
        cwd=tmp_path,
        env=_runtime_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 2
    assert "--deadline" in proc.stderr
    assert "'soon'" in proc.stderr


def test_proc_stat_parsing_treats_a_zombie_as_gone():
    """A killed process its parent has not reaped keeps its /proc entry,
    with state Z and its original start time, until it is reaped. The
    runner's parent is the launch kernel, which never reaps it."""
    from colab_cli.job.runtime_payload import ident

    # Fields after comm: state, then 18 fields, then starttime (field 22).
    running = "3962 (python3) S 1 3962 3962 0 -1 4194560 " + " ".join(["0"] * 12) + " 98988 0 0"
    zombie = running.replace(") S ", ") Z ", 1)
    odd_comm = running.replace("(python3)", "(py) (x)")

    assert ident._parse_stat(running) == ("S", "98988")
    assert ident._parse_stat(zombie) == ("Z", "98988")
    assert ident._parse_stat(odd_comm) == ("S", "98988")
    assert ident._is_gone("Z") and ident._is_gone("X")
    assert not ident._is_gone("S") and not ident._is_gone("R")


@pytest.mark.skipif(
    not (sys.platform.startswith("linux") and Path("/proc").is_dir()),
    reason="needs Linux /proc",
)
def test_an_unreaped_killed_process_is_not_alive():
    from colab_cli.job.runtime_payload import ident

    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(600)"],
        start_new_session=True,
    )
    try:
        time.sleep(0.3)
        started, boot = ident.starttime(child.pid), ident.boot_id()
        assert ident.alive(child.pid, started, boot)
        os.kill(child.pid, signal.SIGKILL)
        time.sleep(0.3)
        # Not reaped yet: the process is a zombie.
        assert not ident.alive(child.pid, started, boot)
        assert child.pid not in ident.descendants(child.pid)
    finally:
        child.kill()
        child.wait()



def test_redact_credentials_removes_url_userinfo():
    """Package index URLs routinely carry a token as userinfo."""
    from colab_cli.job.runtime_payload.redact import redact_credentials

    text = (
        "index https://ci-bot:SECRET1@pkgs.example/simple/ and "
        "https://user:SECRET2@pkgs.example/simple/x?token=SECRET3 and "
        "https://SECRET4@pkgs.example/simple/ and a plain https://pypi.org/simple/"
    )
    redacted = redact_credentials(text)
    for secret in ("SECRET1", "SECRET2", "SECRET3", "SECRET4"):
        assert secret not in redacted
    assert "https://***@pkgs.example/simple/ and" in redacted
    assert "https://***@pkgs.example/simple/x?<redacted>" in redacted
    assert "https://pypi.org/simple/" in redacted


def test_watchdog_never_signals_the_runner(tmp_path, monkeypatch):
    """The runner carries MIGHTY_JOB_ID like the workload, but it is the
    process that stops the workload, uploads artifacts and writes
    result.json after a cancel or the deadline. Seen live: a cancelled job
    lost its result when the watchdog's escapee sweep killed the runner."""
    from colab_cli.job.runtime_payload import watchdog

    job_dir = tmp_path / "job"
    job_dir.mkdir()
    (job_dir / "cancel.json").write_text('{"intent":"cancelled"}')
    runner_pid = 424242
    excluded = []
    ticks = 0

    monkeypatch.setattr(
        watchdog, "_runner_identity",
        lambda _d: (runner_pid, "", "", time.time() - 1, time.time(), None),
    )
    monkeypatch.setattr(watchdog.ident, "alive", lambda *_a: True)
    monkeypatch.setattr(
        watchdog.ident, "signal_tagged", lambda _job, _sig, exclude=(): excluded.append(set(exclude)) or []
    )
    monkeypatch.setattr(watchdog, "_safe_killpg", lambda pgid, sig: True)

    def record(*_args):
        nonlocal ticks
        ticks += 1
        if ticks == 3:
            (job_dir / "result.json").write_text("{}")

    monkeypatch.setattr(watchdog, "_record", record)
    clock = iter(range(0, 1000, 10))
    monkeypatch.setattr(watchdog.time, "time", lambda: float(next(clock)))
    monkeypatch.setattr(watchdog.time, "sleep", lambda _s: None)

    watchdog.main(["--job-dir", str(job_dir), "--shim-pgid", "4321", "--interval", "0.01"])

    assert excluded, "the escapee sweep should have run"
    assert all(runner_pid in ex for ex in excluded)


def test_watchdog_deadline_kill_never_signals_the_runner(tmp_path, monkeypatch):
    """At wall_clock the watchdog terminates the workload, then escalates;
    the runner must survive both to offload and write result.json."""
    from colab_cli.job.runtime_payload import watchdog

    job_dir = tmp_path / "job"
    job_dir.mkdir()
    runner_pid = 424242
    excluded = []
    ticks = 0

    monkeypatch.setattr(
        watchdog, "_runner_identity", lambda _d: (runner_pid, "", "", 5.0, 0.0, None)
    )
    monkeypatch.setattr(watchdog.ident, "alive", lambda *_a: True)
    monkeypatch.setattr(
        watchdog.ident, "signal_tagged", lambda _job, _sig, exclude=(): excluded.append(set(exclude)) or []
    )
    monkeypatch.setattr(watchdog, "_safe_killpg", lambda pgid, sig: True)

    def record(*_args):
        nonlocal ticks
        ticks += 1
        if ticks == 4:
            (job_dir / "result.json").write_text("{}")

    monkeypatch.setattr(watchdog, "_record", record)
    clock = iter(range(0, 1000, 10))
    monkeypatch.setattr(watchdog.time, "time", lambda: float(next(clock)))
    monkeypatch.setattr(watchdog.time, "sleep", lambda _s: None)

    watchdog.main(["--job-dir", str(job_dir), "--shim-pgid", "4321", "--interval", "0.01"])

    assert len(excluded) >= 2, "both the deadline SIGTERM and the SIGKILL escalation sweep"
    assert all(runner_pid in ex for ex in excluded)


# --------------------------------------------------------------------------
# Failure detail in the result, runner.log and watchdog.json
# --------------------------------------------------------------------------


def test_sigkill_names_the_signal_on_the_vm(tmp_path):
    _proc, result, _job_dir = _run(
        tmp_path, "import os, signal; os.kill(os.getpid(), signal.SIGKILL)\n"
    )
    assert result["signal_name"] == "SIGKILL"
    # An int where /proc/vmstat exists (Linux), None elsewhere.
    assert result["oom_kills"] in (None, 0)
    assert result["oom_log"] == []


def test_the_runner_deadline_writes_a_wall_clock_cancel_intent(tmp_path):
    _proc, result, job_dir = _run(
        tmp_path, "import time; time.sleep(60)\n", "--deadline", "1"
    )
    assert result["workload"] == "cancelled"
    assert result["cancel_intent"]["intent"] == "cancelled"
    assert result["cancel_intent"]["by"] == "wall_clock"
    assert result["signal_name"] == "SIGTERM"


def test_an_unreadable_cancel_record_still_cancels_and_is_a_warning(tmp_path):
    _package, entry, job_dir = _prepare(tmp_path, "import time; time.sleep(60)\n")
    runner = subprocess.Popen(
        [sys.executable, "-m", "mighty_runtime.runner", "--job-dir", str(job_dir), str(entry)],
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
        (job_dir / "cancel.json").write_text("{")
        runner.wait(timeout=20)
    finally:
        if runner.poll() is None:
            runner.kill()
    result = json.loads((job_dir / "result.json").read_text())
    assert result["workload"] == "cancelled"
    assert result["cancel_intent"]["by"] is None
    assert result["cancel_intent"]["error"].startswith("JSONDecodeError")
    assert any(w.startswith("cancel.json unreadable") for w in result["runner_warnings"])


def test_a_failing_periodic_sync_is_logged_and_counted(tmp_path, monkeypatch, capsys):
    from colab_cli.job.runtime_payload import runner as runner_module
    from colab_cli.job.runtime_payload.netpolicy import HTTPStatusError

    monkeypatch.setattr(runner_module.time, "sleep", lambda _s: None)
    (tmp_path / "a.pt").write_bytes(b"a")
    url = "https://x.example/a?X-Goog-Signature=SECRET"

    def reject(_url, _path):
        raise HTTPStatusError(403, "Forbidden", f"denied {url}".encode())

    monkeypatch.setattr(runner_module, "_http_put_file", reject)
    failures = {}
    manifest = [{"path": "a.pt", "url_ref": hashlib.sha256(url.encode()).hexdigest(),
                 "url_id": runner_module._url_id(url)}]
    urls = {manifest[0]["url_ref"]: url}

    runner_module._sync_artifacts_once(str(tmp_path), manifest, urls, {}, failures)
    runner_module._sync_artifacts_once(str(tmp_path), manifest, urls, {}, failures)

    assert failures == {"a.pt": (2, "HTTP 403 Forbidden")}
    out = capsys.readouterr().out
    assert out.count("[runner] artifact sync failed path=a.pt http_status=403") == 2
    assert "SECRET" not in out


def test_a_failed_result_put_logs_status_and_reason_without_the_query(
    tmp_path, monkeypatch, capsys
):
    from colab_cli.job.runtime_payload import runner as runner_module
    from colab_cli.job.runtime_payload.netpolicy import HTTPStatusError

    url = "https://x.example/result.json?X-Goog-Signature=SECRET"

    def reject(_url, _path):
        raise HTTPStatusError(403, "Forbidden", f"<Error>{url}</Error>".encode())

    monkeypatch.setattr(runner_module, "_http_put_file", reject)
    runner_module._put_result(str(tmp_path / "result.json"), url)

    out = capsys.readouterr().out
    assert "[runner] control.result PUT failed http_status=403" in out
    assert "reason=HTTP 403 Forbidden" in out
    assert "SECRET" not in out


def test_a_setup_error_keeps_its_text_and_traceback_without_signed_queries(tmp_path):
    from colab_cli.job.runtime_payload import runner as runner_module

    result_path = tmp_path / "result.json"
    try:
        raise RuntimeError("fetch of https://x.example/o?sig=SECRET went wrong")
    except RuntimeError as error:
        runner_module._stage_failure(
            result_path=str(result_path), job_dir=str(tmp_path), result_put_url=None,
            cli_version="t", started=0.0, attempt=1, error=error, inputs=[],
        )
    result = json.loads(result_path.read_text())
    assert result["exception"]["message"].startswith(
        "RuntimeError: fetch of https://x.example/o"
    )
    assert "RuntimeError" in result["exception"]["traceback"]
    assert "SECRET" not in result_path.read_text()


def test_the_shim_keeps_where_sys_exit_was_called(tmp_path):
    _proc, _result, job_dir = _run(tmp_path, "import sys\n\ndef stop():\n    sys.exit(3)\n\nstop()\n")
    record = json.loads((job_dir / "exception.json").read_text())
    assert record["type"] == "SystemExit"
    assert record["message"] == "3"
    assert "in stop" in record["traceback"]


def test_the_shim_keeps_the_head_of_a_long_chained_traceback(tmp_path):
    source = (
        "try:\n"
        "    raise KeyError('original cause ' + 'x' * 7000)\n"
        "except KeyError as e:\n"
        "    raise RuntimeError('surfaced here') from e\n"
    )
    proc, _result, job_dir = _run(tmp_path, source)
    record = json.loads((job_dir / "exception.json").read_text())
    assert record["type"] == "RuntimeError"
    assert "characters omitted; the full traceback is in runner.log" in record["traceback"]
    assert "KeyError: 'original cause x" in record["traceback"][:2000]
    assert "RuntimeError: surfaced here" in record["traceback"][-500:]
    assert "The above exception was the direct cause" in proc.stderr


def test_the_shim_qualifies_a_non_builtin_exception_type(tmp_path):
    source = "import json\njson.loads('{')\n"
    _proc, _result, job_dir = _run(tmp_path, source)
    assert json.loads((job_dir / "exception.json").read_text())["type"] == (
        "json.decoder.JSONDecodeError"
    )


def test_watchdog_gpu_query_says_why_there_is_no_reading(monkeypatch):
    from colab_cli.job.runtime_payload import watchdog

    def absent(*_a, **_k):
        raise FileNotFoundError("nvidia-smi")

    monkeypatch.setattr(watchdog.subprocess, "run", absent)
    assert watchdog._gpu_query() == (None, "nvidia-smi not found")

    monkeypatch.setattr(
        watchdog.subprocess,
        "run",
        lambda *_a, **_k: subprocess.CompletedProcess(
            [], 9, stdout="", stderr="Unable to determine the device handle for GPU0: Unknown Error"
        ),
    )
    assert watchdog._gpu_query() == (
        None,
        "nvidia-smi exited 9: Unable to determine the device handle for GPU0: Unknown Error",
    )


def test_watchdog_reports_unknown_liveness_for_an_unreadable_launch_record(tmp_path, monkeypatch):
    """An unreadable launch.json is not a dead runner; the supervisor ends
    the job only on runner_alive false."""
    from colab_cli.job.runtime_payload import watchdog

    job_dir = tmp_path / "job"
    job_dir.mkdir()
    (job_dir / "launch.json").write_text("{")
    monkeypatch.setattr(watchdog, "_gpu_query", lambda: (None, "nvidia-smi not found"))

    def stop(*_a):
        (job_dir / "result.json").write_text("{}")

    monkeypatch.setattr(watchdog.time, "sleep", stop)
    watchdog.main(["--job-dir", str(job_dir), "--shim-pgid", "4321", "--interval", "0.01"])
    watchdog.main(["--job-dir", str(job_dir), "--shim-pgid", "4321", "--interval", "0.01"])

    record = json.loads((job_dir / "watchdog.json").read_text())
    assert record["runner_alive"] is None
    assert record["runner_identity_error"].startswith("launch.json unreadable: JSONDecodeError")
    assert record["disk_path"] == str(job_dir)
    assert record["gpu_error"] == "nvidia-smi not found"


def test_watchdog_still_kills_at_the_deadline_when_its_record_cannot_be_written(
    tmp_path, monkeypatch, capsys
):
    from colab_cli.job.runtime_payload import watchdog

    job_dir = tmp_path / "job"
    job_dir.mkdir()
    killed = []
    ticks = 0
    monkeypatch.setattr(
        watchdog, "_runner_identity", lambda _d: (424242, "", "", 5.0, 0.0, None)
    )
    monkeypatch.setattr(watchdog.ident, "alive", lambda *_a: True)
    monkeypatch.setattr(watchdog.ident, "signal_tagged", lambda *_a, **_k: [])
    monkeypatch.setattr(watchdog, "_safe_killpg", lambda pgid, sig: killed.append(sig))

    def full_disk(*_a):
        nonlocal ticks
        ticks += 1
        if ticks == 3:
            (job_dir / "result.json").write_text("{}")
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(watchdog, "_record", full_disk)
    clock = iter(range(0, 1000, 10))
    monkeypatch.setattr(watchdog.time, "time", lambda: float(next(clock)))
    monkeypatch.setattr(watchdog.time, "sleep", lambda _s: None)

    watchdog.main(["--job-dir", str(job_dir), "--shim-pgid", "4321", "--interval", "0.01"])

    assert signal.SIGTERM in killed
    assert "[watchdog] watchdog.json not written: OSError" in capsys.readouterr().err


def test_a_log_line_that_cannot_be_written_does_not_stop_the_runner(monkeypatch):
    import builtins

    from colab_cli.job.runtime_payload import runner as runner_module

    def full_disk(*_a, **_k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(builtins, "print", full_disk)
    runner_module._log("verdict line")


def test_oom_log_keeps_only_the_kernels_kill_lines_for_this_run(monkeypatch):
    from colab_cli.job.runtime_payload import runner as runner_module

    dmesg = "\n".join(
        [
            "[ 10.0] eth0: link up",
            "[ 50.0] Out of memory: Killed process 11 (old) total-vm:1kB",
            "[ 90.0] Memory cgroup out of memory: Killed process 22 (python3) total-vm:2kB",
            "[ 95.0] oom_reaper: reaped process 22 (python3)",
        ]
    )
    seen = {}

    def run(cmd, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, stdout=dmesg, stderr="")

    monkeypatch.setattr(runner_module.subprocess, "run", run)

    assert runner_module._oom_log(1) == [
        "[ 90.0] Memory cgroup out of memory: Killed process 22 (python3) total-vm:2kB"
    ]
    assert runner_module._oom_log(0) == []
    # Kernel log bytes that are not UTF-8 must not raise in the verdict path.
    assert seen["errors"] == "replace"
