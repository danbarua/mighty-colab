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

"""Direct tests for the `--json` envelope's Pydantic model family."""

import pytest
from pydantic import ValidationError

from colab_cli.envelopes import (
    Block,
    EnvelopeBase,
    ExecAsyncStarted,
    ExecEnvelope,
    RunEnvelope,
)


def _base_kwargs(**overrides):
    kwargs = dict(
        schema_version="1",
        cli_version="abc1234",
        command="exec",
        status="ok",
        exit_code=0,
    )
    kwargs.update(overrides)
    return kwargs


def test_envelope_base_valid_construction():
    envelope = EnvelopeBase(**_base_kwargs())
    assert envelope.status == "ok"
    assert envelope.reason is None
    assert envelope.content is None
    assert envelope.next_offset is None


def test_envelope_base_command_is_required():
    kwargs = _base_kwargs()
    del kwargs["command"]
    with pytest.raises(ValidationError):
        EnvelopeBase(**kwargs)


def test_envelope_base_rejects_unexpected_field():
    with pytest.raises(ValidationError):
        EnvelopeBase(**_base_kwargs(unexpected_field="boom"))


def test_envelope_base_accepts_content_and_next_offset():
    """These are cross-cutting log--tail-poll metadata, not part of any
    one command's own semantics -- available on every envelope shape via
    the base, not a dedicated subclass."""
    envelope = EnvelopeBase(
        **_base_kwargs(status="running", content="step 0\n", next_offset=7)
    )
    assert envelope.content == "step 0\n"
    assert envelope.next_offset == 7


def test_envelope_base_error_shape():
    envelope = EnvelopeBase(
        **_base_kwargs(status="error", exit_code=1, reason="session_not_found")
    )
    assert envelope.reason == "session_not_found"


def test_block_valid_construction():
    block = Block(
        code="print(1)",
        outputs=[{"output_type": "stream", "text": "1\n"}],
        cell_index=None,
        cell_id=None,
    )
    assert block.code == "print(1)"
    assert block.outputs == [{"output_type": "stream", "text": "1\n"}]


def test_block_rejects_unexpected_field():
    with pytest.raises(ValidationError):
        Block(code="x", outputs=[], extra="nope")


def test_exec_envelope_valid_construction():
    envelope = ExecEnvelope(
        **_base_kwargs(
            blocks=[
                {
                    "code": "print(1)",
                    "outputs": [{"output_type": "stream", "text": "1\n"}],
                    "cell_index": None,
                    "cell_id": None,
                }
            ]
        )
    )
    assert len(envelope.blocks) == 1
    assert isinstance(envelope.blocks[0], Block)


def test_exec_envelope_empty_blocks_is_valid():
    envelope = ExecEnvelope(**_base_kwargs(blocks=[]))
    assert envelope.blocks == []


def test_exec_envelope_requires_blocks():
    with pytest.raises(ValidationError):
        ExecEnvelope(**_base_kwargs())


def test_exec_envelope_rejects_pid_field():
    """pid/log_path belong to ExecAsyncStarted, not ExecEnvelope -- a
    mixed-up shape must fail validation, not silently pass through."""
    with pytest.raises(ValidationError):
        ExecEnvelope(**_base_kwargs(blocks=[], pid=123))


def test_run_envelope_valid_construction():
    envelope = RunEnvelope(
        **_base_kwargs(
            command="run", outputs=[{"output_type": "stream", "text": "hi\n"}]
        )
    )
    assert envelope.outputs == [{"output_type": "stream", "text": "hi\n"}]


def test_run_envelope_requires_outputs():
    with pytest.raises(ValidationError):
        RunEnvelope(**_base_kwargs(command="run"))


def test_exec_async_started_valid_construction():
    envelope = ExecAsyncStarted(
        **_base_kwargs(
            command="exec-async",
            status="started",
            pid=1234,
            log_path="/tmp/s1.exec.log",
        )
    )
    assert envelope.pid == 1234
    assert envelope.log_path == "/tmp/s1.exec.log"


def test_exec_async_started_requires_pid_and_log_path():
    with pytest.raises(ValidationError):
        ExecAsyncStarted(**_base_kwargs(command="exec-async", status="started"))


def test_exec_async_started_rejects_blocks_field():
    with pytest.raises(ValidationError):
        ExecAsyncStarted(
            **_base_kwargs(
                command="exec-async",
                status="started",
                pid=1,
                log_path="/tmp/x.log",
                blocks=[],
            )
        )
