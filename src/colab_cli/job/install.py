"""Dependency install for `job apply`: uv first, pip as the fallback.

The install runs as one kernel call. On the VM, each installer writes
straight into the job's install.log, between a header (installer, version,
command, index configuration it reads) and a footer (exit code, timeout,
duration), so the log survives a lost kernel connection and is copied off
the VM before release. The kernel call returns every attempt; this module
classifies each one and combines them into a retry class.

Classification is built from real output captured on Colab
(tests/fixtures/installer_failures.json). One finding shapes the verdict:
pip reports an index answering 403, 429 or 500 only as "No matching
distribution found", identical to a missing package, while uv names the
status. The verdict therefore takes the most specific cause across all
attempts: auth over transient over everything else.
"""

from __future__ import annotations

import json
from typing import List, Optional, Tuple

from colab_cli.job.models import RetryClass
from colab_cli.job.runtime_payload import redact

# Each installer gets its own budget, so pip is not left with whatever uv
# did not use. Version probes and `pip config list` get a short one each.
INSTALL_ATTEMPT_TIMEOUT = 1500.0
INSTALL_PROBE_TIMEOUT = 60.0
# The kernel call must outlast both attempts: a kernel reply timeout is a
# transport failure (retry_same), an installer timeout is fix_code.
INSTALL_KERNEL_TIMEOUT = 2 * INSTALL_ATTEMPT_TIMEOUT + 3 * INSTALL_PROBE_TIMEOUT + 120.0

RESULT_TAG = "INSTALL_RESULT="
OUTPUT_HEAD = 4000
OUTPUT_TAIL = 12000

FAILURE_RETRY = {
    "auth": RetryClass.FIX_HUMAN,
    "transient": RetryClass.RETRY_SAME,
    "timeout": RetryClass.FIX_CODE,
    "build": RetryClass.FIX_CODE,
    "resolution": RetryClass.FIX_CODE,
    "unknown": RetryClass.FIX_CODE,
}

FAILURE_HINT = {
    "resolution": (
        "no version satisfies the pins in `deps`: check each name and version on "
        "the package index, and for conflicts with packages Colab preinstalls"
    ),
    "build": (
        "a package had to be built from source and the build failed: pin a version "
        "that ships a prebuilt wheel for this Python, or add its build dependencies"
    ),
    "auth": (
        "the package index refused the request (401/403): check the index URL and "
        "its credentials"
    ),
    "transient": (
        "the package index was unreachable or failing (DNS, connection, 429, 5xx): "
        "retrying the same job is expected to work"
    ),
    "timeout": (
        "an installer ran past its budget, usually a source build: pin a version "
        "with a prebuilt wheel or install fewer packages"
    ),
    "unknown": "the installer failed in a way not recognised here: read install.log",
}

# Installer output that identifies a cause. Checked in this order: a
# timeout, then auth, then transient, then build, then resolution.
_UV_RULES = (
    ("auth", ("(401 Unauthorized)", "lack of valid authentication credentials", "403 Forbidden")),
    (
        "transient",
        (
            "Request failed after",
            "dns error",
            "tcp connect error",
            "Connection refused",
            "HTTP status server error",
            "429 Too Many Requests",
            "operation timed out",
        ),
    ),
    ("build", ("The build backend returned an error", "Failed to build")),
    ("resolution", ("No solution found when resolving dependencies", "not found in the package registry")),
)
_PIP_RULES = (
    ("auth", ("_prompt_for_password", "401 Client Error", "403 Client Error")),
    # Only exhausted retries: pip also warns on retries that later succeed.
    ("transient", ("Retry(total=0", "Max retries exceeded")),
    (
        "build",
        (
            "subprocess-exited-with-error",
            "metadata-generation-failed",
            "Failed building wheel",
            "Failed to build",
        ),
    ),
    (
        "resolution",
        (
            "No matching distribution found",
            "ResolutionImpossible",
            "Could not find a version that satisfies",
        ),
    ),
)

_MAX_KEY_LINES = 8


def classify_attempt(attempt: dict) -> Tuple[Optional[str], List[str]]:
    """`(failure, key_lines)` for one installer attempt; failure is None
    for a successful attempt."""
    output = f"{attempt.get('output_head', '')}\n{attempt.get('output_tail', '')}"
    key_lines = _key_lines(attempt["installer"], output)
    if attempt.get("timed_out"):
        return "timeout", key_lines or [f"no exit within {INSTALL_ATTEMPT_TIMEOUT:.0f}s"]
    if attempt.get("exit_code") == 0:
        return None, key_lines
    rules = _UV_RULES if attempt["installer"] == "uv" else _PIP_RULES
    for failure, markers in rules:
        if any(marker in output for marker in markers):
            return failure, key_lines
    return "unknown", key_lines or [line for line in output.strip().splitlines()[-3:]]


def _key_lines(installer: str, output: str) -> List[str]:
    lines = output.splitlines()
    picked: List[str] = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if installer == "uv":
            keep = stripped.startswith(("error:", "cause:", "hint:"))
        else:
            keep = (
                stripped.startswith(("ERROR:", "error:", "×", "EOFError"))
                or "Retry(total=0" in stripped
            )
            if stripped == "error: subprocess-exited-with-error":
                # pip prints the failed build's own stderr just above.
                block = [
                    earlier.strip()
                    for earlier in lines[max(0, index - 4) : index]
                    if earlier.strip()
                ]
                picked.extend(
                    earlier
                    for earlier in block
                    if not earlier.startswith(("Processing", "Preparing"))
                )
        if keep:
            picked.append(stripped)
    deduped: List[str] = []
    for line in picked:
        if line not in deduped:
            deduped.append(line)
    return deduped[:_MAX_KEY_LINES]


def install_verdict(attempts: List[dict]) -> Tuple[RetryClass, str]:
    """The retry class for a failed install, from every attempt's failure:
    auth over transient over the rest, since pip hides index statuses that
    uv names."""
    failures = [a["failure"] for a in attempts if a.get("failure")]
    for preferred in ("auth", "transient", "timeout"):
        if preferred in failures:
            return FAILURE_RETRY[preferred], preferred
    failure = failures[0] if failures else "unknown"
    return FAILURE_RETRY[failure], failure


def redact_attempt(attempt: dict) -> dict:
    """Every string in an attempt with credentials removed, before it is
    stored or shown."""

    def clean(value):
        if isinstance(value, str):
            return redact.redact_credentials(value)
        if isinstance(value, list):
            return [clean(item) for item in value]
        if isinstance(value, dict):
            return {key: clean(item) for key, item in value.items()}
        return value

    return {key: clean(value) for key, value in attempt.items()}


def short_version(version: str) -> str:
    """`pip 24.1.2` from `pip 24.1.2 from /usr/local/... (python 3.13)`."""
    return " ".join(version.split()[:2])


def failure_reason(packages: List[str], attempts: List[dict], failure: str) -> str:
    """One bounded line: the packages, then each installer with its exit
    status, failure class and first key lines."""
    parts = []
    for attempt in attempts:
        status = "timed out" if attempt.get("timed_out") else f"exit {attempt.get('exit_code')}"
        lines = " / ".join(attempt.get("key_lines", [])[:3])
        parts.append(
            f"{short_version(attempt['version'])} {status} ({attempt.get('failure')}): {lines}"
        )
    text = (
        f"dependency install failed ({failure}) for "
        f"{redact.redact_credentials(' '.join(packages))}: " + "; ".join(parts)
    )
    return text if len(text) <= 1500 else text[:1497] + "..."


def parse_install_result(text: str) -> Optional[List[dict]]:
    for line in text.splitlines():
        if line.startswith(RESULT_TAG):
            return json.loads(line[len(RESULT_TAG):])
    return None


def install_code(log_path: str, packages: List[str]) -> str:
    """The kernel code that runs the install on the VM."""
    prologue = (
        f"LOG = {log_path!r}\n"
        f"PKGS = {list(packages)!r}\n"
        f"ATTEMPT_TIMEOUT = {INSTALL_ATTEMPT_TIMEOUT!r}\n"
        f"PROBE_TIMEOUT = {INSTALL_PROBE_TIMEOUT!r}\n"
        f"HEAD = {OUTPUT_HEAD!r}\n"
        f"TAIL = {OUTPUT_TAIL!r}\n"
        f"RESULT_TAG = {RESULT_TAG!r}\n"
        # The redaction patterns come from redact.py, their single owner;
        # the runtime payload is not staged yet when install runs.
        f"QUERY_PATTERN = {redact._QUERY.pattern!r}\n"
        f"USERINFO_PATTERN = {redact._USERINFO.pattern!r}\n"
    )
    return prologue + _KERNEL_BODY


_KERNEL_BODY = r'''
import json, os, re, shutil, subprocess, sys, time

_QUERY = re.compile(QUERY_PATTERN)
_USERINFO = re.compile(USERINFO_PATTERN)


def _redact(text):
    return _QUERY.sub("?<redacted>", _USERINFO.sub(r"\1***@", text))


def _first_line(cmd):
    try:
        out = subprocess.run(
            cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=PROBE_TIMEOUT,
        )
    except Exception as error:
        return "unknown (%s: %s)" % (type(error).__name__, error)
    lines = (out.stdout or out.stderr or "").strip().splitlines()
    return lines[0] if lines else "unknown (exit %s)" % out.returncode


_INDEX_FLAGS = (
    "--index-url", "-i", "--extra-index-url", "--default-index", "--index",
    "--find-links", "-f",
)


def _index_flags_in_deps():
    found = {}
    for position, arg in enumerate(PKGS):
        for flag in _INDEX_FLAGS:
            if arg == flag and position + 1 < len(PKGS):
                found.setdefault("deps " + flag, []).append(PKGS[position + 1])
            elif flag.startswith("--") and arg.startswith(flag + "="):
                found.setdefault("deps " + flag, []).append(arg.split("=", 1)[1])
    return {key: " ".join(values) for key, values in found.items()}


_UV_ENV = (
    "UV_DEFAULT_INDEX", "UV_INDEX", "UV_INDEX_URL", "UV_EXTRA_INDEX_URL",
    "UV_FIND_LINKS", "UV_INDEX_STRATEGY", "UV_KEYRING_PROVIDER", "UV_OFFLINE",
    "UV_CONSTRAINT", "UV_BUILD_CONSTRAINT", "UV_PRERELEASE",
)
_PIP_ENV = (
    "PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL", "PIP_FIND_LINKS", "PIP_NO_INDEX",
    "PIP_CONSTRAINT", "PIP_PRE",
)


def _uv_index():
    found = {name: os.environ[name] for name in _UV_ENV if os.environ.get(name)}
    found.update(_index_flags_in_deps())
    return found


def _pip_index():
    found = {name: os.environ[name] for name in _PIP_ENV if os.environ.get(name)}
    try:
        config = subprocess.run(
            [sys.executable, "-m", "pip", "config", "list"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=PROBE_TIMEOUT,
        ).stdout
    except Exception as error:
        config = ""
        found["pip config"] = "unreadable (%s)" % type(error).__name__
    for line in (config or "").splitlines():
        key, _, value = line.partition("=")
        if any(word in key for word in ("index-url", "find-links", "constraint", "no-index")):
            found["pip.conf " + key.strip()] = value.strip().strip("'\"")
    found.update(_index_flags_in_deps())
    return found


plan = []
uv = shutil.which("uv")
if uv:
    plan.append(("uv", [uv, "pip", "install", "--system"] + PKGS, [uv, "--version"], _uv_index))
plan.append((
    "pip",
    [sys.executable, "-m", "pip", "install", "-v", "--upgrade-strategy", "only-if-needed"] + PKGS,
    [sys.executable, "-m", "pip", "--version"],
    _pip_index,
))

os.makedirs(os.path.dirname(LOG), exist_ok=True)
open(LOG, "w").close()
attempts = []
for installer, command, version_command, index in plan:
    header = {
        "installer": installer,
        "version": _first_line(version_command),
        "command": command,
        "index": index(),
    }
    with open(LOG, "a") as log:
        log.write(_redact("=== mighty-colab install attempt " + json.dumps(header)) + "\n")
    start = os.path.getsize(LOG)
    started = time.time()
    timed_out = False
    # Append mode: the installer writes at the end of the file however far
    # this process's own file position lags behind.
    with open(LOG, "a") as log:
        try:
            exit_code = subprocess.run(
                command, stdin=subprocess.DEVNULL, stdout=log,
                stderr=subprocess.STDOUT, timeout=ATTEMPT_TIMEOUT,
            ).returncode
        except subprocess.TimeoutExpired:
            exit_code, timed_out = None, True
    with open(LOG, "rb") as reader:
        reader.seek(start)
        output = reader.read().decode("utf-8", "replace")
    footer = {
        "installer": installer,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "seconds": round(time.time() - started, 1),
    }
    with open(LOG, "a") as log:
        log.write("\n=== mighty-colab install result " + json.dumps(footer) + "\n")
    attempt = dict(header, **footer)
    attempt["output_head"] = _redact(output[:HEAD])
    attempt["output_tail"] = _redact(output[-TAIL:])
    attempt["index"] = {key: _redact(value) for key, value in attempt["index"].items()}
    attempt["command"] = [_redact(part) for part in attempt["command"]]
    attempts.append(attempt)
    if exit_code == 0:
        break
print(RESULT_TAG + json.dumps(attempts))
'''
