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

"""Local job records: `~/.config/colab-cli/jobs/<job_id>/`.

The job directory is the durable half of the supervisor. A `job status`
invoked from a *different* process than the one that ran `apply` must still
be able to answer, which means the envelope is persisted after every phase
transition rather than held in memory.

It also means `apply` must persist **before** it launches anything. The
keep-alive daemon taught this repo the same lesson the hard way (AGENTS.md
item 17): a detached child that reads shared state can win the race against
its parent's first write, and then reports `not_found` for a job that is
about to exist.
"""

import fcntl
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import List, Optional

from colab_cli.job.models import JobEnvelope, JobSpec, Plan
from colab_cli.job.spec_io import has_url_query, is_redacted_url, redacted_url

PLAN_FILE = "plan.json"
ENVELOPE_FILE = "envelope.json"
SPEC_FILE = "spec.json"
SUPERVISOR_IDENTITY_FILE = "supervisor.json"
APPLY_LOCK_FILE = "apply.lock"


SECRET_SIDECAR_SUFFIX = ".mighty-colab-secrets.json"
SECRET_TEMP_PREFIX = ".mighty-colab-secret-tmp-"


def _atomic_write(path: Path, payload: str, mode: int = 0o600) -> None:
    """Atomically publish a complete owner-only record."""

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=SECRET_TEMP_PREFIX)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def plan_secrets_path(plan_path: str | Path) -> Path:
    path = Path(plan_path)
    return path.with_name(path.name + SECRET_SIDECAR_SUFFIX)


def _url_field(key: str | None) -> bool:
    return key is not None and (key == "url" or key.endswith("_url"))


def _redact(value, key=None, secrets=None):
    secrets = {} if secrets is None else secrets
    if isinstance(value, dict):
        return {name: _redact(item, name, secrets) for name, item in value.items()}
    if isinstance(value, list):
        return [_redact(item, key, secrets) for item in value]
    if isinstance(value, str) and _url_field(key) and has_url_query(value):
        marker = redacted_url(value)
        secrets[marker] = value
        return marker
    return value


def _marker_set(value, key=None):
    if isinstance(value, dict):
        markers = set()
        for name, item in value.items():
            markers.update(_marker_set(item, name))
        return markers
    if isinstance(value, list):
        markers = set()
        for item in value:
            markers.update(_marker_set(item, key))
        return markers
    if isinstance(value, str) and _url_field(key) and is_redacted_url(value):
        return {value}
    return set()


def _hydrate(value, secrets, key=None):
    if isinstance(value, dict):
        return {name: _hydrate(item, secrets, name) for name, item in value.items()}
    if isinstance(value, list):
        return [_hydrate(item, secrets, key) for item in value]
    if isinstance(value, str) and _url_field(key):
        if is_redacted_url(value):
            try:
                return secrets[value]
            except KeyError:
                raise ValueError("plan secret sidecar is missing a URL reference") from None
        if has_url_query(value):
            raise ValueError("plan file contains an unprotected signed URL")
    return value


def _read_secret_map(path: Path, markers: set[str]) -> dict[str, str]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        raise ValueError("plan secret sidecar is missing or unsafe") from None
    try:
        info = os.fstat(fd)
    except OSError:
        os.close(fd)
        raise ValueError("plan secret sidecar is invalid") from None
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        os.close(fd)
        raise ValueError("plan secret sidecar is not an owner-controlled file")
    if stat.S_IMODE(info.st_mode) & 0o077:
        os.close(fd)
        raise ValueError("plan secret sidecar has unsafe permissions")
    try:
        with os.fdopen(fd) as f:
            payload = json.load(f)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        raise ValueError("plan secret sidecar is invalid") from None
    urls = payload.get("urls") if isinstance(payload, dict) else None
    if not isinstance(urls, dict) or set(urls) != markers:
        raise ValueError("plan secret sidecar does not match the plan")
    for marker, raw in urls.items():
        if not isinstance(raw, str) or redacted_url(raw) != marker:
            raise ValueError("plan secret sidecar does not match the plan")
    return urls


def _validate_plan(payload) -> Plan:
    try:
        return Plan.model_validate(payload)
    except Exception:
        raise ValueError("plan file is invalid") from None


def load_plan_file(path: str | Path, *, hydrate: bool = False) -> Plan:
    plan_path = Path(path)
    try:
        payload = json.loads(plan_path.read_text())
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        raise ValueError("plan file is unreadable or invalid") from None
    metadata = _validate_plan(payload)
    if not hydrate:
        return metadata
    markers = _marker_set(payload)
    secrets = _read_secret_map(plan_secrets_path(plan_path), markers) if markers else {}
    return _validate_plan(_hydrate(payload, secrets))


def write_plan_file(path: str | Path, plan: Plan) -> Path:
    plan_path = Path(path)
    secrets = {}
    payload = _redact(plan.model_dump(mode="json"), secrets=secrets)
    secret_path = plan_secrets_path(plan_path)
    if secrets:
        _atomic_write(secret_path, json.dumps({"urls": secrets}, indent=2))
    _atomic_write(plan_path, json.dumps(payload, indent=2))
    if not secrets:
        secret_path.unlink(missing_ok=True)
    return plan_path



class ApplyInProgress(Exception):
    """Another live apply already owns this job ID."""

    def __init__(self, job_id: str, pid: int | None = None):
        self.job_id = job_id
        self.pid = pid
        message = f"job {job_id} is already being applied"
        if pid is not None:
            message += f" by pid {pid}"
        message += "; wait for it to finish or kill that process, then re-run"
        super().__init__(message)


class ApplyClaim:
    """Held exclusive apply lock. Release on every apply exit path."""

    def __init__(self, path: Path, fd: int):
        self.path = path
        self.fd = fd

    def release(self) -> None:
        if self.fd >= 0:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(self.fd)
            self.fd = -1
        try:
            self.path.unlink()
        except OSError:
            pass

def redacted_model_json(model) -> str:
    return json.dumps(_redact(model.model_dump(mode="json")), indent=2)

class JobStore:
    """Filesystem-backed store for job records."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def job_dir(self, job_id: str) -> Path:
        return self.root / job_id

    # -- plan ------------------------------------------------------------

    def write_plan(self, plan: Plan) -> Path:
        return write_plan_file(self.job_dir(plan.job_id) / PLAN_FILE, plan)

    @staticmethod
    def plan_secrets_path(plan_path: str | Path) -> Path:
        return plan_secrets_path(plan_path)

    def read_plan(self, job_id: str) -> Optional[Plan]:
        path = self.job_dir(job_id) / PLAN_FILE
        if not path.exists():
            return None
        return load_plan_file(path)

    def read_plan_for_apply(self, job_id: str) -> Optional[Plan]:
        path = self.job_dir(job_id) / PLAN_FILE
        if not path.exists():
            return None
        return load_plan_file(path, hydrate=True)

    # -- spec ------------------------------------------------------------

    def write_spec(self, job_id: str, spec: JobSpec) -> Path:
        path = self.job_dir(job_id) / SPEC_FILE
        _atomic_write(path, redacted_model_json(spec))
        return path

    # -- envelope --------------------------------------------------------

    def write_envelope(self, envelope: JobEnvelope) -> Path:
        path = self.job_dir(envelope.job_id) / ENVELOPE_FILE
        _atomic_write(path, envelope.model_dump_json(indent=2))
        return path

    def read_envelope(self, job_id: str) -> Optional[JobEnvelope]:
        path = self.job_dir(job_id) / ENVELOPE_FILE
        if not path.exists():
            return None
        return JobEnvelope.model_validate_json(path.read_text())

    # -- supervisor liveness ---------------------------------------------

    def write_supervisor_identity(
        self, job_id: str, *, pid: int, starttime: str, boot_id: str
    ) -> None:
        _atomic_write(
            self.job_dir(job_id) / SUPERVISOR_IDENTITY_FILE,
            json.dumps({"pid": pid, "starttime": starttime, "boot_id": boot_id}),
        )

    def claim_apply(
        self, job_id: str, *, pid: int, starttime: str, boot_id: str
    ) -> ApplyClaim:
        """Own this job ID until `ApplyClaim.release`. Fails if another apply is live."""
        directory = self.job_dir(job_id)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / APPLY_LOCK_FILE
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            owner = self._lock_identity(fd) or self.supervisor_identity(job_id)
            os.close(fd)
            raise ApplyInProgress(
                job_id, owner.get("pid") if owner else None
            ) from error
        payload = json.dumps(
            {"pid": pid, "starttime": starttime, "boot_id": boot_id}
        ).encode()
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, payload)
        os.fsync(fd)
        self.write_supervisor_identity(
            job_id, pid=pid, starttime=starttime, boot_id=boot_id
        )
        return ApplyClaim(path, fd)

    @staticmethod
    def _lock_identity(fd: int) -> Optional[dict]:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            raw = os.read(fd, 4096)
            value = json.loads(raw.decode())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError):
            return None
        if not isinstance(value, dict):
            return None
        pid = value.get("pid")
        if not isinstance(pid, int):
            return None
        return value


    def supervisor_identity(self, job_id: str) -> Optional[dict]:
        path = self.job_dir(job_id) / SUPERVISOR_IDENTITY_FILE
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text())
            if not isinstance(value["pid"], int):
                return None
            if not isinstance(value["starttime"], str):
                return None
            if not isinstance(value["boot_id"], str):
                return None
            return value
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def clear_supervisor_identity(self, job_id: str) -> None:
        try:
            (self.job_dir(job_id) / SUPERVISOR_IDENTITY_FILE).unlink()
        except OSError:
            pass

    # -- listing ---------------------------------------------------------

    def list_jobs(self) -> List[str]:
        if not self.root.exists():
            return []
        return sorted(p.name for p in self.root.iterdir() if p.is_dir())

    def append_event(self, job_id: str, event: dict) -> None:
        """Append one JSONL event to the job's own log.

        Separate from `HistoryLogger`: that records CLI invocations, while
        this records phase transitions within a single long-lived apply.
        """
        path = self.job_dir(job_id) / "events.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(event) + "\n")
