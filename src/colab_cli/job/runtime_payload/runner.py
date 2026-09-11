"""Detached parent process for a job attempt.

The runner owns the process tree and the durable verdict.  It is intentionally
stdlib-only because this module is copied to the VM and imported as the
standalone ``mighty_runtime`` package.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from urllib import request
from urllib.parse import urlsplit

from . import SCHEMA_VERSION
from . import ident

# Seconds between SIGTERM and SIGKILL when a budget kill fires.
GRACE_SECONDS = 5
HTTP_TIMEOUT_SECONDS = 30
_URL_ENV_NAMES = (
    "MIGHTY_CONTROL_RESULT_PUT_URL",
    "MIGHTY_RESULT_PUT_URL",
    "CONTROL_RESULT_PUT_URL",
)


def _safe_killpg(pgid, sig):
    """Signal a process group without ever raising.

    An already-empty group is not an error: BSD returns EPERM (not ESRCH)
    once the last member is gone, so catching ProcessLookupError alone lets a
    routine race kill the runner before it writes why. Any OSError here means
    "nothing left to signal".
    """
    try:
        os.killpg(pgid, sig)
        return True
    except OSError:
        return False


def _atomic_write_json(path, payload):
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _option_value(argv, index, option):
    if index + 1 >= len(argv):
        raise ValueError(f"{option} requires a value")
    return argv[index + 1]


def _split_consumer_args(argv):
    """Split runner options from consumer argv at the first bare separator."""
    try:
        separator = argv.index("--")
    except ValueError:
        return list(argv), []
    return list(argv[:separator]), list(argv[separator + 1 :])


def _parse_args(argv):
    job_dir = None
    deadline_secs = None
    result_put_url = None
    stage_manifest = None
    offload_manifest = None
    entry_option = None
    rest = []
    i = 0
    while i < len(argv):
        if argv[i] == "--job-dir":
            job_dir = _option_value(argv, i, "--job-dir")
            i += 2
        elif argv[i] == "--deadline":
            deadline_secs = float(_option_value(argv, i, "--deadline"))
            i += 2
        elif argv[i] == "--result-put-url":
            result_put_url = _option_value(argv, i, "--result-put-url")
            i += 2
        elif argv[i] == "--entry":
            entry_option = _option_value(argv, i, "--entry")
            i += 2
        elif argv[i] == "--stage-manifest":
            stage_manifest = _option_value(argv, i, "--stage-manifest")
            i += 2
        elif argv[i] == "--offload-manifest":
            offload_manifest = _option_value(argv, i, "--offload-manifest")
            i += 2
        else:
            rest = ([entry_option] if entry_option else []) + argv[i:]
            break
    if entry_option and not rest:
        rest = [entry_option]
    if result_put_url is None:
        for name in _URL_ENV_NAMES:
            result_put_url = os.environ.get(name)
            if result_put_url:
                break
    return (
        job_dir,
        deadline_secs,
        result_put_url,
        stage_manifest,
        offload_manifest,
        rest,
    )


def _load_manifest(path):
    if not path:
        return []
    with open(path) as f:
        manifest = json.load(f)
    if not isinstance(manifest, list):
        raise ValueError("manifest must be a JSON array")
    return manifest


def _url_id(url):
    """Return a query-free stable identifier for a signed URL."""
    parts = urlsplit(url)
    public = f"{parts.scheme}://{parts.netloc}{parts.path}"
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
    return f"{public}#{digest}"


def _http_get(url):
    req = request.Request(url, method="GET")
    with request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as response:
        return response.read()


def _http_put(url, data):
    req = request.Request(
        url,
        data=data,
        method="PUT",
        headers={"Content-Type": "application/octet-stream"},
    )
    with request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as response:
        response.read(1)


def _resolve_job_path(job_dir, path):
    if os.path.isabs(path):
        return path
    return os.path.join(job_dir, path)


def _stage_one(job_dir, item):
    if not isinstance(item, dict):
        raise ValueError("stage item must be an object")
    url = item.get("url")
    dest = item.get("dest")
    if not isinstance(url, str) or not isinstance(dest, str):
        raise ValueError("stage item requires url and dest")
    data = _http_get(url)
    expected_size = item.get("size_bytes")
    if expected_size is not None and len(data) != expected_size:
        raise ValueError("staged size does not match manifest")
    digest = hashlib.sha256(data).hexdigest()
    expected_hash = item.get("sha256")
    if expected_hash is not None and digest.lower() != str(expected_hash).lower():
        raise ValueError("staged sha256 does not match manifest")

    target = _resolve_job_path(job_dir, dest)
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = f"{target}.tmp.{os.getpid()}"
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, target)


def _stage(job_dir, manifest_path):
    for item in _load_manifest(manifest_path):
        _stage_one(job_dir, item)


def _artifact_record(job_dir, item):
    if not isinstance(item, dict):
        raise ValueError("offload item must be an object")
    path = item.get("path")
    url = item.get("url")
    if not isinstance(path, str) or not isinstance(url, str):
        raise ValueError("offload item requires path and url")
    record = {
        "path": path,
        "url_id": _url_id(url),
        "status": "missing",
        "sha256": None,
        "bytes": None,
    }
    local_path = _resolve_job_path(job_dir, path)
    try:
        # Read immediately before the PUT. This intentionally avoids stale
        # metadata and ensures the hash describes exactly the bytes uploaded.
        with open(local_path, "rb") as f:
            data = f.read()
    except OSError:
        return record

    digest = hashlib.sha256(data).hexdigest()
    record["sha256"] = digest
    record["bytes"] = len(data)
    try:
        _http_put(url, data)
    except Exception:  # noqa: BLE001 - artifact failure belongs in the verdict
        record["status"] = "failed"
    else:
        record["status"] = "ok"
    return record


def _offload(job_dir, manifest_path):
    records = []
    failed = False
    try:
        manifest = _load_manifest(manifest_path)
    except Exception:  # noqa: BLE001 - malformed manifest is an offload error
        return records, True
    for item in manifest:
        try:
            record = _artifact_record(job_dir, item)
        except Exception:  # noqa: BLE001 - preserve remaining artifact attempts
            path = item.get("path", "") if isinstance(item, dict) else ""
            url = item.get("url", "") if isinstance(item, dict) else ""
            record = {
                "path": path,
                "url_id": _url_id(url) if isinstance(url, str) else "",
                "status": "failed",
                "sha256": None,
                "bytes": None,
            }
        records.append(record)
        required = bool(item.get("required", True)) if isinstance(item, dict) else True
        if record["status"] == "failed" or (
            record["status"] == "missing" and required
        ):
            failed = True
    return records, failed


def _result_payload(
    *,
    workload,
    exit_code,
    term_signal,
    intent,
    exception,
    survivors,
    tagged,
    detect_ok,
    runner_error,
    attempt,
    started,
    phase,
    artifacts,
    offload_status,
):
    all_survivors = sorted(set(survivors) | set(tagged))
    return {
        "schema_version": SCHEMA_VERSION,
        "workload": workload,
        "exit_code": exit_code,
        "signal": term_signal,
        "cancel_intent": intent,
        "exception": exception,
        "surviving_descendants": all_survivors,
        "survivors_by_pgid": survivors,
        "survivors_by_job_tag": tagged,
        "escapee_detection_available": detect_ok,
        "runner_error": runner_error,
        "attempt": attempt,
        "started_at": started,
        "finished_at": time.time(),
        "phase": phase,
        "artifacts": artifacts,
        "offload": offload_status,
    }


def _put_result(result_path, result_put_url):
    if not result_put_url:
        return
    try:
        with open(result_path, "rb") as f:
            data = f.read()
        _http_put(result_put_url, data)
    except Exception as e:  # noqa: BLE001 - local verdict remains authoritative
        print(f"[runner] result PUT failed: {type(e).__name__}", file=sys.stderr)


def _stop_watchdog(watchdog_proc):
    if watchdog_proc is None:
        return
    try:
        pgid = os.getpgid(watchdog_proc.pid)
    except OSError:
        return
    _safe_killpg(pgid, signal.SIGTERM)


def _stage_failure(
    *,
    result_path,
    job_dir,
    result_put_url,
    started,
    attempt,
    error,
):
    # Do not include exception text: urllib errors can contain a signed query
    # string, which must never become part of a durable job record.
    result = _result_payload(
        workload="failed",
        exit_code=1,
        term_signal=None,
        intent=None,
        exception={
            "type": type(error).__name__,
            "message": "stage failed",
            "traceback": "",
        },
        survivors=[],
        tagged=[],
        detect_ok=ident.can_detect_escapees(),
        runner_error="stage failed",
        attempt=attempt,
        started=started,
        phase="stage",
        artifacts=[],
        offload_status="pending",
    )
    _atomic_write_json(result_path, result)
    _put_result(result_path, result_put_url)


def main(argv):
    runner_argv, consumer_args = _split_consumer_args(argv)
    try:
        (
            job_dir,
            deadline_secs,
            result_put_url,
            stage_manifest,
            offload_manifest,
            rest,
        ) = _parse_args(runner_argv)
    except (TypeError, ValueError) as e:
        print(f"runner: {e}", file=sys.stderr)
        return 2
    if not job_dir or not rest:
        print(
            "usage: runner --job-dir DIR [--deadline S] "
            "[--stage-manifest PATH] [--offload-manifest PATH] entry.py",
            file=sys.stderr,
        )
        return 2

    entry, script_args = rest[0], rest[1:] + consumer_args
    os.makedirs(job_dir, exist_ok=True)
    launch_path = os.path.join(job_dir, "launch.json")
    result_path = os.path.join(job_dir, "result.json")

    started = time.time()
    deadline = started + deadline_secs if deadline_secs else None
    attempt = int(os.environ.get("MIGHTY_ATTEMPT", "1"))

    # O_EXCL: if a launch record already exists for a live runner, this
    # invocation is a duplicate (lost RPC reply, retried call) and must NOT
    # start a second consumer.
    try:
        fd = os.open(launch_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        prior = _read_json(launch_path) or {}
        if ident.alive(
            prior.get("pid", -1),
            prior.get("starttime", ""),
            prior.get("boot_id", ""),
        ):
            print(f"[runner] duplicate launch; live runner pid={prior.get('pid')}")
            return 0
        os.replace(launch_path, os.path.join(job_dir, "launch.json.stale"))
        fd = os.open(launch_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)

    me = os.getpid()
    with os.fdopen(fd, "w") as f:
        json.dump(
            {
                "schema_version": SCHEMA_VERSION,
                "pid": me,
                "pgid": os.getpgid(me),
                "starttime": ident.starttime(me),
                "boot_id": ident.boot_id(),
                "attempt": attempt,
                "deadline": deadline,
                "started_at": started,
            },
            f,
            indent=2,
        )
        f.flush()
        os.fsync(f.fileno())

    try:
        _stage(job_dir, stage_manifest)
    except BaseException as e:  # noqa: BLE001 - stage verdict must land
        _stage_failure(
            result_path=result_path,
            job_dir=job_dir,
            result_put_url=result_put_url,
            started=started,
            attempt=attempt,
            error=e,
        )
        return 0

    # The shim gets its OWN session. If it shared ours, killpg on the workload
    # would also kill this process before it could record why.
    job_id = os.path.basename(os.path.normpath(job_dir))
    child_env = dict(os.environ)
    child_env[ident.JOB_ENV_VAR] = job_id
    # Signed control URLs belong only to the runner. Remove all supported
    # spellings from the consumer environment, even when inherited externally.
    for name in _URL_ENV_NAMES:
        child_env.pop(name, None)
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "mighty_runtime.shim",
            "--job-dir",
            job_dir,
            entry,
            *script_args,
        ],
        start_new_session=True,
        stdout=sys.stdout,
        stderr=sys.stderr,
        env=child_env,
    )
    shim_pgid = os.getpgid(proc.pid)

    # Watchdog is a sibling in its own session. It reads launch.json for the
    # runner identity and receives only local process metadata, never URLs.
    watchdog_proc = None
    watchdog_env = dict(os.environ)
    for name in _URL_ENV_NAMES:
        watchdog_env.pop(name, None)
    try:
        watchdog_proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "mighty_runtime.watchdog",
                "--job-dir",
                job_dir,
                "--shim-pgid",
                str(shim_pgid),
            ],
            start_new_session=True,
            stdout=sys.stdout,
            stderr=sys.stderr,
            env=watchdog_env,
        )
    except OSError as e:
        print(f"[runner] watchdog start failed: {type(e).__name__}", file=sys.stderr)

    exit_code = None
    term_signal = None
    runner_error = None
    term_sent = False
    kill_sent = False
    escalate_at = None

    # INVARIANT: nothing below may prevent result.json from being written. A
    # runner that dies in its own kill path produces a spurious `unknown` -- the
    # one terminal value an agent cannot act on -- caused by cleanup rather
    # than by the workload.
    try:
        while True:
            done_pid, status = os.waitpid(proc.pid, os.WNOHANG)
            if done_pid == proc.pid:
                if os.WIFSIGNALED(status):
                    term_signal = os.WTERMSIG(status)
                elif os.WIFEXITED(status):
                    exit_code = os.WEXITSTATUS(status)
                break
            now = time.time()
            if deadline and now > deadline:
                if not term_sent:
                    _atomic_write_json(
                        os.path.join(job_dir, "cancel.json"),
                        {"cancelled_by": "wall_clock", "at": now},
                    )
                    _safe_killpg(shim_pgid, signal.SIGTERM)
                    term_sent = True
                    escalate_at = now + GRACE_SECONDS
                elif not kill_sent and now > escalate_at:
                    # Only escalate if SIGTERM did not do the job.
                    _safe_killpg(shim_pgid, signal.SIGKILL)
                    kill_sent = True
            time.sleep(0.2)
    except BaseException as e:  # noqa: BLE001 - verdict must still land
        runner_error = f"{type(e).__name__}: {e}"

    # Every probe below is best-effort: a ps/proc hiccup must not eat the
    # verdict, which is the same hole that already cost one cycle.
    survivors, tagged, intent, exception = [], [], None, None
    detect_ok = False
    try:
        survivors = ident.descendants(shim_pgid)
    except Exception:  # noqa: BLE001
        pass
    try:
        # The watchdog carries MIGHTY_JOB_ID too, by design, and is still
        # alive at verdict time -- exclude it or every job reports a
        # phantom escapee. Confirmed on a live VM before this was fixed.
        own = {watchdog_proc.pid} if watchdog_proc is not None else set()
        tagged = ident.tagged_processes(job_id, exclude=own)
        detect_ok = ident.can_detect_escapees()
    except Exception:  # noqa: BLE001
        pass
    try:
        intent = _read_json(os.path.join(job_dir, "cancel.json"))
    except Exception:  # noqa: BLE001
        pass
    try:
        exception = _read_json(os.path.join(job_dir, "exception.json"))
    except Exception:  # noqa: BLE001
        pass

    if runner_error is not None and exit_code is None and term_signal is None:
        workload = "unknown"
    elif term_signal is not None:
        workload = "cancelled" if intent else "failed"
    elif exit_code == 0:
        workload = "succeeded"
    else:
        workload = "failed"

    if offload_manifest:
        artifacts, offload_failed = _offload(job_dir, offload_manifest)
        phase = "offload" if offload_failed else "run"
        offload_status = "failed" if offload_failed else "ok"
    else:
        artifacts, offload_failed = [], False
        phase = "run"
        offload_status = "not_required"
    result = _result_payload(
        workload=workload,
        exit_code=exit_code,
        term_signal=term_signal,
        intent=intent,
        exception=exception,
        survivors=survivors,
        tagged=tagged,
        detect_ok=detect_ok,
        runner_error=runner_error,
        attempt=attempt,
        started=started,
        phase=phase,
        artifacts=artifacts,
        offload_status=offload_status,
    )
    _atomic_write_json(result_path, result)
    _stop_watchdog(watchdog_proc)
    _put_result(result_path, result_put_url)
    print(
        f"[runner] {workload} exit={exit_code} signal={term_signal} "
        f"survivors={survivors} err={runner_error}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
