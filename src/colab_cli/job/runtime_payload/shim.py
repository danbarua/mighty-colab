"""Run the consumer's entry in this process with script semantics.

The runner is a different OS process, so waitpid gives it only an exit code
and signal. Rich exception details must therefore be recorded by this shim,
which can catch the exception before it exits.
"""

from __future__ import annotations

import json
import os
import runpy
import sys
import traceback

# exception.json keeps the first and last characters of a long traceback:
# the head of a chained traceback is the original cause, the tail is where
# it surfaced. The full text goes to stderr, which is runner.log.
TRACEBACK_HEAD_CHARS = 2000
TRACEBACK_TAIL_CHARS = 4000


def _atomic_write_json(path, payload):
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(payload, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _systemexit_code(exc: SystemExit) -> int:
    """CPython's own mapping: None/0 -> 0, int -> int, str -> 1."""
    code = exc.code
    if code is None:
        return 0
    if isinstance(code, bool):
        return int(code)
    if isinstance(code, int):
        return code
    return 1


def _traceback_excerpt(text):
    if len(text) <= TRACEBACK_HEAD_CHARS + TRACEBACK_TAIL_CHARS:
        return text
    omitted = len(text) - TRACEBACK_HEAD_CHARS - TRACEBACK_TAIL_CHARS
    return (
        text[:TRACEBACK_HEAD_CHARS]
        + f"\n[... {omitted} characters omitted; the full traceback is in runner.log ...]\n"
        + text[-TRACEBACK_TAIL_CHARS:]
    )


def _type_name(exc):
    """Builtins by bare name, anything else module-qualified
    (`torch.OutOfMemoryError`, not `OutOfMemoryError`)."""
    kind = type(exc)
    if kind.__module__ == "builtins":
        return kind.__qualname__
    return f"{kind.__module__}.{kind.__qualname__}"


def main(argv):
    if len(argv) < 3 or argv[0] != "--job-dir":
        print("usage: shim --job-dir DIR entry.py [args...]", file=sys.stderr)
        return 2
    job_dir, entry, script_args = argv[1], argv[2], argv[3:]
    entry = os.path.abspath(entry)

    # Real `python entry.py` semantics: argv, __file__, and sys.path[0] are
    # all rooted at the entry's own directory. runpy sets __file__ in the
    # executed __main__ globals; the module assignment keeps the shim's own
    # process metadata consistent as well.
    sys.argv = [entry] + list(script_args)
    sys.path.insert(0, os.path.dirname(entry))
    globals()["__file__"] = entry

    try:
        runpy.run_path(entry, run_name="__main__")
    except SystemExit as e:
        code = _systemexit_code(e)
        # A clean sys.exit(0) is a successful completion, not a failure.
        if code != 0:
            # The traceback says where sys.exit was called.
            _atomic_write_json(
                os.path.join(job_dir, "exception.json"),
                {
                    "type": "SystemExit",
                    "message": str(e.code),
                    "traceback": _traceback_excerpt(traceback.format_exc()),
                },
            )
        return code
    except BaseException as e:  # noqa: BLE001 - deliberately record anything
        text = traceback.format_exc()
        sys.stderr.write(text)
        sys.stderr.flush()
        _atomic_write_json(
            os.path.join(job_dir, "exception.json"),
            {
                "type": _type_name(e),
                "message": str(e)[:2000],
                "traceback": _traceback_excerpt(text),
            },
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
