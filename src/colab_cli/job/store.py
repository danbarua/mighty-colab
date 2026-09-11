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

import json
import os
import tempfile
from pathlib import Path
from typing import List, Optional

from colab_cli.job.models import JobEnvelope, JobSpec, Plan

PLAN_FILE = "plan.json"
ENVELOPE_FILE = "envelope.json"
SPEC_FILE = "spec.json"
SUPERVISOR_PID_FILE = "supervisor.pid"


def _atomic_write(path: Path, payload: str) -> None:
    """Write via a same-directory temp file + `os.replace`.

    A `job status` racing a mid-flight write must never observe a truncated
    envelope: a half-written `done: true` is worse than no answer at all.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
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


class JobStore:
    """Filesystem-backed store for job records."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def job_dir(self, job_id: str) -> Path:
        return self.root / job_id

    # -- plan ------------------------------------------------------------

    def write_plan(self, plan: Plan) -> Path:
        path = self.job_dir(plan.job_id) / PLAN_FILE
        _atomic_write(path, plan.model_dump_json(indent=2))
        return path

    def read_plan(self, job_id: str) -> Optional[Plan]:
        path = self.job_dir(job_id) / PLAN_FILE
        if not path.exists():
            return None
        return Plan.model_validate_json(path.read_text())

    # -- spec ------------------------------------------------------------

    def write_spec(self, job_id: str, spec: JobSpec) -> Path:
        path = self.job_dir(job_id) / SPEC_FILE
        _atomic_write(path, spec.model_dump_json(indent=2))
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

    def write_supervisor_pid(self, job_id: str, pid: int) -> None:
        _atomic_write(self.job_dir(job_id) / SUPERVISOR_PID_FILE, str(pid))

    def supervisor_pid(self, job_id: str) -> Optional[int]:
        path = self.job_dir(job_id) / SUPERVISOR_PID_FILE
        if not path.exists():
            return None
        try:
            return int(path.read_text().strip())
        except ValueError:
            return None

    def clear_supervisor_pid(self, job_id: str) -> None:
        try:
            (self.job_dir(job_id) / SUPERVISOR_PID_FILE).unlink()
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
