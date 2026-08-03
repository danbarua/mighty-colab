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

"""Live integration test for chunked upload against a real Colab session.

Everything in test_contents.py mocks `requests.request` -- it proves our
client sends the right bytes in the right requests, but nothing in this
repo can see how Colab's actual (closed-source) ContentsManager handles a
multi-chunk save, or whether it finalizes correctly on the `chunk: -1`
sentinel. This test exercises the real stack end to end: provisions an
actual VM, uploads a file well over CHUNK_SIZE, and confirms the byte
count landed intact.

Skipped by default -- this provisions a real, billable Colab VM and
requires working `mighty-colab` auth (see skills/colab-operator/SKILL.md).
Opt in explicitly:

    MIGHTY_COLAB_RUN_LIVE_TESTS=1 uv run pytest tests/test_upload_live.py -v -s
"""

import os
import subprocess
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("MIGHTY_COLAB_RUN_LIVE_TESTS"),
    reason=(
        "Provisions a real, billable Colab VM. Opt in explicitly with "
        "MIGHTY_COLAB_RUN_LIVE_TESTS=1 (requires working `mighty-colab` auth)."
    ),
)

_TIMEOUT_PROVISION_SEC = 180
_TIMEOUT_UPLOAD_SEC = 180
_TIMEOUT_EXEC_SEC = 60
_TIMEOUT_STOP_SEC = 60

# 5 chunks at the real 1MB CHUNK_SIZE -- enough to prove multi-chunk
# finalization works without the multi-minute upload time of the ~150MB
# files used to originally validate this fix by hand.
_TEST_FILE_SIZE = 5 * 1024 * 1024 + 137  # deliberately not a round multiple


def _run(*args, input_text=None, timeout):
    return subprocess.run(
        ["mighty-colab", *args],
        input=input_text,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


@pytest.fixture
def live_session():
    name = f"pytest-chunk-{uuid.uuid4().hex[:8]}"
    result = _run("new", "-s", name, timeout=_TIMEOUT_PROVISION_SEC)
    assert result.returncode == 0, f"session creation failed: {result.stderr}"
    yield name
    _run("stop", "-s", name, timeout=_TIMEOUT_STOP_SEC)


def test_chunked_upload_round_trips_large_file_on_real_session(live_session, tmp_path):
    local_file = tmp_path / "big.bin"
    local_file.write_bytes(os.urandom(_TEST_FILE_SIZE))

    upload = _run(
        "upload", "-s", live_session, str(local_file), "content/big.bin",
        timeout=_TIMEOUT_UPLOAD_SEC,
    )
    assert upload.returncode == 0, f"upload failed: {upload.stderr}"

    check = _run(
        "exec", "-s", live_session,
        input_text="import os; print(os.path.getsize('/content/big.bin'))",
        timeout=_TIMEOUT_EXEC_SEC,
    )
    assert check.returncode == 0, f"exec failed: {check.stderr}"
    assert int(check.stdout.strip()) == _TEST_FILE_SIZE
