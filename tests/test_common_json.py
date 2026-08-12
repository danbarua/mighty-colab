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

"""Shared `--json` infrastructure: `build_envelope`, ANSI stripping, and the
CPython-mirroring `SystemExit` -> exit-code derivation shared by `exec` and
`run`.
"""

from colab_cli.common import (
    SCHEMA_VERSION,
    _exit_code_from_outputs,
    _is_systemexit,
    _strip_ansi,
    build_envelope,
    json_safe_outputs,
    make_ansi_stream_stripper,
)


def test_build_envelope_shape(mocker):
    mocker.patch("colab_cli.auto_update.get_app_version", return_value="1.2.3")
    envelope = build_envelope("ok", "exec", exit_code=0)
    assert envelope == {
        "schema_version": SCHEMA_VERSION,
        "cli_version": "1.2.3",
        "command": "exec",
        "status": "ok",
        "exit_code": 0,
    }


def test_build_envelope_includes_reason_only_when_set(mocker):
    mocker.patch("colab_cli.auto_update.get_app_version", return_value="1.2.3")
    ok_envelope = build_envelope("ok", "exec")
    assert "reason" not in ok_envelope

    error_envelope = build_envelope(
        "error", "exec", exit_code=1, reason="session_not_found"
    )
    assert error_envelope["reason"] == "session_not_found"


def test_build_envelope_passes_through_extra_fields(mocker):
    mocker.patch("colab_cli.auto_update.get_app_version", return_value="1.2.3")
    envelope = build_envelope(
        "started", "exec-async", pid=123, log_path="/tmp/log"
    )
    assert envelope["pid"] == 123
    assert envelope["log_path"] == "/tmp/log"


def _systemexit_output(evalue: str):
    return {
        "output_type": "error",
        "ename": "SystemExit",
        "evalue": evalue,
        "traceback": [f"SystemExit: {evalue}\n"],
    }


def test_systemexit_zero_normalizes_to_exit_code_zero():
    """`raise SystemExit(main())` with `main() == 0` is the normal way a
    well-behaved script ends -- it must resolve to exit_code 0, never be
    treated as a job failure. This is the exact incident that motivated the
    `--json` design: IPython reports every SystemExit as an `error`-type
    output regardless of the code, so a naive `any(output_type=='error')`
    check misclassifies this as a failure.
    """
    outputs = [_systemexit_output("0")]
    assert _exit_code_from_outputs(outputs) == 0


def test_systemexit_none_normalizes_to_exit_code_zero():
    outputs = [_systemexit_output("None")]
    assert _exit_code_from_outputs(outputs) == 0


def test_systemexit_nonzero_propagates_code():
    outputs = [_systemexit_output("7")]
    assert _exit_code_from_outputs(outputs) == 7


def test_systemexit_string_message_maps_to_one():
    outputs = [_systemexit_output("some error message")]
    assert _exit_code_from_outputs(outputs) == 1


def test_other_exception_maps_to_one():
    outputs = [
        {
            "output_type": "error",
            "ename": "ValueError",
            "evalue": "boom",
            "traceback": ["ValueError: boom\n"],
        }
    ]
    assert _exit_code_from_outputs(outputs) == 1


def test_no_error_outputs_maps_to_zero():
    outputs = [{"output_type": "stream", "text": "hello\n"}]
    assert _exit_code_from_outputs(outputs) == 0


def test_is_systemexit_true_only_for_systemexit_error():
    assert _is_systemexit(_systemexit_output("0")) is True
    assert (
        _is_systemexit({"output_type": "error", "ename": "ValueError"}) is False
    )
    assert _is_systemexit({"output_type": "stream"}) is False


def test_strip_ansi_removes_sgr_escapes():
    raw = "\x1b[0;31mValueError\x1b[0m\x1b[0;31m:\x1b[0m boom"
    assert _strip_ansi(raw) == "ValueError: boom"


def test_strip_ansi_leaves_plain_text_untouched():
    assert _strip_ansi("plain text, no escapes") == "plain text, no escapes"


# ---------------------------------------------------------------------------
# make_ansi_stream_stripper: `_strip_ansi` applied chunk-by-chunk, for a live
# stream (`log -f`) where a single escape sequence can be split across two
# reads. Real captured data (a genuine IPython ValueError traceback from a
# live mighty-colab exec-async run, 2026-08-12) is used for the whole-input
# regression check, since it's the format this actually has to survive.
# ---------------------------------------------------------------------------

_REAL_ANSI_TRACEBACK = (
    "\x1b[0;31m---------------------------------------------------------------------------"
    "\x1b[0m\x1b[0;31mValueError\x1b[0m"
    "                                Traceback (most recent call last)\n"
    "\x1b[0;32m<mighty-colab-exec:ansi_demo.py>\x1b[0m in \x1b[0;36m<cell line: 0>\x1b[0;34m()\x1b[0m\n"
    "\x1b[0;31mValueError\x1b[0m: deliberate failure to produce a colored IPython traceback\n"
)

_REAL_ANSI_TRACEBACK_STRIPPED = (
    "---------------------------------------------------------------------------"
    "ValueError"
    "                                Traceback (most recent call last)\n"
    "<mighty-colab-exec:ansi_demo.py> in <cell line: 0>()\n"
    "ValueError: deliberate failure to produce a colored IPython traceback\n"
)


def test_ansi_stream_stripper_matches_strip_ansi_for_a_single_whole_chunk():
    feed = make_ansi_stream_stripper()
    assert feed(_REAL_ANSI_TRACEBACK) == _REAL_ANSI_TRACEBACK_STRIPPED


def test_ansi_stream_stripper_handles_escape_split_across_chunk_boundary():
    """The exact failure mode `_strip_ansi` alone can't handle: an escape
    sequence's opening half arrives in one chunk, its closing letter in the
    next -- `_strip_ansi` run separately on each half would leave the first
    half's `\\x1b[0;3` unmatched (no terminating letter yet) and leak it."""
    raw = "\x1b[0;31mValueError\x1b[0m"
    split_point = raw.index(";31m") + 2  # inside the escape, before "1m"
    first, second = raw[:split_point], raw[split_point:]
    assert first.endswith("\x1b[0;3")  # sanity: genuinely mid-escape

    feed = make_ansi_stream_stripper()
    out1 = feed(first)
    out2 = feed(second)
    assert out1 + out2 == "ValueError"
    # And no raw escape bytes leaked into either piece.
    assert "\x1b" not in out1
    assert "\x1b" not in out2


def test_ansi_stream_stripper_handles_escape_split_one_byte_at_a_time():
    feed = make_ansi_stream_stripper()
    out = "".join(feed(ch) for ch in _REAL_ANSI_TRACEBACK)
    assert out == _REAL_ANSI_TRACEBACK_STRIPPED


def test_ansi_stream_stripper_flushes_a_genuinely_truncated_escape_at_eof():
    """A stream that ends mid-escape (process killed mid-write) must not
    silently swallow those bytes forever -- `final=True` flushes whatever's
    pending as plain text instead of holding it."""
    feed = make_ansi_stream_stripper()
    out1 = feed("hello \x1b[0;3")
    assert out1 == "hello "
    out2 = feed("", final=True)
    assert out2 == "\x1b[0;3"


def test_ansi_stream_stripper_plain_text_across_chunks_is_untouched():
    feed = make_ansi_stream_stripper()
    assert feed("no ") + feed("escapes ") + feed("here") == "no escapes here"


def _error_output(traceback_lines):
    return {
        "output_type": "error",
        "ename": "ValueError",
        "evalue": "boom",
        "traceback": traceback_lines,
    }


def test_json_safe_outputs_strips_by_default_and_drops_raw_field():
    raw_line = "\x1b[0;31mValueError\x1b[0m\x1b[0;31m:\x1b[0m boom\n"
    result = json_safe_outputs([_error_output([raw_line])])

    out = result[0]
    assert out["traceback"] == ["ValueError: boom\n"]
    assert "traceback_raw" not in out


def test_json_safe_outputs_strip_false_preserves_ansi_and_still_no_raw_field():
    raw_line = "\x1b[0;31mValueError\x1b[0m\x1b[0;31m:\x1b[0m boom\n"
    result = json_safe_outputs([_error_output([raw_line])], strip=False)

    out = result[0]
    assert out["traceback"] == [raw_line]
    assert "traceback_raw" not in out


def test_json_safe_outputs_does_not_mutate_input():
    raw_line = "\x1b[0;31mboom\x1b[0m\n"
    original = [_error_output([raw_line])]
    json_safe_outputs(original)
    assert original[0]["traceback"] == [raw_line]


def test_json_safe_outputs_non_error_outputs_untouched():
    stream_output = {"output_type": "stream", "name": "stdout", "text": "hi\n"}
    result = json_safe_outputs([stream_output])
    assert result[0] == stream_output
    assert "traceback_raw" not in result[0]
