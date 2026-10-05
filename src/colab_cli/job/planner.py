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

"""Plan-time validation for spec-driven jobs."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterator, Optional
from urllib.parse import urlsplit

from colab_cli.job.runtime_payload.netpolicy import BlockedDestination, resolve_public_addresses
from colab_cli.job.runtime_payload.redact import describe_error

from colab_cli.job import verdict
from colab_cli.job.install import INSTALL_KERNEL_TIMEOUT
from colab_cli.job.models import Diagnostic, JobSpec, Plan, RetryClass
from colab_cli.job.spec_io import (
    control_object_identity,
    parse_signed_url_expiry,
    probe_get_url,
    spec_hash,
    url_id,
)

_logger = logging.getLogger(__name__)
# These are the accelerator names accepted by the upstream Colab assignment
# API.  Unknown names must not silently turn into A100.
KNOWN_ACCELERATORS = frozenset({"T4", "L4", "G4", "H100", "A100", "v5e1", "v6e1"})

# Stable diagnostic identifiers.  Keep these as constants so command output,
# tests, and future clients do not duplicate string literals.
ACCELERATOR_UNKNOWN = "accelerator_unknown"
URL_SCHEME_NOT_HTTPS = "url_scheme_not_https"
URL_HOST_NOT_PUBLIC = "url_host_not_public"
URL_HOST_UNRESOLVED = "url_host_unresolved"
URL_MALFORMED = "url_malformed"
DESTINATION_OUTSIDE_JOB_DIR = "destination_outside_job_dir"
RESERVED_PATH = "reserved_path"
DUPLICATE_DESTINATION = "duplicate_destination"
DATA_DEST_COLLIDES_WITH_ARTIFACT = "data_dest_collides_with_artifact"
RETRY_NOT_IMPLEMENTED = "retry_not_implemented"
POLICY_NOT_IMPLEMENTED = "policy_not_implemented"
CODE_ENTRY_MISSING = "code_entry_missing"
CONTROL_URL_OBJECT_MISMATCH = "control_url_object_mismatch"
ENTRY_NOT_UNDER_BUNDLE = "entry_not_under_bundle"
URL_EXPIRY_TOO_SOON = "url_expiry_too_soon"
RANGED_GET_FAILED = "ranged_get_failed"
ARTIFACT_SIZE_UNKNOWN = "artifact_size_unknown"
RANGED_GET_IGNORED_RANGE = "ranged_get_ignored_range"
DATA_SIZE_UNKNOWN = "data_size_unknown"
DATA_SIZE_MISMATCH = "data_size_mismatch"
DATA_SIZE_PROBED = "data_size_probed"
SOURCE_UNREADABLE = "source_unreadable"
SOURCE_FILE_TOO_LARGE = "source_file_too_large"
SOURCE_PAYLOAD_LARGE = "source_payload_large"
ARTIFACT_SYNC_INTERVAL_INVALID = "artifact_sync_interval_invalid"

DIAGNOSTIC_CODES = frozenset(
    {
        ACCELERATOR_UNKNOWN,
        URL_SCHEME_NOT_HTTPS,
        URL_HOST_NOT_PUBLIC,
        URL_HOST_UNRESOLVED,
        URL_MALFORMED,
        DESTINATION_OUTSIDE_JOB_DIR,
        RESERVED_PATH,
        DUPLICATE_DESTINATION,
        DATA_DEST_COLLIDES_WITH_ARTIFACT,
        RETRY_NOT_IMPLEMENTED,
        POLICY_NOT_IMPLEMENTED,
        CODE_ENTRY_MISSING,
        CONTROL_URL_OBJECT_MISMATCH,
        ENTRY_NOT_UNDER_BUNDLE,
        URL_EXPIRY_TOO_SOON,
        RANGED_GET_FAILED,
        ARTIFACT_SIZE_UNKNOWN,
        RANGED_GET_IGNORED_RANGE,
        DATA_SIZE_UNKNOWN,
        DATA_SIZE_MISMATCH,
        DATA_SIZE_PROBED,
        SOURCE_UNREADABLE,
        SOURCE_FILE_TOO_LARGE,
        SOURCE_PAYLOAD_LARGE,
        ARTIFACT_SYNC_INTERVAL_INVALID,
    }
)

# Files and directories owned by the launcher/supervisor.  A consumer must not
# be able to overwrite one of these records by choosing a destination path.
_RESERVED_FILES = frozenset(
    {
        "launch.json",
        "result.json",
        "exception.json",
        "watchdog.json",
        "cancel.json",
        "install.log",
    }
)
_RESERVED_DIRECTORIES = frozenset({"mighty_runtime"})

# The extra fifteen minutes covers staging/offload and the handoff between
# attempts.  Control URLs must cover the complete retry budget instead.
_URL_SLACK_SECONDS = 900
# Install runs before the run starts and can take up to the install kernel
# call's own limit, so a URL checked before install must also cover it.
INSTALL_ALLOWANCE_SECONDS = int(INSTALL_KERNEL_TIMEOUT)


def _diagnostic(
    severity: str,
    code: str,
    message: str,
    retry_class: RetryClass,
    hint: str,
) -> Diagnostic:
    return Diagnostic(
        severity=severity,
        code=code,
        message=message,
        retry_class=retry_class,
        hint=hint,
    )


def _url_items(spec: JobSpec) -> Iterator[tuple[str, str, str]]:
    """Yield ``(field, URL, purpose)`` without exposing URL query strings."""

    for index, item in enumerate(spec.data):
        yield f"data[{index}].url", item.url, "data"
    for index, item in enumerate(spec.artifacts):
        yield f"artifacts[{index}].url", item.url, "artifact"

    control = getattr(spec, "control", None)
    for channel_name in ("result", "log"):
        channel = getattr(control, channel_name, None) if control is not None else None
        if channel is None:
            continue
        for method in ("put_url", "get_url"):
            url = getattr(channel, method, None)
            if url:
                yield f"control.{channel_name}.{method}", url, "control"


# What one DNS lookup of a URL's host found: "public", "blocked" (it
# resolves to a non-public address), "unresolved" (DNS failed) or
# "malformed" (no host, or a host or port that does not parse), with the
# detail for a diagnostic.
HostCheck = tuple[str, str]


def _check_host(url: str) -> HostCheck:
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port or 443
    except ValueError as error:
        return "malformed", str(error)
    if not host:
        return "malformed", "no host"
    try:
        resolve_public_addresses(host, port)
    except BlockedDestination as error:
        return "blocked", str(error)
    except OSError as error:
        return "unresolved", f"{host}: {describe_error(error)}"
    return "public", ""


class _HostChecks:
    """One DNS lookup per URL for the whole plan: the URL checks and the
    probe gate ask the same question."""

    def __init__(self) -> None:
        self._seen: Dict[str, HostCheck] = {}

    def __call__(self, url: str) -> HostCheck:
        if url not in self._seen:
            self._seen[url] = _check_host(url)
        return self._seen[url]



# The writable scratch on every Colab VM. Containment is enforced against
# this, not against the job's own directory: `/content/data/x.npy` and
# `/content/out/model.pt` are the conventional paths that ordinary Colab
# code already uses, and forcing everything under `/content/jobs/<id>/`
# would break the promise that an unmodified script is a valid job. One
# job owns one VM, so cross-job collision is not a live risk; escaping
# `/content` (into `/usr`, or via `..`) is.
CONTENT_ROOT = Path("/content")


def _job_root(job_id: str) -> Path:
    # ``job_id`` is generated by the CLI and is not accepted as a user path.
    # Resolve it nevertheless so the containment comparison is unambiguous.
    return Path("/content/jobs") / job_id


def _resolved_destination(raw: str, root: Path) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        path = root / path
    return path.resolve(strict=False)


def _inside(root: Path, path: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _reserved(path: Path, root: Path) -> bool:
    if not _inside(root, path):
        return False
    relative_parts = path.relative_to(root).parts
    return bool(
        any(part in _RESERVED_DIRECTORIES for part in relative_parts)
        or (relative_parts and relative_parts[-1] in _RESERVED_FILES)
    )


def _path_diagnostics(spec: JobSpec, job_id: str) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    data_destinations: list[tuple[str, Path]] = []
    artifact_paths: list[tuple[str, Path]] = []
    root = _job_root(job_id).resolve(strict=False)

    for index, item in enumerate(spec.data):
        data_destinations.append((f"data[{index}].dest", _resolved_destination(item.dest, root)))
    for index, item in enumerate(spec.artifacts):
        artifact_paths.append((f"artifacts[{index}].path", _resolved_destination(item.path, root)))

    seen: dict[str, str] = {}
    for field, destination in [*data_destinations, *artifact_paths]:
        normalized = str(destination)
        if not _inside(CONTENT_ROOT, destination):
            diagnostics.append(
                _diagnostic(
                    "error",
                    DESTINATION_OUTSIDE_JOB_DIR,
                    f"{field} resolves outside {CONTENT_ROOT}",
                    RetryClass.FIX_CODE,
                    f"Set {field} to a path below {CONTENT_ROOT}; it is the "
                    "only writable scratch that survives on a Colab VM.",
                )
            )
        if _reserved(destination, root):
            diagnostics.append(
                _diagnostic(
                    "error",
                    RESERVED_PATH,
                    f"{field} resolves onto a supervisor-owned path",
                    RetryClass.FIX_CODE,
                    "Choose a destination outside mighty_runtime and supervisor JSON files.",
                )
            )
        prior = seen.get(normalized)
        if prior is not None:
            # Cross-type collisions have a more useful, specific diagnostic
            # than a generic duplicate destination.
            if field.startswith("data[") and prior.startswith("artifacts["):
                diagnostics.append(
                    _diagnostic(
                        "error",
                        DATA_DEST_COLLIDES_WITH_ARTIFACT,
                        f"{field} duplicates artifact destination {prior}",
                        RetryClass.FIX_CODE,
                        "Use a data destination distinct from every artifact path.",
                    )
                )
            elif field.startswith("artifacts[") and prior.startswith("data["):
                diagnostics.append(
                    _diagnostic(
                        "error",
                        DATA_DEST_COLLIDES_WITH_ARTIFACT,
                        f"{field} duplicates data destination {prior}",
                        RetryClass.FIX_CODE,
                        "Use an artifact path distinct from every data destination.",
                    )
                )
            else:
                diagnostics.append(
                    _diagnostic(
                        "error",
                        DUPLICATE_DESTINATION,
                        f"{field} duplicates destination {prior}",
                        RetryClass.FIX_CODE,
                        "Give each data and artifact item a distinct destination path.",
                    )
                )
        else:
            seen[normalized] = field

    return diagnostics


def _duration(seconds: int) -> str:
    """Whole minutes, rounded up, as `1h15m`, `2h` or `45m`."""
    total_minutes = (max(0, int(seconds)) + 59) // 60
    hours, minutes = divmod(total_minutes, 60)
    if hours and minutes:
        return f"{hours}h{minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


def expiry_problems(
    spec: JobSpec,
    now: datetime,
    *,
    after_install: bool,
    stored_expiry: Optional[Dict[str, Optional[str]]] = None,
) -> list[Diagnostic]:
    """An error for each signed URL that expires before the time it must
    stay valid, measured from `now`.

    Data and artifact URLs must cover `wall_clock` plus 15 minutes for
    staging and offload, plus the install allowance when `deps` is not
    empty and install has not run yet. Before install, control URLs must
    cover the later of `retry.budget_seconds` plus 15 minutes and that same
    deadline, because the runner PUTs the result at the end of the run.
    After install, what remains for every URL is staging, the run and
    offload, so control URLs get the data deadline only.
    `stored_expiry` is a plan's recorded expiry per field, used when the
    current URL parser cannot read one from the URL.
    """

    data_parts = [f"wall_clock {spec.budgets.wall_clock}s", "15 min for staging and offload"]
    data_seconds = spec.budgets.wall_clock + _URL_SLACK_SECONDS
    if spec.deps and not after_install:
        data_seconds += INSTALL_ALLOWANCE_SECONDS
        data_parts.append(f"{_duration(INSTALL_ALLOWANCE_SECONDS)} for installing deps")
    budget_seconds = spec.retry.budget_seconds + _URL_SLACK_SECONDS
    if after_install:
        control_seconds, control_parts = data_seconds, data_parts
    elif budget_seconds >= data_seconds:
        control_seconds = budget_seconds
        control_parts = [f"retry.budget_seconds {spec.retry.budget_seconds}s", "15 min"]
    else:
        control_seconds = data_seconds
        control_parts = data_parts + ["(longer than retry.budget_seconds + 15 min)"]

    problems: list[Diagnostic] = []
    for field, url, purpose in _url_items(spec):
        expiry = parse_signed_url_expiry(url)
        if expiry is None and stored_expiry:
            stored = stored_expiry.get(field)
            if stored is not None:
                try:
                    expiry = datetime.fromisoformat(stored)
                except (TypeError, ValueError):
                    _logger.warning(
                        "plan records an unparseable expiry %r for %s (%s); "
                        "treating its expiry as unknown",
                        stored,
                        field,
                        url_id(url),
                    )
        if expiry is None:
            continue
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        seconds, parts = (
            (control_seconds, control_parts)
            if purpose == "control"
            else (data_seconds, data_parts)
        )
        deadline = now + timedelta(seconds=seconds)
        if expiry >= deadline:
            continue
        remaining = int((expiry - now).total_seconds())
        when = "has already expired" if remaining <= 0 else f"expires in {_duration(remaining)}"
        problems.append(
            _diagnostic(
                "error",
                URL_EXPIRY_TOO_SOON,
                f"{field} ({url_id(url)}) {when} ({expiry.isoformat()}); it must stay "
                f"valid for {_duration(seconds)}: {' + '.join(parts)}",
                RetryClass.REFRESH_URLS,
                f"Re-sign {field} so it remains valid for at least {_duration(seconds)}.",
            )
        )
    return problems


def _expiry_map(spec: JobSpec) -> dict[str, str | None]:
    expiry_map: dict[str, str | None] = {}
    for field, url, _purpose in _url_items(spec):
        expiry = parse_signed_url_expiry(url)
        expiry_map[field] = expiry.isoformat() if expiry is not None else None
    return expiry_map


def _url_diagnostics(spec: JobSpec, hosts: _HostChecks) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    for field, url, _purpose in _url_items(spec):
        try:
            scheme = urlsplit(url).scheme.lower()
        except ValueError:
            scheme = ""
        if scheme != "https":
            diagnostics.append(
                _diagnostic(
                    "error",
                    URL_SCHEME_NOT_HTTPS,
                    f"{field} ({url_id(url)}) does not use https",
                    RetryClass.FIX_HUMAN,
                    "Use an https URL signed for the required HTTP method.",
                )
            )
            continue
        kind, detail = hosts(url)
        if kind == "blocked":
            diagnostics.append(
                _diagnostic(
                    "error",
                    URL_HOST_NOT_PUBLIC,
                    f"{field} targets a link-local or private address ({detail})",
                    RetryClass.FIX_HUMAN,
                    "Use a public storage endpoint; private and link-local hosts are blocked.",
                )
            )
        elif kind == "malformed":
            diagnostics.append(
                _diagnostic(
                    "error",
                    URL_MALFORMED,
                    f"{field} ({url_id(url)}) is not a usable URL: {detail}",
                    RetryClass.FIX_CODE,
                    f"Correct {field}.",
                )
            )
        elif kind == "unresolved":
            diagnostics.append(
                _diagnostic(
                    "error",
                    URL_HOST_UNRESOLVED,
                    f"{field} ({url_id(url)}) host does not resolve: {detail}",
                    RetryClass.FIX_CODE,
                    "Check the host name; if this machine was offline, re-run job plan.",
                )
            )
    return diagnostics


def _control_url_diagnostics(spec: JobSpec) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    for channel_name in ("result", "log"):
        channel = getattr(spec.control, channel_name)
        if channel is None:
            continue
        put_identity = control_object_identity(channel.put_url)
        get_identity = control_object_identity(channel.get_url)
        if (
            put_identity is not None
            and get_identity is not None
            and put_identity != get_identity
        ):
            diagnostics.append(
                _diagnostic(
                    "error",
                    CONTROL_URL_OBJECT_MISMATCH,
                    f"control.{channel_name} PUT and GET URLs identify different GCS objects",
                    RetryClass.FIX_HUMAN,
                    f"Sign both control.{channel_name} URLs for the same GCS object.",
                )
            )
    return diagnostics


def _probe_diagnostics(
    spec: JobSpec, enabled: bool, hosts: _HostChecks
) -> tuple[list[Diagnostic], dict[str, int]]:
    """Diagnostics from a ranged GET of each data URL, and the sizes it
    measured for inputs with no declared `size_bytes`, keyed `data[i]`."""

    if not enabled:
        return [], {}
    diagnostics: list[Diagnostic] = []
    probed: dict[str, int] = {}
    for index, item in enumerate(spec.data):
        # Never probe a URL that already fails the scheme, SSRF or DNS
        # check. Apart from avoiding a pointless request, this keeps the
        # guard from becoming an SSRF oracle.
        try:
            safe_scheme = urlsplit(item.url).scheme.lower()
        except ValueError:
            safe_scheme = ""
        if safe_scheme != "https" or hosts(item.url)[0] != "public":
            continue
        result = probe_get_url(item.url)
        field = f"data[{index}].url"
        ident = url_id(item.url)
        if result.error is not None:
            error = result.error
            detail = error.summary + (
                f"; response body: {' '.join(error.body.split())}" if error.body else ""
            )
            diagnostics.append(
                _diagnostic(
                    "error",
                    RANGED_GET_FAILED,
                    f"{field} ({ident}) ranged GET failed: {detail}",
                    verdict.transfer_retry_class(error, "GET"),
                    "401/403: re-sign the URL or fix the grant; 404: the object "
                    "is not at this URL; 408/429/5xx or no response: re-run job plan.",
                )
            )
        elif result.range_ignored:
            diagnostics.append(
                _diagnostic(
                    "warn",
                    RANGED_GET_IGNORED_RANGE,
                    f"{field} server ignored the ranged GET; size is unknown",
                    RetryClass.RETRY_SAME,
                    "Use a storage endpoint that supports ranged GETs, or acknowledge the warning.",
                )
            )
        elif result.size_bytes is not None:
            if item.size_bytes is not None and item.size_bytes != result.size_bytes:
                diagnostics.append(
                    _diagnostic(
                        "error",
                        DATA_SIZE_MISMATCH,
                        f"data[{index}] ({ident}) is {result.size_bytes} bytes at the URL "
                        f"but size_bytes declares {item.size_bytes}",
                        RetryClass.FIX_CODE,
                        f"Set data[{index}].size_bytes to {result.size_bytes}, or point "
                        "the URL at the intended object.",
                    )
                )
            elif item.size_bytes is None:
                probed[f"data[{index}]"] = result.size_bytes
                diagnostics.append(
                    _diagnostic(
                        "info",
                        DATA_SIZE_PROBED,
                        f"data[{index}] ({ident}) declares no size_bytes; the ranged "
                        f"GET measured {result.size_bytes} bytes, used for disk planning",
                        None,
                        f"Declare data[{index}].size_bytes to have staging check it.",
                    )
                )
    return diagnostics, probed

def _code_entry_path(spec: JobSpec) -> tuple[Path, Path, bool]:
    """Return (root, candidate, escapes_root) for local code validation."""

    configured_root = getattr(spec.code, "root", None)
    root = Path(configured_root or ".").resolve(strict=False)
    candidate = root / spec.code.entry
    resolved = candidate.resolve(strict=False)
    return root, resolved, not _inside(root, resolved)


def _bundle_entry_diagnostics(spec: JobSpec) -> list[Diagnostic]:
    _root, _entry, escapes_root = _code_entry_path(spec)
    if not escapes_root:
        return []
    return [
        _diagnostic(
            "error",
            ENTRY_NOT_UNDER_BUNDLE,
            "code.entry resolves outside its configured code root",
            RetryClass.FIX_CODE,
            "Keep code.entry below code.root; remove symlink traversal outside that directory.",
        )
    ]


def revalidate_expiry(plan: Plan, *, after_install: bool = False) -> list[Diagnostic]:
    """URL expiry failures for a durable plan, measured from now: before
    assignment, and again after install."""

    return expiry_problems(
        plan.spec,
        datetime.now(timezone.utc),
        after_install=after_install,
        stored_expiry=plan.url_expiry,
    )


def _collect_source(
    spec: JobSpec, source_spec_path: str | None, covered: bool
) -> tuple[list, list[Diagnostic]]:
    """The source lock, or a diagnostic saying why it could not be built.
    `covered` is true when a missing or escaping entry is already reported."""
    from colab_cli.job.payload_bundle import collect_source_files

    try:
        files = (
            collect_source_files(spec, source_spec_path)
            if source_spec_path is not None
            else collect_source_files(spec)
        )
    except (FileNotFoundError, ValueError) as error:
        if covered:
            return [], []
        return [], [
            _diagnostic(
                "error",
                SOURCE_UNREADABLE,
                f"the source files cannot be locked: {error}",
                RetryClass.FIX_CODE,
                "Fix the source layout named above, then re-run job plan.",
            )
        ]
    except OSError as error:
        return [], [
            _diagnostic(
                "error",
                SOURCE_UNREADABLE,
                f"a source file cannot be read: {describe_error(error)}",
                RetryClass.FIX_HUMAN,
                "Fix the file's permissions or remove it, then re-run job plan.",
            )
        ]
    return files, []


def _source_size_diagnostics(files: list) -> list[Diagnostic]:
    from colab_cli.job.payload_bundle import CONTENTS_UPLOAD_CEILING

    diagnostics: list[Diagnostic] = []
    total = sum(item.size_bytes for item in files)
    for item in files:
        if item.size_bytes > CONTENTS_UPLOAD_CEILING:
            diagnostics.append(
                _diagnostic(
                    "error",
                    SOURCE_FILE_TOO_LARGE,
                    f"source file {item.path} is {item.size_bytes} bytes; "
                    "Contents uploads are limited to 250 MB per file",
                    RetryClass.FIX_CODE,
                    "Move large inputs to a data URL instead of the source bundle.",
                )
            )
    if total > CONTENTS_UPLOAD_CEILING and not diagnostics:
        diagnostics.append(
            _diagnostic(
                "warn",
                SOURCE_PAYLOAD_LARGE,
                f"aggregate source payload is {total} bytes across {len(files)} files",
                RetryClass.FIX_CODE,
                "Prefer data URLs for datasets; Contents is for code, not bulk data.",
            )
        )
    return diagnostics



def build_plan(
    spec: JobSpec,
    job_id: str,
    probe: bool = True,
    source_spec_path: str | None = None,
) -> Plan:
    """Validate ``spec`` and return a durable plan, without assigning a VM."""

    diagnostics: list[Diagnostic] = []

    # Case-insensitive on purpose. The canonical spellings are mixed case
    # (GPUs upper, TPUs lower: `T4`, `A100`, `v5e1`), which nobody will
    # remember, and the orchestrator already case-folds on the way to
    # `resolve_runtime_options`. Rejecting `t4` here would make the spec
    # quietly case-sensitive in a way nothing else about it is.
    canonical = {a.casefold(): a for a in KNOWN_ACCELERATORS}
    unknown = sorted(
        name for name in set(spec.accelerator.prefer)
        if name.casefold() not in canonical
    )
    if unknown:
        diagnostics.append(
            _diagnostic(
                "error",
                ACCELERATOR_UNKNOWN,
                f"Unknown accelerator name(s): {', '.join(unknown)}",
                RetryClass.FIX_CODE,
                f"Use only one of: {', '.join(sorted(KNOWN_ACCELERATORS))}.",
            )
        )

    unsupported_retry = []
    if spec.retry.max_attempts != 1:
        unsupported_retry.append(f"max_attempts={spec.retry.max_attempts}")
    if spec.retry.when != [RetryClass.RETRY_SAME]:
        unsupported_retry.append("when")
    if spec.retry.mode != "recreate":
        unsupported_retry.append(f"mode={spec.retry.mode}")
    if unsupported_retry:
        diagnostics.append(
            _diagnostic(
                "error",
                RETRY_NOT_IMPLEMENTED,
                "retry is not implemented; unsupported settings: "
                + ", ".join(unsupported_retry),
                RetryClass.DO_NOT_RETRY,
                "Use retry.max_attempts=1, retry.when=[retry_same], and "
                "retry.mode=recreate.",
            )
        )

    unsupported_policy = []
    if spec.on_run_fail != "offload_anyway":
        unsupported_policy.append(f"on_run_fail={spec.on_run_fail}")
    if spec.control.log is not None:
        unsupported_policy.append("control.log")
    if unsupported_policy:
        diagnostics.append(
            _diagnostic(
                "error",
                POLICY_NOT_IMPLEMENTED,
                "unsupported inactive setting(s): " + ", ".join(unsupported_policy),
                RetryClass.DO_NOT_RETRY,
                "Remove these settings until their behavior is implemented.",
            )
        )

    sync_interval = spec.budgets.artifact_sync_interval_seconds
    if sync_interval is not None:
        if sync_interval <= 0:
            diagnostics.append(
                _diagnostic(
                    "error",
                    ARTIFACT_SYNC_INTERVAL_INVALID,
                    f"budgets.artifact_sync_interval_seconds must be positive, "
                    f"got {sync_interval}",
                    RetryClass.DO_NOT_RETRY,
                    "Remove artifact_sync_interval_seconds or set it to a "
                    "positive number of seconds.",
                )
            )
        elif not spec.artifacts:
            diagnostics.append(
                _diagnostic(
                    "warn",
                    ARTIFACT_SYNC_INTERVAL_INVALID,
                    "budgets.artifact_sync_interval_seconds is set but "
                    "artifacts[] is empty; there is nothing to periodically sync",
                    RetryClass.RETRY_SAME,
                    "Declare artifacts[] to sync, or remove "
                    "artifact_sync_interval_seconds.",
                )
            )
        elif sync_interval >= spec.budgets.wall_clock:
            diagnostics.append(
                _diagnostic(
                    "warn",
                    ARTIFACT_SYNC_INTERVAL_INVALID,
                    f"budgets.artifact_sync_interval_seconds "
                    f"({sync_interval}) is not smaller than wall_clock "
                    f"({spec.budgets.wall_clock}); periodic sync may "
                    "never fire before the run's own deadline",
                    RetryClass.RETRY_SAME,
                    "Set artifact_sync_interval_seconds well below wall_clock.",
                )
            )

    hosts = _HostChecks()
    diagnostics.extend(_url_diagnostics(spec, hosts))
    diagnostics.extend(_control_url_diagnostics(spec))
    diagnostics.extend(_path_diagnostics(spec, job_id))
    diagnostics.extend(_bundle_entry_diagnostics(spec))

    _code_root, entry, _escapes_root = _code_entry_path(spec)
    if not entry.is_file():
        diagnostics.append(
            _diagnostic(
                "error",
                CODE_ENTRY_MISSING,
                f"code.entry does not exist on disk under {_code_root}: {spec.code.entry}",
                RetryClass.FIX_CODE,
                "Create code.entry or provide the path to an existing file.",
            )
        )

    now = datetime.now(timezone.utc)
    diagnostics.extend(expiry_problems(spec, now, after_install=False))

    for index, artifact in enumerate(spec.artifacts):
        if getattr(artifact, "size_bytes", None) is None:
            diagnostics.append(
                _diagnostic(
                    "warn",
                    ARTIFACT_SIZE_UNKNOWN,
                    f"artifacts[{index}] has no declared size",
                    RetryClass.RETRY_SAME,
                    "Declare artifact size_bytes to improve disk-space planning.",
                )
            )

    probe_diagnostics, probed = _probe_diagnostics(spec, probe, hosts)
    diagnostics.extend(probe_diagnostics)
    unknown = [
        f"data[{index}]"
        for index, item in enumerate(spec.data)
        if item.size_bytes is None and f"data[{index}]" not in probed
    ]
    if unknown:
        diagnostics.append(
            _diagnostic(
                "warn",
                DATA_SIZE_UNKNOWN,
                f"size unknown for {', '.join(unknown)}: no size_bytes declared "
                "and the ranged GET did not measure one",
                RetryClass.RETRY_SAME,
                "Declare size_bytes for every data item to enable disk-space planning.",
            )
        )

    covered = any(
        d.code in (CODE_ENTRY_MISSING, ENTRY_NOT_UNDER_BUNDLE) for d in diagnostics
    )
    source_files, source_diagnostics = _collect_source(spec, source_spec_path, covered)
    diagnostics.extend(source_diagnostics)
    diagnostics.extend(_source_size_diagnostics(source_files))

    return Plan(
        job_id=job_id,
        spec_hash=spec_hash(spec),
        created_at=now.isoformat(),
        spec=spec,
        diagnostics=diagnostics,
        url_expiry=_expiry_map(spec),
        probed_size_bytes=probed,
        source_spec_path=source_spec_path,
        source_files=source_files,
    )
