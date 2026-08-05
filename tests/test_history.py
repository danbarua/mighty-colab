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

import tempfile
import shutil
import unittest
from colab_cli.history import HistoryLogger


class TestHistory(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.logger = HistoryLogger(log_dir=self.test_dir)

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_log_and_get_history(self):
        self.logger.log_event("test-session", "session_created", {"variant": "DEFAULT"})
        self.logger.log_event(
            "test-session", "execution", {"code": "print(1)", "outputs": []}
        )

        history = self.logger.get_history("test-session")
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["event_type"], "session_created")
        self.assertEqual(history[1]["event_type"], "execution")
        self.assertEqual(history[1]["code"], "print(1)")

    def test_list_sessions(self):
        self.logger.log_event("s1", "event", {})
        self.logger.log_event("s2", "event", {})

        sessions = self.logger.list_sessions()
        self.assertIn("s1", sessions)
        self.assertIn("s2", sessions)
        self.assertEqual(len(sessions), 2)

    def test_log_event_uses_a_write_lock(self):
        """Regression guard: log_event must serialize concurrent writers to
        the same session's history file via a real file lock -- this is
        the only piece of shared on-disk state in the codebase without one
        (see state.py's _LockedFileStore for the established pattern)."""
        import filelock
        from unittest.mock import patch

        real_rwlock = filelock.ReadWriteLock

        created_locks = []

        def spying_rwlock(*args, **kwargs):
            rw = real_rwlock(*args, **kwargs)
            created_locks.append(rw)
            return rw

        with patch("colab_cli.history.filelock.ReadWriteLock", side_effect=spying_rwlock):
            self.logger.log_event("test-session", "session_created", {})

        self.assertTrue(created_locks, "log_event never constructed a ReadWriteLock")


if __name__ == "__main__":
    unittest.main()
