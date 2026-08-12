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

import datetime
import json
import os
from typing import Any, Dict, List

import filelock


class HistoryLogger:
    def __init__(self, log_dir: str = "~/.config/colab-cli/history"):
        self.log_dir = os.path.expanduser(log_dir)
        os.makedirs(self.log_dir, exist_ok=True)

    def _get_log_path(self, session_name: str) -> str:
        return os.path.join(self.log_dir, f"{session_name}.jsonl")

    def _lock_for(self, log_path: str) -> filelock.ReadWriteLock:
        # is_singleton=False matches state.py's _LockedFileStore: each lock
        # instance contends via the real lock file on disk, not Python
        # object identity, so a fresh instance per call is fine.
        return filelock.ReadWriteLock(f"{log_path}.lock", is_singleton=False)

    def log_event(self, session_name: str, event_type: str, data: Dict[str, Any]):
        """
        Appends a structured event to the session's history file.

        event_types (grep this file's own `.log_event(` call sites for the
        exact `data` shape each carries -- this logger itself doesn't
        constrain it):
          - session_created / session_terminated / session_refreshed /
            session_adopted
          - execution (code + outputs; exec/exec-async/run/repl also
            carry an "invocation" sub-dict with the CLI params that
            produced it, e.g. timeout/env/file)
          - stdin_request / input_reply (stdin prompts/replies)
          - file_operation (ls, rm, upload, download, edit)
          - automation / automation_result (auth, install, drivemount)
          - colab_request / drive_auth_needed / drive_auth_success
            (Drive-mount automation's own sub-events)
          - repl_started / console_started
          - keep_alive_started / keep_alive_error / keep_alive_stopped
        """
        log_path = self._get_log_path(session_name)
        event = {
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "event_type": event_type,
            **data,
        }
        with self._lock_for(log_path).write_lock():
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(event) + "\n")

    def list_sessions(self) -> List[str]:
        if not os.path.exists(self.log_dir):
            return []
        return [f[:-6] for f in os.listdir(self.log_dir) if f.endswith(".jsonl")]

    def get_history(self, session_name: str) -> List[Dict[str, Any]]:
        log_path = self._get_log_path(session_name)
        if not os.path.exists(log_path):
            return []

        with self._lock_for(log_path).read_lock():
            history = []
            with open(log_path, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        history.append(json.loads(line))
        return history
