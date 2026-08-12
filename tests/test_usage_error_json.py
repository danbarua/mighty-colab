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

"""Click/Typer parse errors (unknown option, unexpected extra argument,
...) under `--json`: diverted to a JSON envelope on stdout instead of
Typer's Rich-boxed stderr display, via `cli._json_aware_rich_format_error`
(hooked onto `typer.rich_utils.rich_format_error`, the one choke point
Typer funnels every parse-time `ClickException` through)."""

import json

from typer.testing import CliRunner

from colab_cli.cli import app

runner = CliRunner()


def test_usage_error_json_extra_argument_gets_hint():
    """The exact shape that prompted this feature: a session passed
    positionally instead of via `-s/--session`."""
    result = runner.invoke(app, ["--json", "stop", "badarg"])
    assert result.exit_code == 2

    envelope = json.loads(result.stdout)
    assert envelope["schema_version"] == "1"
    assert envelope["command"] == "stop"
    assert envelope["status"] == "error"
    assert envelope["exit_code"] == 2
    assert envelope["reason"] == "usage_error"
    assert "Got unexpected extra argument(s) (badarg)" in envelope["message"]
    assert envelope["hint"] == "Pass the session name with '-s', not as a positional argument."


def test_usage_error_json_unknown_option_has_no_hint():
    """A parse-error shape with no special-cased hint -- `hint` is omitted
    entirely (matching this codebase's existing "absent, not null" field
    convention), not synthesized."""
    result = runner.invoke(app, ["--json", "stop", "--bogus-flag"])
    assert result.exit_code == 2

    envelope = json.loads(result.stdout)
    assert envelope["command"] == "stop"
    assert envelope["reason"] == "usage_error"
    assert "No such option: --bogus-flag" in envelope["message"]
    assert "hint" not in envelope


def test_usage_error_without_json_is_unaffected():
    """Regression guard: the default (non-`--json`) path stays exactly as
    Typer renders it -- no envelope, no change to the Rich-boxed display."""
    result = runner.invoke(app, ["stop", "badarg"])
    assert result.exit_code == 2
    assert "{" not in result.stdout
    assert "Got unexpected extra argument(s)" in result.output
