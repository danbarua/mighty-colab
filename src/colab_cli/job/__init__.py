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

"""The `job` supervisor: spec-driven, VM-side-detached runs.

See `docs/08_job.md` for the design and the live-spike evidence behind it.

The short version of why this exists: `exec` runs inside the kernel, and the
kernel kills its children when the websocket drops -- which on a flaky link
makes it unusable for anything longer than a couple of minutes. `exec-async`
detaches on the *client* side, which is the wrong side of that hop. `job`
detaches on the **VM** side: one short kernel RPC starts a runner that
outlives it, and the verdict is read back through the Contents API without
ever calling `execute_code` again.
"""

SCHEMA_VERSION = "1"
RESULT_SCHEMA_VERSION = "2"
