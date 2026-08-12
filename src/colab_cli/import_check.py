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

"""Import pre-flight check for `run` -- catches sibling-import failures
before a VM is provisioned, without running the script.

`run` transmits only the target file's *text* to the remote kernel --
nothing else travels with it, no sibling files, no directory structure
(see `commands.execution._build_script_prelude`, which fakes `__file__`
remotely as `<mighty-colab-exec:basename>` precisely because nothing real
backs it there). A script whose module-scope code resolves a sibling path
via `os.path.dirname(os.path.abspath(__file__))` can work perfectly
locally -- the real repo structure is genuinely sitting there -- and still
`ModuleNotFoundError` on the remote VM, where `/content` starts empty.

This check only means something if it reproduces that exact substitution:
importing the file from its *real* absolute path (no copying, no
isolating it away from real sibling files -- that part should resolve
normally), but with the loaded module's `__file__` overridden to the same
sentinel `run` sets remotely, and the subprocess's CWD set to a fresh
empty directory (since `__file__`-relative path math resolves against CWD
once `__file__` has no real path in it, mirroring `/content`'s own
"starts empty" property). Skipping either half would let a script whose
sys.path hack only works because the real local repo structure is present
falsely pass.
"""

import dataclasses
import json
import os
import shutil
import subprocess
import sys
import tempfile
from typing import List, Optional

DEFAULT_TIMEOUT_SECONDS = 30.0


@dataclasses.dataclass
class ImportCheckResult:
    ok: bool
    new_sys_path_entries: List[str] = dataclasses.field(default_factory=list)
    error: Optional[str] = None
    missing_module: Optional[str] = None
    timed_out: bool = False


def _build_check_script(abs_path: str, sentinel: str) -> str:
    """Python source run in a throwaway subprocess: imports `abs_path` as
    an ordinary module (never sets `__name__ = '__main__'`, so code
    correctly guarded by `if __name__ == "__main__":` does not execute),
    overrides `__file__` to `sentinel`, and reports the outcome plus any
    `sys.path` entries the module added as one JSON line on stdout."""
    return (
        "import importlib.util, json, sys, traceback\n"
        f"FILE_PATH = {abs_path!r}\n"
        f"SENTINEL = {sentinel!r}\n"
        "before = list(sys.path)\n"
        "result = {'ok': True, 'new_sys_path_entries': [], 'error': None, "
        "'missing_module': None}\n"
        "try:\n"
        "    spec = importlib.util.spec_from_file_location(\n"
        "        '_mighty_colab_import_check', FILE_PATH\n"
        "    )\n"
        "    module = importlib.util.module_from_spec(spec)\n"
        "    module.__file__ = SENTINEL\n"
        "    spec.loader.exec_module(module)\n"
        "except BaseException as e:\n"
        "    result['ok'] = False\n"
        "    result['error'] = ''.join(\n"
        "        traceback.format_exception_only(type(e), e)\n"
        "    ).strip()\n"
        "    result['missing_module'] = getattr(e, 'name', None)\n"
        "finally:\n"
        "    result['new_sys_path_entries'] = [\n"
        "        p for p in list(sys.path) if p not in before\n"
        "    ]\n"
        "print(json.dumps(result))\n"
    )


def check_imports(
    file_path: str, timeout: float = DEFAULT_TIMEOUT_SECONDS
) -> ImportCheckResult:
    """Import `file_path` the way `run` would fake it remotely, and report
    whether it imports cleanly.

    Reports two independent facts: (1) did the import succeed or raise --
    including a genuinely missing package, with the honest caveat that
    "installed locally" and "installed on the remote VM" aren't guaranteed
    to match, so this reports the local answer, not a prediction of the
    remote one; and (2) did the script add anything to `sys.path` (a plain
    before/after list diff, no interception) -- worth flagging even on a
    pass, since that's exactly the shape of path that won't travel with a
    real remote run.

    Runs in a subprocess with a hard `timeout`: a script with genuinely
    unguarded module-scope work (or one that just hangs) is cut off
    rather than left to run indefinitely or take the CLI process down.
    """
    abs_path = os.path.abspath(file_path)
    basename = os.path.basename(abs_path)
    sentinel = f"<mighty-colab-exec:{basename}>"
    script = _build_check_script(abs_path, sentinel)

    scratch_dir = tempfile.mkdtemp(prefix="mighty-colab-import-check-")
    try:
        try:
            proc = subprocess.run(
                [sys.executable, "-c", script],
                cwd=scratch_dir,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return ImportCheckResult(
                ok=False,
                timed_out=True,
                error=(
                    f"Import check did not finish within {timeout:g}s -- "
                    "either genuinely slow module-scope work, or "
                    "module-scope code not guarded by "
                    "`if __name__ == '__main__':`."
                ),
            )
    finally:
        shutil.rmtree(scratch_dir, ignore_errors=True)

    stdout_lines = proc.stdout.strip().splitlines()
    if not stdout_lines:
        return ImportCheckResult(
            ok=False,
            error=proc.stderr.strip()
            or f"Import check subprocess exited {proc.returncode} with no output.",
        )

    try:
        data = json.loads(stdout_lines[-1])
    except ValueError:
        return ImportCheckResult(
            ok=False,
            error=proc.stderr.strip() or "Import check produced unparseable output.",
        )

    return ImportCheckResult(
        ok=data["ok"],
        new_sys_path_entries=data.get("new_sys_path_entries", []),
        error=data.get("error"),
        missing_module=data.get("missing_module"),
    )
