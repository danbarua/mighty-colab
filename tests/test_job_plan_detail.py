"""Plan-time failure detail: spec and plan load errors, host checks, the
data probe, measured sizes, URL expiry and the source lock."""

from __future__ import annotations

import io
import json
import socket
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from colab_cli.cli import app
from colab_cli.job.models import (
    Budgets,
    CodeSpec,
    Control,
    ControlChannel,
    DataItem,
    JobSpec,
    Retry,
    RetryClass,
)
from colab_cli.job.planner import (
    DATA_SIZE_MISMATCH,
    DATA_SIZE_PROBED,
    DATA_SIZE_UNKNOWN,
    INSTALL_ALLOWANCE_SECONDS,
    RANGED_GET_FAILED,
    SOURCE_UNREADABLE,
    URL_EXPIRY_TOO_SOON,
    URL_HOST_NOT_PUBLIC,
    URL_HOST_UNRESOLVED,
    build_plan,
    revalidate_expiry,
)
from colab_cli.job.spec_io import ProbeResult, load_spec, probe_get_url
from colab_cli.job.store import load_plan_file, write_plan_file

JOB_ID = "plan-detail"
URL = "https://storage.example.test/bucket/input.bin"


def _spec(tmp_path: Path, **updates) -> JobSpec:
    (tmp_path / "entry.py").write_text("print('ok')\n")
    values = {
        "name": "detail",
        "code": CodeSpec(root=str(tmp_path), entry="entry.py"),
        "data": [DataItem(url=URL, dest="in.bin", size_bytes=3)],
    }
    values.update(updates)
    return JobSpec(**values)


def _by_code(plan, code):
    return [d for d in plan.diagnostics if d.code == code]


def _signed(minutes_from_now: float) -> str:
    expires = int((datetime.now(timezone.utc) + timedelta(minutes=minutes_from_now)).timestamp())
    return f"{URL}?Expires={expires}&Signature=SECRET"


# -- spec loading -----------------------------------------------------------


def test_invalid_yaml_names_the_problem_but_not_the_line(tmp_path):
    path = tmp_path / "job.yaml"
    path.write_text("name: x\ncode: {entry: a.py\ndata: [{url: https://h/o?sig=SECRET}]\n")

    with pytest.raises(ValueError) as caught:
        load_spec(path)

    message = str(caught.value)
    assert message.startswith("invalid YAML at line ")
    assert "expected ',' or '}'" in message or "while parsing" in message
    assert "SECRET" not in message


def test_plan_command_reports_why_the_spec_is_unreadable(tmp_path):
    path = tmp_path / "job.yaml"
    path.write_text("name: x\n  code: bad\n")
    result = CliRunner().invoke(app, ["job", "plan", str(path)])

    assert result.exit_code == 1
    assert "Could not read spec" in result.output
    assert "invalid YAML at line 2" in result.output


# -- plan loading ------------------------------------------------------------


def test_an_invalid_plan_names_the_field_without_its_value(tmp_path):
    plan = build_plan(_spec(tmp_path), JOB_ID, probe=False)
    path = write_plan_file(tmp_path / "plan.json", plan)
    payload = json.loads(path.read_text())
    payload["spec"]["budgets"]["wall_clock"] = "https://h/o?sig=SECRET"
    path.write_text(json.dumps(payload))

    with pytest.raises(ValueError) as caught:
        load_plan_file(path)

    message = str(caught.value)
    assert message.startswith("plan file is invalid: spec.budgets.wall_clock: ")
    assert "SECRET" not in message


def test_a_plan_that_is_not_json_gives_the_position(tmp_path):
    path = tmp_path / "plan.json"
    path.write_text('{"job_id": "x",\n "sig": "SECRET"')

    with pytest.raises(ValueError) as caught:
        load_plan_file(path)

    assert "is not valid JSON" in str(caught.value)
    assert "line 2" in str(caught.value)
    assert "SECRET" not in str(caught.value)


def test_a_missing_plan_file_gives_the_os_error(tmp_path):
    with pytest.raises(ValueError, match="plan file cannot be read: FileNotFoundError"):
        load_plan_file(tmp_path / "absent.json")


# -- host checks -------------------------------------------------------------


def test_an_unresolvable_host_is_its_own_diagnostic(tmp_path, monkeypatch):
    def no_such_host(host, *_a, **_k):
        raise socket.gaierror(-2, "Name or service not known")

    monkeypatch.setattr("colab_cli.job.runtime_payload.netpolicy.socket.getaddrinfo", no_such_host)
    probed = []
    monkeypatch.setattr(
        "colab_cli.job.planner.probe_get_url", lambda url: probed.append(url) or ProbeResult(206, 3)
    )

    plan = build_plan(_spec(tmp_path), JOB_ID)

    [diagnostic] = _by_code(plan, URL_HOST_UNRESOLVED)
    assert diagnostic.severity == "error"
    assert diagnostic.retry_class is RetryClass.FIX_CODE
    assert "storage.example.test: gaierror: [Errno -2] Name or service not known" in diagnostic.message
    assert not _by_code(plan, URL_HOST_NOT_PUBLIC)
    assert probed == []


def test_a_private_host_names_the_addresses(tmp_path):
    spec = _spec(tmp_path, data=[DataItem(url="https://private.test/o", dest="in.bin", size_bytes=1)])

    plan = build_plan(spec, JOB_ID, probe=False)

    [diagnostic] = _by_code(plan, URL_HOST_NOT_PUBLIC)
    assert "non-public address for private.test: 10.0.0.1" in diagnostic.message


def test_each_url_host_is_looked_up_once(tmp_path, monkeypatch):
    from colab_cli.job import planner

    calls = []
    real = planner.resolve_public_addresses
    monkeypatch.setattr(
        planner, "resolve_public_addresses", lambda h, p: calls.append(h) or real(h, p)
    )
    monkeypatch.setattr("colab_cli.job.planner.probe_get_url", lambda url: ProbeResult(206, 3))

    build_plan(_spec(tmp_path), JOB_ID)

    assert calls == ["storage.example.test"]


# -- the data probe ----------------------------------------------------------


def _http_error(status, body=b""):
    def raise_it(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url, status, "Reason", {}, io.BytesIO(body)
        )

    return raise_it


@pytest.mark.parametrize(
    "status, retry",
    [
        (401, RetryClass.REFRESH_URLS),
        (403, RetryClass.REFRESH_URLS),
        (404, RetryClass.FIX_CODE),
        (410, RetryClass.FIX_CODE),
        (408, RetryClass.RETRY_SAME),
        (429, RetryClass.RETRY_SAME),
        (500, RetryClass.RETRY_SAME),
        (503, RetryClass.RETRY_SAME),
    ],
)
def test_every_probe_failure_is_an_error_classified_by_the_transfer_table(
    tmp_path, monkeypatch, status, retry
):
    monkeypatch.setattr("colab_cli.job.spec_io.urlopen_public", _http_error(status))

    plan = build_plan(_spec(tmp_path), JOB_ID)

    [diagnostic] = _by_code(plan, RANGED_GET_FAILED)
    assert diagnostic.severity == "error"
    assert diagnostic.retry_class is retry
    assert f"HTTP Error {status}: Reason" in diagnostic.message


def test_a_real_gcs_403_body_is_kept_without_the_signature(monkeypatch):
    """The body GCS returned on 2026-10-05 for a GOOG4 URL with a wrong
    signature (tests/fixtures/gcs_signature_does_not_match_403.xml). It
    quotes the canonical request, but never the signature."""
    fixture = Path(__file__).parent / "fixtures" / "gcs_signature_does_not_match_403.xml"
    url = (
        "https://storage.googleapis.com/gcp-public-data-landsat/index.csv.gz"
        "?X-Goog-Algorithm=GOOG4-RSA-SHA256&X-Goog-Credential=nobody%40example.iam."
        "gserviceaccount.com%2F20261005%2Fauto%2Fstorage%2Fgoog4_request&X-Goog-Date="
        "20261005T000000Z&X-Goog-Expires=604800&X-Goog-SignedHeaders=host"
        "&X-Goog-Signature=SENTINEL0123"
    )
    monkeypatch.setattr(
        "colab_cli.job.spec_io.urlopen_public", _http_error(403, fixture.read_bytes())
    )

    result = probe_get_url(url)

    assert result.error.body.startswith(
        "<?xml version='1.0' encoding='UTF-8'?><Error><Code>SignatureDoesNotMatch</Code>"
    )
    assert len(result.error.body) <= 300
    assert "SENTINEL0123" not in json.dumps(result.error.model_dump())


def test_a_probe_with_no_response_is_retry_same(tmp_path, monkeypatch):
    def refused(request, timeout):
        raise urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))

    monkeypatch.setattr("colab_cli.job.spec_io.urlopen_public", refused)

    plan = build_plan(_spec(tmp_path), JOB_ID)

    [diagnostic] = _by_code(plan, RANGED_GET_FAILED)
    assert diagnostic.retry_class is RetryClass.RETRY_SAME
    assert "URLError" in diagnostic.message


def test_a_probed_size_that_differs_from_the_declared_one_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr("colab_cli.job.planner.probe_get_url", lambda url: ProbeResult(206, 11358, None, True))

    plan = build_plan(_spec(tmp_path), JOB_ID)

    [diagnostic] = _by_code(plan, DATA_SIZE_MISMATCH)
    assert diagnostic.retry_class is RetryClass.FIX_CODE
    assert "is 11358 bytes at the URL but size_bytes declares 3" in diagnostic.message


def test_an_empty_object_is_a_mismatch_against_a_declared_size(tmp_path, monkeypatch):
    monkeypatch.setattr("colab_cli.job.planner.probe_get_url", lambda url: ProbeResult(416, 0, None, True))

    plan = build_plan(_spec(tmp_path), JOB_ID)

    assert _by_code(plan, DATA_SIZE_MISMATCH)


def test_a_probed_size_stands_in_for_an_undeclared_one(tmp_path, monkeypatch):
    monkeypatch.setattr("colab_cli.job.planner.probe_get_url", lambda url: ProbeResult(206, 11358, None, True))
    spec = _spec(tmp_path, data=[DataItem(url=URL, dest="in.bin")])

    plan = build_plan(spec, JOB_ID)

    assert plan.probed_size_bytes == {"data[0]": 11358}
    assert plan.input_bytes() == 11358
    [info] = _by_code(plan, DATA_SIZE_PROBED)
    assert info.severity == "info"
    assert not _by_code(plan, DATA_SIZE_UNKNOWN)
    assert not plan.has_errors and not plan.has_warnings


def test_size_unknown_names_the_inputs_nothing_measured(tmp_path):
    spec = _spec(tmp_path, data=[DataItem(url=URL, dest="a.bin"), DataItem(url=URL, dest="b.bin", size_bytes=1)])

    plan = build_plan(spec, JOB_ID, probe=False)

    [warning] = _by_code(plan, DATA_SIZE_UNKNOWN)
    assert warning.message.startswith("size unknown for data[0]:")


def test_an_info_only_plan_is_ready_to_apply(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("colab_cli.job.planner.probe_get_url", lambda url: ProbeResult(206, 7, None, True))
    (tmp_path / "entry.py").write_text("print(1)\n")
    path = tmp_path / "job.yaml"
    path.write_text(
        f"name: info\ncode: {{entry: entry.py}}\ndata: [{{url: '{URL}', dest: in.bin}}]\n"
    )

    result = CliRunner().invoke(app, ["job", "plan", str(path)])

    assert result.exit_code == 0, result.output
    assert "INFO  data_size_probed" in result.output
    assert "ready to apply" in result.output
    assert "warning(s)" not in result.output


# -- URL expiry --------------------------------------------------------------


def test_the_expiry_message_says_when_and_what_it_must_cover(tmp_path):
    spec = _spec(tmp_path, budgets=Budgets(wall_clock=3600),
                 data=[DataItem(url=_signed(30), dest="in.bin", size_bytes=3)])

    plan = build_plan(spec, JOB_ID, probe=False)

    [diagnostic] = _by_code(plan, URL_EXPIRY_TOO_SOON)
    assert "expires in 30m" in diagnostic.message or "expires in 29m" in diagnostic.message
    assert "it must stay valid for 1h15m: wall_clock 3600s + 15 min for staging and offload" in diagnostic.message
    assert "SECRET" not in diagnostic.message


def test_deps_add_the_install_allowance(tmp_path):
    minutes = (600 + 900 + INSTALL_ALLOWANCE_SECONDS) / 60 - 10
    data = [DataItem(url=_signed(minutes), dest="in.bin", size_bytes=3)]

    without = build_plan(_spec(tmp_path, budgets=Budgets(wall_clock=600), data=data), JOB_ID, probe=False)
    with_deps = build_plan(
        _spec(tmp_path, budgets=Budgets(wall_clock=600), data=data, deps=["six"]), JOB_ID, probe=False
    )

    assert not _by_code(without, URL_EXPIRY_TOO_SOON)
    [diagnostic] = _by_code(with_deps, URL_EXPIRY_TOO_SOON)
    assert "55m for installing deps" in diagnostic.message


def test_after_install_the_allowance_is_not_counted(tmp_path):
    minutes = (600 + 900) / 60 + 10
    spec = _spec(tmp_path, budgets=Budgets(wall_clock=600), deps=["six"],
                 data=[DataItem(url=_signed(minutes), dest="in.bin", size_bytes=3)])
    plan = build_plan(spec, JOB_ID, probe=False)

    assert revalidate_expiry(plan)
    assert revalidate_expiry(plan, after_install=True) == []


def test_a_control_url_must_last_as_long_as_the_data_deadline(tmp_path):
    put = _signed(30).replace("input.bin", "result.json")
    get = put.replace("Signature=SECRET", "Signature=SECRET2")
    spec = _spec(
        tmp_path,
        budgets=Budgets(wall_clock=3600),
        retry=Retry(budget_seconds=60),
        control=Control(result=ControlChannel(put_url=put, get_url=get)),
    )

    plan = build_plan(spec, JOB_ID, probe=False)

    messages = [d.message for d in _by_code(plan, URL_EXPIRY_TOO_SOON) if d.message.startswith("control.result")]
    assert len(messages) == 2
    assert "longer than retry.budget_seconds + 15 min" in messages[0]


def test_an_unparseable_recorded_expiry_is_logged(tmp_path, caplog):
    spec = _spec(tmp_path)
    plan = build_plan(spec, JOB_ID, probe=False)
    plan.url_expiry["data[0].url"] = "not a time"

    with caplog.at_level("WARNING", logger="colab_cli.job.planner"):
        assert revalidate_expiry(plan) == []

    assert "plan records an unparseable expiry 'not a time' for data[0].url" in caplog.text


# -- the source lock ---------------------------------------------------------


def test_a_source_that_cannot_be_locked_is_a_plan_error(tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    (root / "entry.py").write_text("print(1)\n")
    (root / "link.py").symlink_to(root / "entry.py")
    spec = _spec(tmp_path, code=CodeSpec(kind="bundle", root=str(root), entry="entry.py"), data=[])

    plan = build_plan(spec, JOB_ID, probe=False)

    [diagnostic] = _by_code(plan, SOURCE_UNREADABLE)
    assert diagnostic.retry_class is RetryClass.FIX_CODE
    assert "link.py" in diagnostic.message
    assert plan.source_files == []


def test_a_missing_entry_is_reported_once(tmp_path):
    spec = _spec(tmp_path, code=CodeSpec(root=str(tmp_path), entry="absent.py"), data=[])

    plan = build_plan(spec, JOB_ID, probe=False)

    assert {d.code for d in plan.diagnostics} == {"code_entry_missing"}


def test_build_plan_records_the_source_lock(tmp_path):
    plan = build_plan(_spec(tmp_path, data=[]), JOB_ID, probe=False)

    assert [f.path for f in plan.source_files] == ["entry.py"]


def test_after_install_a_control_url_needs_only_the_rest_of_the_run(tmp_path):
    """retry.budget_seconds is counted from apply; after install, the result
    PUT is due at the end of the run."""
    put = _signed(60 * 3).replace("input.bin", "result.json")
    get = put.replace("Signature=SECRET", "Signature=SECRET2")
    spec = _spec(
        tmp_path,
        budgets=Budgets(wall_clock=3600),
        retry=Retry(budget_seconds=4 * 3600),
        control=Control(result=ControlChannel(put_url=put, get_url=get)),
        data=[],
    )
    plan = build_plan(spec, JOB_ID, probe=False)

    before = [d for d in revalidate_expiry(plan) if d.message.startswith("control.result")]
    assert before, "a 3-hour URL does not cover a 4-hour budget"
    assert revalidate_expiry(plan, after_install=True) == []


@pytest.mark.parametrize("url", ["https://storage.example.test:99999/x", "https:///x"])
def test_a_malformed_url_is_a_spec_error_and_is_not_probed(tmp_path, monkeypatch, url):
    probed = []
    monkeypatch.setattr(
        "colab_cli.job.planner.probe_get_url", lambda u: probed.append(u) or ProbeResult(206, 3)
    )

    plan = build_plan(_spec(tmp_path, data=[DataItem(url=url, dest="in.bin", size_bytes=3)]), JOB_ID)

    [diagnostic] = [d for d in plan.diagnostics if d.code == "url_malformed"]
    assert diagnostic.retry_class is RetryClass.FIX_CODE
    assert probed == []


def test_a_non_https_url_is_reported_once(tmp_path):
    plan = build_plan(_spec(tmp_path, data=[DataItem(url="not a url", dest="in.bin", size_bytes=3)]),
                      JOB_ID, probe=False)

    assert [d.code for d in plan.diagnostics if d.code.startswith("url_")] == ["url_scheme_not_https"]


def test_durations_never_show_sixty_minutes():
    from colab_cli.job.planner import _duration

    assert _duration(7199) == "2h"
    assert _duration(7260) == "2h1m"
    assert _duration(2700) == "45m"
