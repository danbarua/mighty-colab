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

"""Hermetic contract tests for job planning and signed URL handling."""

from __future__ import annotations

import hashlib
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

import pytest

from colab_cli.job.models import (
    Accelerator,
    ArtifactItem,
    CodeSpec,
    Control,
    ControlChannel,
    DataItem,
    JobSpec,
    Retry,
)
from colab_cli.job.planner import (
    ACCELERATOR_UNKNOWN,
    ARTIFACT_SIZE_UNKNOWN,
    CODE_ENTRY_MISSING,
    DATA_DEST_COLLIDES_WITH_ARTIFACT,
    DATA_SIZE_UNKNOWN,
    DESTINATION_OUTSIDE_JOB_DIR,
    DIAGNOSTIC_CODES,
    DUPLICATE_DESTINATION,
    ENTRY_NOT_UNDER_BUNDLE,
    RANGED_GET_FAILED,
    RANGED_GET_IGNORED_RANGE,
    RESERVED_PATH,
    RETRY_NOT_IMPLEMENTED,
    URL_EXPIRY_TOO_SOON,
    URL_HOST_NOT_PUBLIC,
    URL_SCHEME_NOT_HTTPS,
    build_plan,
    revalidate_expiry,
)
from colab_cli.job.spec_io import (
    ProbeResult,
    canonical_url,
    load_spec,
    parse_signed_url_expiry,
    probe_get_url,
    spec_hash,
    url_id,
)

JOB_ID = "planner-test"
PUBLIC_URL = "https://storage.example.test/bucket/input.bin"


def make_spec(tmp_path: Path, *, data: list[DataItem] | None = None, **updates) -> JobSpec:
    entry = tmp_path / "entry.py"
    entry.write_text("print('ok')\n", encoding="utf-8")
    values = {
        "name": "test-job",
        "code": CodeSpec(root=str(tmp_path), entry="entry.py"),
        "data": data or [DataItem(url=PUBLIC_URL, dest=f"/content/jobs/{JOB_ID}/data.bin", size_bytes=3)],
    }
    values.update(updates)
    return JobSpec(**values)


def diagnostic_codes(plan) -> set[str]:
    return {diagnostic.code for diagnostic in plan.diagnostics}


def test_data_sha256_must_have_64_hexadecimal_characters():
    with pytest.raises(ValueError, match="64 hexadecimal"):
        DataItem(url=PUBLIC_URL, dest="/content/x", sha256="a" * 32)
    with pytest.raises(ValueError, match="64 hexadecimal"):
        DataItem(url=PUBLIC_URL, dest="/content/x", sha256="a" * 65)


def test_data_sha256_rejects_non_hexadecimal_characters():
    with pytest.raises(ValueError, match="64 hexadecimal"):
        DataItem(url=PUBLIC_URL, dest="/content/x", sha256="z" * 64)


def test_data_sha256_normalizes_uppercase_hexadecimal():
    item = DataItem(url=PUBLIC_URL, dest="/content/x", sha256="A" * 64)
    assert item.sha256 == "a" * 64


def test_load_spec_yaml_and_resolves_default_root(tmp_path):
    spec_path = tmp_path / "job.yaml"
    spec_path.write_text(
        "name: yaml-job\ncode:\n  entry: entry.py\n",
        encoding="utf-8",
    )
    (tmp_path / "entry.py").write_text("pass\n", encoding="utf-8")

    spec = load_spec(spec_path)

    assert spec.name == "yaml-job"
    assert spec.code.root == str(tmp_path)


def test_load_spec_accepts_json(tmp_path):
    spec_path = tmp_path / "job.json"
    spec_path.write_text('{"name":"json-job","code":{"entry":"main.py"}}', encoding="utf-8")
    (tmp_path / "main.py").write_text("pass\n", encoding="utf-8")

    assert load_spec(spec_path).name == "json-job"


def test_load_spec_rejects_non_mapping(tmp_path):
    path = tmp_path / "invalid.yaml"
    path.write_text("[]\n", encoding="utf-8")

    with pytest.raises(ValueError, match="mapping"):
        load_spec(path)


def test_canonical_url_removes_query_and_fragment():
    assert canonical_url("https://example.test/a/b?X-Goog-Signature=secret#fragment") == (
        "https://example.test/a/b"
    )
    assert canonical_url("https://user:password@example.test/a") == "https://example.test/a"


def test_url_id_has_canonical_identity_and_original_url_digest():
    url = "https://example.test/a?signature=secret"
    expected = f"https://example.test/a#{hashlib.sha256(url.encode()).hexdigest()[:12]}"
    assert url_id(url) == expected


def test_parse_signed_url_expiry_gcs_v4():
    url = "https://storage.example/a?X-Goog-Date=20260911T120000Z&X-Goog-Expires=90"
    assert parse_signed_url_expiry(url) == datetime(2026, 9, 11, 12, 1, 30, tzinfo=timezone.utc)


def test_parse_signed_url_expiry_gcs_v2():
    assert parse_signed_url_expiry("https://storage.example/a?Expires=1799100000") == datetime.fromtimestamp(
        1799100000, tz=timezone.utc
    )


def test_parse_signed_url_expiry_s3_v4():
    url = "https://s3.example/a?X-Amz-Date=20260911T120000Z&X-Amz-Expires=120"
    assert parse_signed_url_expiry(url) == datetime(2026, 9, 11, 12, 2, tzinfo=timezone.utc)


def test_parse_signed_url_expiry_azure_sas():
    url = "https://blob.example/a?se=2026-09-11T12%3A03%3A00Z"
    assert parse_signed_url_expiry(url) == datetime(2026, 9, 11, 12, 3, tzinfo=timezone.utc)


def test_parse_signed_url_expiry_public_url_is_none():
    assert parse_signed_url_expiry(PUBLIC_URL) is None


def test_parse_signed_url_expiry_malformed_is_none():
    assert parse_signed_url_expiry("https://example.test/a?X-Amz-Date=nope&X-Amz-Expires=bad") is None


def test_spec_hash_ignores_resigning_but_detects_object_change(tmp_path):
    first = make_spec(
        tmp_path,
        data=[DataItem(url=f"{PUBLIC_URL}?X-Goog-Signature=one", dest="/content/jobs/planner-test/x", size_bytes=1)],
    )
    resigned = first.model_copy(deep=True)
    resigned.data[0].url = f"{PUBLIC_URL}?X-Goog-Signature=two&X-Goog-Date=20260911T120000Z"
    different = first.model_copy(deep=True)
    different.data[0].url = "https://storage.example.test/bucket/other.bin?signature=one"

    assert spec_hash(first) == spec_hash(resigned)
    assert spec_hash(first) != spec_hash(different)


class StubResponse:
    def __init__(self, status: int, headers: dict[str, str] | None = None, body: bytes = b"x"):
        self.status = status
        self.headers = headers or {}
        self.body = body
        self.read_sizes: list[int | None] = []
        self.closed = False

    def read(self, size=-1):
        self.read_sizes.append(size)
        return self.body[:size]

    def close(self):
        self.closed = True


def test_probe_get_url_206_reads_at_most_one_byte(monkeypatch):
    response = StubResponse(206, {"Content-Range": "bytes 0-0/123"})
    requests = []
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda request, timeout: (requests.append((request, timeout)) or response),
    )

    result = probe_get_url(PUBLIC_URL, timeout=4)

    assert result == ProbeResult(status=206, size_bytes=123, range_honored=True)
    assert response.read_sizes == [1]
    assert response.closed
    assert requests[0][0].get_method() == "GET"
    assert requests[0][0].get_header("Range") == "bytes=0-0"
    assert requests[0][1] == 4


def test_probe_get_url_200_closes_without_reading_body(monkeypatch):
    response = StubResponse(200, body=b"a" * 1024 * 1024)
    monkeypatch.setattr("urllib.request.urlopen", lambda request, timeout: response)

    result = probe_get_url(PUBLIC_URL)

    assert result.status == 200
    assert result.size_bytes is None
    assert result.range_ignored
    assert response.read_sizes == []
    assert response.closed


def test_probe_get_url_416_is_empty_not_error(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", lambda request, timeout: StubResponse(416))

    result = probe_get_url(PUBLIC_URL)

    assert result.status == 416
    assert result.size_bytes == 0
    assert result.empty
    assert result.error is None


@pytest.mark.parametrize("status", [403, 404])
def test_probe_get_url_forbidden_or_missing_is_error(monkeypatch, status):
    def raise_http_error(request, timeout):
        raise urllib.error.HTTPError(PUBLIC_URL, status, "failure", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", raise_http_error)

    result = probe_get_url(PUBLIC_URL)

    assert result.status == status
    assert result.error


def test_build_plan_clean_spec_has_no_error(tmp_path, monkeypatch):
    monkeypatch.setattr("colab_cli.job.planner.probe_get_url", lambda url: ProbeResult(206, 3, None, True))

    plan = build_plan(make_spec(tmp_path), JOB_ID)

    assert not plan.has_errors
    assert RANGED_GET_FAILED not in diagnostic_codes(plan)


def test_unknown_accelerator_diagnostic(tmp_path):
    spec = make_spec(tmp_path, accelerator=Accelerator(prefer=["RTX999"]))
    plan = build_plan(spec, JOB_ID, probe=False)
    assert ACCELERATOR_UNKNOWN in diagnostic_codes(plan)
    assert ACCELERATOR_UNKNOWN not in diagnostic_codes(build_plan(make_spec(tmp_path), JOB_ID, probe=False))
def test_retry_request_is_rejected_until_retry_is_implemented(tmp_path):
    plan = build_plan(
        make_spec(tmp_path, retry=Retry(max_attempts=2)), JOB_ID, probe=False
    )
    assert plan.has_errors
    assert RETRY_NOT_IMPLEMENTED in diagnostic_codes(plan)
    assert diagnostic_codes(plan) <= DIAGNOSTIC_CODES


def test_resume_mode_is_rejected_until_resume_is_implemented(tmp_path):
    plan = build_plan(
        make_spec(tmp_path, retry=Retry(mode="resume")), JOB_ID, probe=False
    )
    assert plan.has_errors
    assert RETRY_NOT_IMPLEMENTED in diagnostic_codes(plan)


def test_run_fail_skip_is_rejected_until_skip_is_implemented(tmp_path):
    plan = build_plan(make_spec(tmp_path, on_run_fail="skip"), JOB_ID, probe=False)
    assert plan.has_errors


def test_control_log_is_rejected_until_log_streaming_is_implemented(tmp_path):
    channel = ControlChannel(put_url=PUBLIC_URL, get_url=PUBLIC_URL)
    plan = build_plan(
        make_spec(tmp_path, control=Control(log=channel)), JOB_ID, probe=False
    )
    assert plan.has_errors


def test_non_https_url_diagnostic(tmp_path):
    spec = make_spec(tmp_path, data=[DataItem(url="file:///tmp/input", dest="/content/jobs/planner-test/x", size_bytes=1)])
    assert URL_SCHEME_NOT_HTTPS in diagnostic_codes(build_plan(spec, JOB_ID, probe=False))
    assert URL_SCHEME_NOT_HTTPS not in diagnostic_codes(build_plan(make_spec(tmp_path), JOB_ID, probe=False))


def test_private_or_link_local_host_diagnostic(tmp_path):
    spec = make_spec(tmp_path, data=[DataItem(url="https://169.254.1.1/x", dest="/content/jobs/planner-test/x", size_bytes=1)])
    assert URL_HOST_NOT_PUBLIC in diagnostic_codes(build_plan(spec, JOB_ID, probe=False))
    assert URL_HOST_NOT_PUBLIC not in diagnostic_codes(build_plan(make_spec(tmp_path), JOB_ID, probe=False))


def test_destination_outside_content_is_an_error(tmp_path):
    """Containment is enforced against `/content`, the VM's writable
    scratch -- not against the job's own directory.

    Forcing every path under `/content/jobs/<id>/` would reject
    `/content/out/model.pt`, which is what ordinary Colab code already
    writes, and break the promise that an unmodified script is a valid
    job. Escaping `/content` is the thing actually worth refusing.
    """
    spec = make_spec(
        tmp_path,
        data=[DataItem(url=PUBLIC_URL, dest="/usr/local/lib/x", size_bytes=1)],
    )
    assert DESTINATION_OUTSIDE_JOB_DIR in diagnostic_codes(
        build_plan(spec, JOB_ID, probe=False)
    )


def test_traversal_out_of_content_is_an_error(tmp_path):
    spec = make_spec(
        tmp_path,
        data=[DataItem(url=PUBLIC_URL, dest="/content/../etc/passwd", size_bytes=1)],
    )
    assert DESTINATION_OUTSIDE_JOB_DIR in diagnostic_codes(
        build_plan(spec, JOB_ID, probe=False)
    )


def test_conventional_content_paths_are_accepted(tmp_path):
    """The regression that motivated the rule change: the shipped example
    spec uses exactly these paths and was rejected outright."""
    spec = make_spec(
        tmp_path,
        data=[DataItem(url=PUBLIC_URL, dest="/content/data/x.npy", size_bytes=1)],
        artifacts=[
            ArtifactItem(path="/content/out/model.pt", url=PUBLIC_URL, size_bytes=1)
        ],
    )
    assert DESTINATION_OUTSIDE_JOB_DIR not in diagnostic_codes(
        build_plan(spec, JOB_ID, probe=False)
    )


def test_reserved_path_diagnostic(tmp_path):
    spec = make_spec(tmp_path, data=[DataItem(url=PUBLIC_URL, dest="/content/jobs/planner-test/mighty_runtime/x", size_bytes=1)])
    assert RESERVED_PATH in diagnostic_codes(build_plan(spec, JOB_ID, probe=False))


def test_duplicate_destination_diagnostic(tmp_path):
    data = [
        DataItem(url=PUBLIC_URL, dest="/content/jobs/planner-test/same", size_bytes=1),
        DataItem(url=PUBLIC_URL, dest="/content/jobs/planner-test/same", size_bytes=1),
    ]
    assert DUPLICATE_DESTINATION in diagnostic_codes(build_plan(make_spec(tmp_path, data=data), JOB_ID, probe=False))


def test_data_destination_artifact_collision_diagnostic(tmp_path):
    data = [DataItem(url=PUBLIC_URL, dest="/content/jobs/planner-test/result", size_bytes=1)]
    artifacts = [ArtifactItem(path="/content/jobs/planner-test/result", url=PUBLIC_URL)]
    assert DATA_DEST_COLLIDES_WITH_ARTIFACT in diagnostic_codes(
        build_plan(make_spec(tmp_path, data=data, artifacts=artifacts), JOB_ID, probe=False)
    )


def test_missing_code_entry_diagnostic(tmp_path):
    spec = make_spec(tmp_path)
    spec.code.entry = "missing.py"
    assert CODE_ENTRY_MISSING in diagnostic_codes(build_plan(spec, JOB_ID, probe=False))


def test_entry_symlink_escape_diagnostic(tmp_path):
    outside = tmp_path.parent / "outside-entry.py"
    outside.write_text("pass\n", encoding="utf-8")
    (tmp_path / "link.py").symlink_to(outside)
    spec = make_spec(tmp_path)
    spec.code.entry = "link.py"

    assert ENTRY_NOT_UNDER_BUNDLE in diagnostic_codes(build_plan(spec, JOB_ID, probe=False))


def test_url_expiry_too_soon_diagnostic(tmp_path):
    soon = int(datetime.now(timezone.utc).timestamp()) + 1
    data = [DataItem(url=f"{PUBLIC_URL}?Expires={soon}", dest="/content/jobs/planner-test/x", size_bytes=1)]

    plan = build_plan(make_spec(tmp_path, data=data), JOB_ID, probe=False)

    assert URL_EXPIRY_TOO_SOON in diagnostic_codes(plan)


def test_revalidate_expiry_reparses_current_spec_urls(tmp_path):
    soon = int(datetime.now(timezone.utc).timestamp()) + 1
    data = [DataItem(url=f"{PUBLIC_URL}?Expires={soon}", dest="/content/jobs/planner-test/x", size_bytes=1)]
    plan = build_plan(make_spec(tmp_path, data=data), JOB_ID, probe=False)

    assert revalidate_expiry(plan)

    # Re-signing in place with a long-lived URL must be evaluated from the
    # current spec, not the stale value persisted in plan.url_expiry.
    plan.spec.data[0].url = f"{PUBLIC_URL}?Expires={int(datetime.now(timezone.utc).timestamp()) + 86400}"
    assert revalidate_expiry(plan) == []


def test_ranged_get_failure_diagnostic(tmp_path, monkeypatch):
    monkeypatch.setattr("colab_cli.job.planner.probe_get_url", lambda url: ProbeResult(403, None, "HTTP 403"))
    plan = build_plan(make_spec(tmp_path), JOB_ID)
    assert RANGED_GET_FAILED in diagnostic_codes(plan)


def test_artifact_size_unknown_warning(tmp_path):
    artifacts = [ArtifactItem(path="/content/jobs/planner-test/out.pt", url=PUBLIC_URL)]
    plan = build_plan(make_spec(tmp_path, artifacts=artifacts), JOB_ID, probe=False)
    assert ARTIFACT_SIZE_UNKNOWN in diagnostic_codes(plan)


def test_ranged_get_ignored_warning(tmp_path, monkeypatch):
    monkeypatch.setattr("colab_cli.job.planner.probe_get_url", lambda url: ProbeResult(200))
    plan = build_plan(make_spec(tmp_path), JOB_ID)
    assert RANGED_GET_IGNORED_RANGE in diagnostic_codes(plan)


def test_data_size_unknown_warning(tmp_path):
    data = [DataItem(url=PUBLIC_URL, dest="/content/jobs/planner-test/x")]
    plan = build_plan(make_spec(tmp_path, data=data), JOB_ID, probe=False)
    assert DATA_SIZE_UNKNOWN in diagnostic_codes(plan)


def test_nondefault_retry_class_is_rejected_until_retry_is_implemented(tmp_path):
    from colab_cli.job.models import RetryClass

    plan = build_plan(
        make_spec(tmp_path, retry=Retry(when=[RetryClass.FIX_CODE])),
        JOB_ID,
        probe=False,
    )
    assert plan.has_errors
    assert RETRY_NOT_IMPLEMENTED in diagnostic_codes(plan)
