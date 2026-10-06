#!/bin/bash
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

# Keep-alive soak: three CPU jobs with an idle workload, run together for
# SOAK_SECONDS (default 3 h), recording whether each VM stays listed. See
# soak.py for the variants. Not part of repro_all.sh: it takes hours and
# measures rather than passes or fails.
#
#   SOAK_SECONDS=300 OBSERVE_SECONDS=60 ./test.sh   # a short check of the harness
#   SOAK_VARIANTS="A C" ./test.sh                   # a subset
#
# The job directories and soak.log stay in SOAK_ROOT (a new temporary
# directory unless set); the session files are removed at the end.

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)
export SOAK_ROOT="${SOAK_ROOT:-$(cd "$(mktemp -d)" && pwd -P)}"
echo "soak root: $SOAK_ROOT"
cd "$REPO_ROOT"
exec uv run python "$SCRIPT_DIR/soak.py"
