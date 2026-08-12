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

"""The `--json` envelope's schema, as Pydantic models.

Single source of truth for the shape `common.build_envelope`/`emit_json`
produce -- validated at emission time (see `common.emit_json`) so a shape
mismatch is a loud `pydantic.ValidationError`, not a silent drift between
what this file says and what actually goes out on stdout.

`content`/`next_offset` live on `EnvelopeBase` rather than a dedicated
subclass: they're cross-cutting metadata `log --tail --json` merges onto
whatever envelope it read (see `commands.utility._tail_log_json`), not
part of any one command's own semantics. That's what lets every
`status="error"` envelope and `log --tail`'s `status="running"` case
validate directly against the base -- no dedicated subclass needed for
either.

`outputs`/`Block.outputs` are deliberately left as `list[dict[str, Any]]`
rather than modeled further: they're nbformat-shaped, and nbformat is
upstream's contract, not this CLI's.
"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict


class EnvelopeBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str
    cli_version: str
    command: str
    status: str
    exit_code: int
    reason: Optional[str] = None
    content: Optional[str] = None
    next_offset: Optional[int] = None


class Block(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    outputs: List[Dict[str, Any]]
    cell_index: Optional[int] = None
    cell_id: Optional[str] = None


class ExecEnvelope(EnvelopeBase):
    blocks: List[Block]


class RunEnvelope(EnvelopeBase):
    outputs: List[Dict[str, Any]]


class ExecAsyncStarted(EnvelopeBase):
    pid: int
    log_path: str
