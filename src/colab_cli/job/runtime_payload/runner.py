"""Detached parent process for a job attempt.

The runner owns the process tree and the durable verdict.  It is intentionally
stdlib-only because this module is copied to the VM and imported as the
standalone ``mighty_runtime`` package.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import resource
import signal
import socket
import ssl
import subprocess
import sys
import time
import traceback
from urllib import error as urllib_error
from urllib import request
from urllib.parse import urlsplit

from . import GRACE_SECONDS, RESULT_SCHEMA_VERSION, RUNTIME_PAYLOAD_VERSION, SCHEMA_VERSION
from . import ident
from .netpolicy import (
    BlockedDestination,
    HTTPStatusError,
    UploadCutShort,
    put_public,
    urlopen_public,
)
from .redact import redact_credentials

HTTP_TIMEOUT_SECONDS = 30
# How many bytes of an HTTP error response body a transfer record keeps.
ERROR_BODY_BYTES = 300
# Kernel log lines kept as evidence of an OOM kill.
OOM_LOG_LINES = 3
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


def _escapee_exclude(watchdog_proc) -> set:
    own = {os.getpid()}
    if watchdog_proc is not None and watchdog_proc.pid:
        own.add(watchdog_proc.pid)
    return own


def _signal_escapees(job_id, sig, exclude) -> None:
    try:
        ident.signal_tagged(job_id, sig, exclude)
    except Exception as error:  # noqa: BLE001 - signaling must not eat the verdict
        _log(f"signalling tagged processes with {sig} failed: {type(error).__name__}: {error}")


def _classify_workload(workload, *, tagged, detect_ok):
    """A clean result requires containment, not just an exit code."""
    if not detect_ok and workload == "succeeded":
        return "unknown", "escapee detection unavailable"
    if tagged and workload == "succeeded":
        return "failed", "tagged descendants survived containment"
    return workload, None



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


def _read_json_checked(path):
    """(value, None) for a readable file, (None, None) for an absent one,
    and (None, why) for one that exists but cannot be read or parsed."""
    try:
        with open(path) as f:
            return json.load(f), None
    except FileNotFoundError:
        return None, None
    except Exception as error:  # noqa: BLE001 - a bad record must not eat the verdict
        return None, f"{type(error).__name__}: {error}"


def _option_value(argv, index, option):
    if index + 1 >= len(argv):
        raise ValueError(f"{option} requires a value")
    return argv[index + 1]


def _option_number(argv, index, option, kind):
    value = _option_value(argv, index, option)
    try:
        return kind(value)
    except ValueError:
        raise ValueError(f"{option} expects a number, got {value!r}") from None


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
    secrets_fd = None
    secrets_required = False
    stage_manifest = None
    offload_manifest = None
    artifact_sync_interval = None
    cli_version = "unknown"
    entry_option = None
    rest = []
    i = 0
    while i < len(argv):
        if argv[i] == "--job-dir":
            job_dir = _option_value(argv, i, "--job-dir")
            i += 2
        elif argv[i] == "--deadline":
            deadline_secs = _option_number(argv, i, "--deadline", float)
            i += 2
        elif argv[i] == "--cli-version":
            cli_version = _option_value(argv, i, "--cli-version")
            i += 2
        elif argv[i] == "--secrets-fd":
            secrets_fd = _option_number(argv, i, "--secrets-fd", int)
            i += 2
        elif argv[i] == "--secrets-required":
            secrets_required = True
            i += 1
        elif argv[i] == "--entry":
            entry_option = _option_value(argv, i, "--entry")
            i += 2
        elif argv[i] == "--stage-manifest":
            stage_manifest = _option_value(argv, i, "--stage-manifest")
            i += 2
        elif argv[i] == "--offload-manifest":
            offload_manifest = _option_value(argv, i, "--offload-manifest")
            i += 2
        elif argv[i] == "--artifact-sync-interval":
            artifact_sync_interval = _option_number(
                argv, i, "--artifact-sync-interval", float
            )
            i += 2
        else:
            rest = ([entry_option] if entry_option else []) + argv[i:]
            break
    if entry_option and not rest:
        rest = [entry_option]
    return (
        job_dir,
        deadline_secs,
        cli_version,
        secrets_fd,
        secrets_required,
        stage_manifest,
        offload_manifest,
        artifact_sync_interval,
        rest,
    )


class ManifestError(ValueError):
    """The runner's own manifest or transfer credential channel is
    unusable. Messages are fixed text and local paths, never URLs."""


class StagedMismatch(ValueError):
    """The bytes received do not match the plan. `kind` is `size` or
    `checksum`; the message gives the planned and received values."""

    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


def _load_manifest(path):
    if not path:
        return []
    try:
        with open(path) as f:
            manifest = json.load(f)
    except (OSError, ValueError) as error:
        raise ManifestError(
            f"manifest {path} is unreadable: {type(error).__name__}: {error}"
        ) from None
    if not isinstance(manifest, list):
        raise ManifestError(f"manifest {path} must be a JSON array")
    return manifest


def _url_id(url):
    """Return the planner's query-free, userinfo-free URL identity."""
    try:
        parts = urlsplit(url)
    except ValueError:
        public = url.split("?", 1)[0].split("#", 1)[0]
    else:
        host = parts.hostname or ""
        try:
            port = parts.port
        except ValueError:
            port = None
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        if port is not None:
            host = f"{host}:{port}"
        public = f"{parts.scheme}://{host}{parts.path}"
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
    return f"{public}#{digest}"


def _disable_core_dumps():
    try:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (OSError, ValueError):
        pass


def _load_transfer_secrets(fd):
    if fd is None:
        return {}, None
    try:
        with os.fdopen(fd, encoding="utf-8") as stream:
            payload = json.load(stream)
    except (OSError, ValueError) as error:
        # The exception text is not kept: a JSON error can quote the
        # payload, which holds signed URLs.
        raise ManifestError(
            f"transfer credential channel unreadable ({type(error).__name__})"
        ) from None
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ManifestError("transfer credential channel: schema_version is not 1")
    urls = payload.get("urls")
    if not isinstance(urls, dict):
        raise ManifestError("transfer credential channel: urls is not an object")
    for reference, url in urls.items():
        if (
            not isinstance(reference, str)
            or len(reference) != 64
            or any(c not in "0123456789abcdef" for c in reference)
            or not isinstance(url, str)
            or hashlib.sha256(url.encode("utf-8")).hexdigest() != reference
        ):
            raise ManifestError(
                "transfer credential channel: a URL does not match its sha256 reference"
            )
    result_ref = payload.get("result_put_ref")
    if result_ref is not None and result_ref not in urls:
        raise ManifestError(
            "transfer credential channel: result_put_ref names no URL"
        )
    return urls, urls.get(result_ref)


def _resolve_transfer_url(item, urls):
    reference = item.get("url_ref")
    declared_id = item.get("url_id")
    if reference is not None:
        if not isinstance(reference, str) or reference not in urls:
            raise ManifestError("manifest credential reference is unavailable")
        url = urls[reference]
        if _url_id(url) != declared_id:
            raise ManifestError("manifest credential reference does not match")
        return url
    url = item.get("url")
    if not isinstance(url, str) or urlsplit(url).query:
        raise ManifestError("manifest contains an unsafe credential URL")
    return url


def _http_get_to_file(url, path, expected_size=None, expected_hash=None):
    req = request.Request(url, method="GET")
    hasher = hashlib.sha256()
    size = 0
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    try:
        with urlopen_public(req, timeout=HTTP_TIMEOUT_SECONDS) as response:
            with open(tmp, "wb") as out:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if expected_size is not None and size > expected_size:
                        raise StagedMismatch(
                            "size",
                            f"received more than the planned {expected_size} bytes",
                        )
                    hasher.update(chunk)
                    out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
        if expected_size is not None and size != expected_size:
            raise StagedMismatch(
                "size", f"received {size} bytes, planned {expected_size}"
            )
        digest = hasher.hexdigest()
        if expected_hash is not None and digest.lower() != str(expected_hash).lower():
            raise StagedMismatch(
                "checksum",
                f"received sha256 {digest}, planned {str(expected_hash).lower()}",
            )
        os.replace(tmp, path)
        return size, digest
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class _HashingReader:
    def __init__(self, fh):
        self.fh = fh
        self.hasher = hashlib.sha256()
        self.size = 0

    def read(self, n=-1):
        chunk = self.fh.read(65536 if n is None or n < 0 else n)
        if chunk:
            self.hasher.update(chunk)
            self.size += len(chunk)
        return chunk


def _http_put_file(url, path):
    file_size = os.path.getsize(path)
    with open(path, "rb") as raw:
        body = _HashingReader(raw)
        put_public(
            url,
            body,
            file_size,
            {"Content-Type": "application/octet-stream"},
            timeout=HTTP_TIMEOUT_SECONDS,
        )
    return body.size, body.hasher.hexdigest()


def _log(message):
    """One line to runner.log, flushed at once: the supervisor copies
    runner.log off the VM as soon as result.json appears, and anything
    still buffered then never reaches the local job record. A log line
    that cannot be written (a full disk) must not stop the verdict."""
    try:
        print(f"[runner] {message}", flush=True)
    except (OSError, ValueError):
        pass


# No response at all: DNS, refused or reset connections, timeouts, TLS
# failures, and an upload whose response could not be read. Checked after
# HTTP errors, since urllib's HTTPError is itself a URLError.
_NETWORK_ERRORS = (
    urllib_error.URLError,
    ConnectionError,
    TimeoutError,
    socket.timeout,
    socket.gaierror,
    socket.herror,
    ssl.SSLError,
    http.client.HTTPException,
    UploadCutShort,
)


def _error_category(error):
    """The `TransferError.category` the supervisor maps to a retry class."""
    if isinstance(error, (HTTPStatusError, urllib_error.HTTPError)):
        return "http"
    if isinstance(error, BlockedDestination):
        return "blocked"
    if isinstance(error, StagedMismatch):
        return error.kind
    if isinstance(error, ManifestError):
        return "setup"
    if isinstance(error, _NETWORK_ERRORS):
        return "network"
    if isinstance(error, OSError):
        return "local"
    return "error"


def _http_error_body(error):
    try:
        return error.read(ERROR_BODY_BYTES)
    except Exception:  # noqa: BLE001 - the status alone still explains it
        return None


def _transfer_error(error, url):
    """Persistable account of a failed transfer: exception type, its
    message, its category, and for an HTTP response the status and first
    body bytes.

    The URL is replaced by its identity and every query string is removed
    from every text field: a signed URL's query is the credential, and an
    error body or message can echo the request target or a redirect.
    """

    def redact(text):
        if url:
            text = text.replace(url, _url_id(url))
            try:
                query = urlsplit(url).query
            except ValueError:
                query = ""
            if query:
                text = text.replace(query, "<redacted>")
        return redact_credentials(text)

    http_status = None
    body = None
    if isinstance(error, HTTPStatusError):
        http_status = error.status
        body = redact(error.body.decode("utf-8", "replace"))
    elif isinstance(error, urllib_error.HTTPError):
        http_status = error.code
        raw = _http_error_body(error)
        if raw:
            body = redact(raw.decode("utf-8", "replace"))
    return {
        "exception": type(error).__name__,
        "reason": redact(str(error)),
        "http_status": http_status,
        "body": body,
        "category": _error_category(error),
    }


def _log_artifact_failure(record):
    error = record.get("error") or {}
    _log(
        f"artifact upload failed path={record['path']} "
        f"http_status={error.get('http_status')} "
        f"exception={error.get('exception')} reason={error.get('reason')}"
    )


def _resolve_job_path(job_dir, path):
    if os.path.isabs(path):
        return path
    return os.path.join(job_dir, path)


class StageItemError(Exception):
    """One declared input failed to stage.

    `record` is the input's persistable record: its destination path (a
    caller-chosen relative path), its URL identity and a `_transfer_error`
    with every query string removed. The message is the destination and
    that redacted reason.
    """

    def __init__(self, record):
        self.record = record
        super().__init__(f"{record['dest']}: {record['error']['reason']}")


def _stage_one(job_dir, item, urls):
    """Fetch one declared input. Returns its record, or raises
    StageItemError carrying the failed record."""
    dest = item.get("dest") if isinstance(item, dict) else None
    declared_id = item.get("url_id") if isinstance(item, dict) else None
    record = {
        "dest": dest if isinstance(dest, str) else "",
        "url_id": declared_id if isinstance(declared_id, str) else "",
        "status": "failed",
        "sha256": None,
        "bytes": None,
    }
    url = None
    try:
        if not isinstance(item, dict):
            raise ManifestError("stage item must be an object")
        if not isinstance(dest, str):
            raise ManifestError("stage item requires dest")
        url = _resolve_transfer_url(item, urls)
        record["url_id"] = record["url_id"] or _url_id(url)
        size, digest = _http_get_to_file(
            url,
            _resolve_job_path(job_dir, dest),
            expected_size=item.get("size_bytes"),
            expected_hash=item.get("sha256"),
        )
    except Exception as error:
        record["error"] = _transfer_error(error, url)
        raise StageItemError(record) from error
    record.update(status="ok", sha256=digest, bytes=size)
    _log(f"staged dest={dest} bytes={size} sha256={digest}")
    return record


def _stage(job_dir, manifest_path, urls, records):
    """Stage every declared input in order, appending each record to
    `records`; the first failure stops staging."""
    for item in _load_manifest(manifest_path):
        try:
            records.append(_stage_one(job_dir, item, urls))
        except StageItemError as error:
            records.append(error.record)
            raise


def _artifact_record(job_dir, item, urls):
    if not isinstance(item, dict):
        raise ValueError("offload item must be an object")
    path = item.get("path")
    url = _resolve_transfer_url(item, urls)
    if not isinstance(path, str):
        raise ValueError("offload item requires path")
    record = {
        "path": path,
        "url_id": item.get("url_id") or _url_id(url),
        "status": "missing",
        "sha256": None,
        "bytes": None,
    }
    local_path = _resolve_job_path(job_dir, path)
    if not os.path.exists(local_path):
        return record
    try:
        size, digest = _http_put_file(url, local_path)
    except FileNotFoundError:
        return record
    except Exception as error:  # noqa: BLE001 - artifact failure belongs in the verdict
        record["status"] = "failed"
        record["error"] = _transfer_error(error, url)
        try:
            record["bytes"] = os.path.getsize(local_path)
        except OSError:
            pass
        _log_artifact_failure(record)
        return record
    record["sha256"] = digest
    record["bytes"] = size
    record["status"] = "ok"
    return record



def _offload(job_dir, manifest_path, urls):
    """Upload every declared artifact. Returns (records, failed, error):
    `error` says why offload could not start, else None."""
    records = []
    failed = False
    try:
        manifest = _load_manifest(manifest_path)
    except Exception as error:  # noqa: BLE001 - malformed manifest is an offload error
        message = f"offload manifest unreadable: {type(error).__name__}: {error}"
        _log(message)
        return records, True, message
    for item in manifest:
        try:
            record = _artifact_record(job_dir, item, urls)
        except Exception as error:  # noqa: BLE001 - preserve remaining artifact attempts
            path = item.get("path", "") if isinstance(item, dict) else ""
            declared_id = item.get("url_id", "") if isinstance(item, dict) else ""
            record = {
                "path": path,
                "url_id": declared_id if isinstance(declared_id, str) else "",
                "status": "failed",
                "sha256": None,
                "bytes": None,
                "error": _transfer_error(error, None),
            }
            _log_artifact_failure(record)
        records.append(record)
        required = bool(item.get("required", True)) if isinstance(item, dict) else True
        if record["status"] == "failed" or (
            record["status"] == "missing" and required
        ):
            failed = True
    return records, failed, None


def _sync_artifacts_once(job_dir, manifest, urls, last_uploaded, failures=None):
    """Re-PUT each declared artifact that has changed since the last
    successful periodic sync, so the last few minutes of a checkpoint
    are recoverable even if the VM disappears before the run's own
    end-of-run offload ever gets to run -- destroyed early, wall_clock
    kill, spot preemption, or a Claude deciding this was a good idea.

    Same signed PUT URLs `_artifact_record` uses at end-of-run, no new
    credential surface. Runs synchronously inside the consumer-wait loop
    in `main()`, not a separate thread or process: single-flight is
    guaranteed by construction (nothing else can start a second sync
    while this one is running) at the cost of delaying cancellation
    checks by however long the uploads in this pass take -- acceptable
    for artifact sizes seen in practice, and this only runs once per
    `--artifact-sync-interval`, not every loop tick.

    `last_uploaded` is mutated in place: {path: (size, mtime)} of what
    was last confirmed uploaded. A failure is logged and counted in
    `failures` ({path: (count, last reason)}) but never stops the run: a
    missed periodic sync is staleness, not corruption, and the
    end-of-run offload still runs after this. An early 403 here
    predicts that the end-of-run upload will fail too.
    """
    for item in manifest:
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        if not isinstance(path, str):
            continue
        url = None
        try:
            local_path = _resolve_job_path(job_dir, path)
            stat1 = os.stat(local_path)
            signature1 = (stat1.st_size, stat1.st_mtime)
            if last_uploaded.get(path) == signature1:
                continue  # dedup: unchanged since the last successful sync
            # Stability window: torch.save and friends do not write
            # atomically by default (no temp-file-then-rename), so a
            # snapshot taken mid-write would upload a torn file. Waiting
            # for size+mtime to hold steady across a short window is a
            # cheap, script-cooperation-free mitigation; it is not a
            # substitute for the script itself writing atomically, which
            # remains the only guarantee with no race at all.
            time.sleep(1)
            stat2 = os.stat(local_path)
            signature2 = (stat2.st_size, stat2.st_mtime)
            if signature1 != signature2:
                continue  # still being written; try again next interval
            url = _resolve_transfer_url(item, urls)
            _http_put_file(url, local_path)
            last_uploaded[path] = signature2
            # This is the only place a periodic sync is observable at
            # all: runner.log is pulled to the local machine on every
            # healthy poll tick, so this line is free per-revision
            # timing info an agent can already read without any new
            # instrumentation -- when each checkpoint/log revision was
            # actually captured, not just that syncing is configured.
            _log(
                f"artifact synced path={path} "
                f"bytes={signature2[0]} at={time.time():.0f}"
            )
        except FileNotFoundError:
            continue  # not written yet
        except Exception as error:  # noqa: BLE001 - one bad artifact must not skip the rest
            detail = _transfer_error(error, url)
            _log(
                f"artifact sync failed path={path} "
                f"http_status={detail['http_status']} "
                f"exception={detail['exception']} reason={detail['reason']}"
            )
            if failures is not None:
                count, _last = failures.get(path, (0, None))
                failures[path] = (count + 1, detail["reason"])



def _result_payload(
    *,
    workload,
    cli_version,
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
    inputs=(),
    signal_name=None,
    oom_kills=None,
    oom_log=(),
    offload_error=None,
    warnings=(),
):
    all_survivors = sorted(set(survivors) | set(tagged))
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "cli_version": cli_version,
        "runtime_payload_version": RUNTIME_PAYLOAD_VERSION,
        "workload": workload,
        "exit_code": exit_code,
        "signal": term_signal,
        # Named on the VM: signal numbers differ between Linux and the
        # supervisor's platform (SIGBUS is 7 on Linux, 10 on macOS).
        "signal_name": signal_name,
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
        "inputs": list(inputs),
        "artifacts": artifacts,
        "offload": offload_status,
        "offload_error": offload_error,
        # Kernel OOM kills during the run (system-wide), None where
        # /proc/vmstat is unreadable, with the kernel's log lines for them.
        "oom_kills": oom_kills,
        "oom_log": list(oom_log),
        # Non-fatal supervisor problems: a probe that failed, a cancel or
        # exception record that could not be read, a periodic sync that
        # kept failing.
        "runner_warnings": list(warnings),
    }


def _put_result(result_path, result_put_url):
    if not result_put_url:
        return
    try:
        _http_put_file(result_put_url, result_path)
    except Exception as e:  # noqa: BLE001 - local verdict remains authoritative
        detail = _transfer_error(e, result_put_url)
        _log(
            f"control.result PUT failed http_status={detail['http_status']} "
            f"exception={detail['exception']} reason={detail['reason']} "
            f"body={detail['body']}"
        )



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
    cli_version,
    started,
    attempt,
    error,
    inputs,
):
    """Record a staging failure: the consumer never runs.

    A failed input carries its own redacted record in `inputs`. Any other
    error is the runner's setup (credential channel, manifest); its text
    and traceback are kept with every query string and URL userinfo
    removed, in case an unexpected exception quotes a signed URL.
    """
    if isinstance(error, StageItemError):
        message, trace = str(error), ""
    else:
        message = redact_credentials(f"{type(error).__name__}: {error}")
        trace = redact_credentials(traceback.format_exc()[-4000:])
    _log(f"stage failed: {message}")
    if trace:
        _log(trace.rstrip())
    result = _result_payload(
        workload="failed",
        cli_version=cli_version,
        exit_code=1,
        term_signal=None,
        intent=None,
        exception={
            "type": type(error).__name__,
            "message": message,
            "traceback": trace,
        },
        survivors=[],
        tagged=[],
        detect_ok=ident.can_detect_escapees(),
        runner_error=message,
        attempt=attempt,
        started=started,
        phase="stage",
        artifacts=[],
        offload_status="pending",
        inputs=inputs,
    )
    _atomic_write_json(result_path, result)
    _put_result(result_path, result_put_url)


def _signal_name(number):
    try:
        return signal.Signals(number).name
    except ValueError:
        return None


def _oom_kill_count():
    """System-wide count of kernel OOM kills from /proc/vmstat, or None
    where it is unreadable. On Colab the job's own cgroup (memory.max is
    "max") did not count an OOM kill that this counter did: the limit that
    fired belongs to an enclosing cgroup."""
    try:
        with open("/proc/vmstat") as f:
            for line in f:
                name, _, value = line.partition(" ")
                if name == "oom_kill":
                    return int(value)
    except (OSError, ValueError):
        return None
    return None


def _oom_log(count):
    """The kernel's last `count` (at most OOM_LOG_LINES) "Killed process"
    lines, naming each victim's pid, command and memory; [] when the
    kernel log is unreadable."""
    try:
        out = subprocess.run(
            ["dmesg"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        _log(f"dmesg unavailable: {type(error).__name__}: {error}")
        return []
    if out.returncode != 0:
        _log(f"dmesg exited {out.returncode}: {out.stderr.strip()[:300]}")
        return []
    lines = [line.strip() for line in out.stdout.splitlines() if "Killed process" in line]
    return lines[-min(count, OOM_LOG_LINES):] if count > 0 else []


def main(argv):
    _disable_core_dumps()
    runner_argv, consumer_args = _split_consumer_args(argv)
    try:
        (
            job_dir,
            deadline_secs,
            cli_version,
            secrets_fd,
            secrets_required,
            stage_manifest,
            offload_manifest,
            artifact_sync_interval,
            rest,
        ) = _parse_args(runner_argv)
    except (TypeError, ValueError) as error:
        print(f"runner: invalid arguments: {error}", file=sys.stderr, flush=True)
        return 2
    if not job_dir or not rest:
        print(
            "usage: runner --job-dir DIR [--deadline S] [--cli-version VERSION] "
            "[--secrets-fd FD] [--stage-manifest PATH] "
            "[--offload-manifest PATH] [--artifact-sync-interval S] entry.py",
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
    urls = {}
    result_put_url = None
    inputs = []
    warnings = []

    def warn(message):
        _log(message)
        warnings.append(message)

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
        urls, result_put_url = _load_transfer_secrets(secrets_fd)
        if secrets_required and secrets_fd is None:
            raise ManifestError("required transfer credential channel is missing")
        _stage(job_dir, stage_manifest, urls, inputs)
    except BaseException as e:  # noqa: BLE001 - stage verdict must land
        _stage_failure(
            result_path=result_path,
            job_dir=job_dir,
            result_put_url=result_put_url,
            started=started,
            cli_version=cli_version,
            attempt=attempt,
            error=e,
            inputs=inputs,
        )
        return 0
    oom_before = _oom_kill_count()

    # The shim gets its OWN session. If it shared ours, killpg on the workload
    # would also kill this process before it could record why.
    job_id = os.path.basename(os.path.normpath(job_dir))
    child_env = dict(os.environ)
    child_env[ident.JOB_ENV_VAR] = job_id
    # The consumer's stdout is runner.log. Unbuffered, its last lines
    # survive a kill and each mid-run pull sees output as it is printed.
    child_env["PYTHONUNBUFFERED"] = "1"
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
    watchdog_env[ident.JOB_ENV_VAR] = job_id
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
        warn(
            f"watchdog did not start ({type(e).__name__}: {e}); only the runner "
            "enforces wall_clock and watchdog.json is not written"
        )

    exit_code = None
    term_signal = None
    runner_error = None
    term_sent = False
    kill_sent = False
    escalate_at = None

    # Loaded once, not re-read every tick: a malformed manifest here
    # disables periodic sync -- the end-of-run _offload() below does its
    # own load and is the one whose failure decides the verdict.
    sync_manifest = []
    if artifact_sync_interval and offload_manifest:
        try:
            sync_manifest = _load_manifest(offload_manifest)
        except Exception as error:  # noqa: BLE001
            warn(f"periodic artifact sync disabled: {error}")
            sync_manifest = []
    last_artifact_sync_at = started
    last_uploaded = {}
    sync_failures = {}

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
            cancel_path = os.path.join(job_dir, "cancel.json")
            cancel_requested = os.path.exists(cancel_path)
            deadline_reached = bool(deadline and now > deadline)
            if not term_sent and (cancel_requested or deadline_reached):
                if deadline_reached and not cancel_requested:
                    _atomic_write_json(
                        cancel_path,
                        {"intent": "cancelled", "by": "wall_clock", "at": now},
                    )
                _safe_killpg(shim_pgid, signal.SIGTERM)
                _signal_escapees(
                    job_id, signal.SIGTERM, _escapee_exclude(watchdog_proc)
                )
                term_sent = True
                escalate_at = now + GRACE_SECONDS
            elif term_sent and not kill_sent and now > escalate_at:
                # Only escalate if SIGTERM did not do the job.
                _safe_killpg(shim_pgid, signal.SIGKILL)
                _signal_escapees(
                    job_id, signal.SIGKILL, _escapee_exclude(watchdog_proc)
                )
                kill_sent = True
            if (
                sync_manifest
                and not term_sent
                and now - last_artifact_sync_at >= artifact_sync_interval
            ):
                _sync_artifacts_once(
                    job_dir, sync_manifest, urls, last_uploaded, sync_failures
                )
                last_artifact_sync_at = time.time()
            time.sleep(0.2)
    except BaseException as e:  # noqa: BLE001 - verdict must still land
        runner_error = redact_credentials(f"{type(e).__name__}: {e}")
        _log(redact_credentials(traceback.format_exc()).rstrip())

    for path, (count, last) in sorted(sync_failures.items()):
        warn(f"periodic sync of {path} failed {count} time(s); last: {last}")

    # Every probe below is best-effort: a ps/proc hiccup must not eat the
    # verdict, which is the same hole that already cost one cycle. A
    # failed probe is a warning in the result.
    survivors, tagged, intent, exception = [], [], None, None
    detect_ok = False
    detect_error = None
    exclude = _escapee_exclude(watchdog_proc)
    try:
        survivors = ident.descendants(shim_pgid)
    except Exception as error:  # noqa: BLE001
        warn(f"descendant probe failed: {type(error).__name__}: {error}")
    try:
        identities = ident.tagged_identities(job_id, exclude)
        if identities:
            ident.signal_identities(identities, signal.SIGTERM)
            time.sleep(GRACE_SECONDS)
            ident.signal_identities(identities, signal.SIGKILL)
            time.sleep(0.2)
    except Exception as error:  # noqa: BLE001
        warn(f"tagged process sweep failed: {type(error).__name__}: {error}")
    try:
        # The watchdog carries MIGHTY_JOB_ID too, by design, and is still
        # alive at verdict time -- exclude it or every job reports a
        # phantom escapee. Confirmed on a live VM before this was fixed.
        tagged = ident.tagged_processes(job_id, exclude=exclude)
        detect_ok = ident.can_detect_escapees()
    except Exception as error:  # noqa: BLE001
        detect_error = f"{type(error).__name__}: {error}"
        warn(f"tagged process probe failed: {detect_error}")
    cancel_path = os.path.join(job_dir, "cancel.json")
    intent, intent_error = _read_json_checked(cancel_path)
    if intent_error is not None:
        # The file's existence is what stopped the workload; an unreadable
        # one is still a cancel, by an unknown requester.
        warn(f"cancel.json unreadable: {intent_error}")
        intent = {"intent": "cancelled", "by": None, "error": intent_error}
    exception, exception_error = _read_json_checked(
        os.path.join(job_dir, "exception.json")
    )
    if exception_error is not None:
        warn(f"exception.json unreadable: {exception_error}")
    oom_after = _oom_kill_count()
    oom_kills = (
        oom_after - oom_before
        if oom_before is not None and oom_after is not None
        else None
    )
    oom_log = _oom_log(oom_kills) if oom_kills else []

    if runner_error is not None and exit_code is None and term_signal is None:
        workload = "unknown"
    elif term_sent and intent:
        # A consumer can handle SIGTERM and exit zero. The accepted cancel
        # request remains the authoritative verdict in that case.
        workload = "cancelled"
    elif term_signal is not None:
        workload = "cancelled" if intent else "failed"
    elif exit_code == 0:
        workload = "succeeded"
    else:
        workload = "failed"
    workload, contain_err = _classify_workload(
        workload, tagged=tagged, detect_ok=detect_ok
    )
    if contain_err and detect_error and not detect_ok:
        contain_err = f"{contain_err} ({detect_error})"
    if contain_err and runner_error is None:
        runner_error = contain_err

    offload_error = None
    if offload_manifest:
        artifacts, offload_failed, offload_error = _offload(
            job_dir, offload_manifest, urls
        )
        phase = "offload" if offload_failed else "run"
        offload_status = "failed" if offload_failed else "ok"
    else:
        artifacts, offload_failed = [], False
        phase = "run"
        offload_status = "not_required"
    result = _result_payload(
        workload=workload,
        cli_version=cli_version,
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
        inputs=inputs,
        signal_name=_signal_name(term_signal) if term_signal is not None else None,
        oom_kills=oom_kills,
        oom_log=oom_log,
        offload_error=offload_error,
        warnings=warnings,
    )
    # Logged before result.json exists: the supervisor copies runner.log
    # off the VM once it sees the result, and may release the VM next.
    _log(
        f"{workload} exit={exit_code} signal={term_signal} "
        f"survivors={survivors} oom_kills={oom_kills} err={runner_error}"
    )
    _atomic_write_json(result_path, result)
    _stop_watchdog(watchdog_proc)
    _put_result(result_path, result_put_url)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
