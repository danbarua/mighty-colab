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

import os
import textwrap

from colab_cli.import_check import check_imports


def test_check_imports_succeeds_for_clean_stdlib_only_script(tmp_path):
    script = tmp_path / "clean.py"
    script.write_text("import os\nimport json\nvalue = os.path.join('a', 'b')\n")

    result = check_imports(str(script))

    assert result.ok is True
    assert result.new_sys_path_entries == []
    assert result.error is None


def test_check_imports_fails_on_genuinely_missing_module(tmp_path):
    script = tmp_path / "bad_import.py"
    script.write_text("from totally_nonexistent_package_xyz import thing\n")

    result = check_imports(str(script))

    assert result.ok is False
    assert result.missing_module == "totally_nonexistent_package_xyz"
    assert "totally_nonexistent_package_xyz" in result.error


def test_check_imports_does_not_execute_main_guarded_work(tmp_path):
    """The check imports the file as an ordinary module -- __name__ is
    never '__main__' -- so code guarded by `if __name__ == "__main__":`
    must not run. Asserted via a concrete side effect (a file the guarded
    block would create), not just 'no exception raised': this is the
    stronger assertion the whole feature depends on, given the original
    incident hinged on exactly this __name__ semantics."""
    sentinel_file = tmp_path / "side_effect_marker.txt"
    script = tmp_path / "guarded.py"
    script.write_text(
        textwrap.dedent(f"""\
            if __name__ == "__main__":
                with open({str(sentinel_file)!r}, "w") as f:
                    f.write("ran")
            """)
    )

    result = check_imports(str(script))

    assert result.ok is True
    assert not sentinel_file.exists()


def test_check_imports_kills_unguarded_work_that_exceeds_timeout(tmp_path):
    script = tmp_path / "hangs.py"
    script.write_text("import time\ntime.sleep(30)\n")

    result = check_imports(str(script), timeout=1.0)

    assert result.ok is False
    assert result.timed_out is True


def test_check_imports_reports_new_sys_path_entries(tmp_path):
    """A script that does its own sys.path.insert must have that entry
    surfaced, even independent of whether the import that follows it
    happens to succeed or fail."""
    added_dir = tmp_path / "not_on_default_path"
    script = tmp_path / "adds_sys_path.py"
    script.write_text(
        textwrap.dedent(f"""\
            import sys
            sys.path.insert(0, {str(added_dir)!r})
            """)
    )

    result = check_imports(str(script))

    assert result.ok is True
    assert str(added_dir) in result.new_sys_path_entries


def test_check_imports_catches_sibling_import_that_only_resolves_locally(tmp_path):
    """The critical regression test: a script whose sibling-import only
    resolves because the REAL local repo structure is present must FAIL
    this check -- proving the __file__ substitution + empty-cwd mechanics
    are actually wired in, not just described. A naive check that imports
    the file from its real location (real __file__, real cwd) would
    falsely PASS here, exactly reproducing the original incident: it
    worked locally and died with ModuleNotFoundError on the remote VM."""
    sibling_dir = tmp_path / "sibling"
    sibling_dir.mkdir()
    (sibling_dir / "sibling_helper.py").write_text("value = 42\n")

    script = tmp_path / "driver.py"
    script.write_text(
        textwrap.dedent("""\
            import os
            import sys
            sys.path.insert(
                0,
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "sibling"),
            )
            from sibling_helper import value
            """)
    )

    # Sanity check: this really does work with the script's REAL __file__,
    # confirming the sibling directory genuinely exists locally -- so a
    # check that DIDN'T fake __file__ would have nothing to catch here.
    import importlib.util

    spec = importlib.util.spec_from_file_location("driver_real", str(script))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.value == 42

    result = check_imports(str(script))

    assert result.ok is False
    assert result.missing_module == "sibling_helper"


def test_check_imports_uses_run_style_file_sentinel(tmp_path):
    """The overridden __file__ must match _build_script_prelude's exact
    sentinel format, or the empty-cwd mechanics silently stop mirroring
    what `run` actually does remotely. Module-scope code (not guarded by
    `if __name__ == "__main__":`) runs during the check, so a script that
    writes __file__ to a marker file at module scope lets this be
    verified directly rather than inferred."""
    marker = tmp_path / "observed_file.txt"
    script = tmp_path / "reports_file.py"
    script.write_text(
        textwrap.dedent(f"""\
            with open({str(marker)!r}, "w") as f:
                f.write(__file__)
            """)
    )

    result = check_imports(str(script))

    assert result.ok is True
    from colab_cli.commands.execution import _build_script_prelude

    prelude = _build_script_prelude(os.path.basename(str(script)))
    expected_sentinel = [
        line for line in prelude.splitlines() if line.startswith("__file__")
    ][0].split("=", 1)[1].strip()
    expected_sentinel = eval(expected_sentinel)  # noqa: S307 -- repr() round-trip of a literal we just built

    assert marker.read_text() == expected_sentinel
