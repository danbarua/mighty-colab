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

import ipaddress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator
from urllib.parse import urlsplit

from colab_cli.job.models import Diagnostic, JobSpec, Plan, RetryClass
from colab_cli.job.spec_io import (
    parse_signed_url_expiry,
    probe_get_url,
    spec_hash,
    url_id,
)
# These are the accelerator names accepted by the upstream Colab assignment
# API.  Unknown names must not silently turn into A100.
KNOWN_ACCELERATORS = frozenset({"T4", "L4", "G4", "H100", "A100", "v5e1", "v6e1"})

# Stable diagnostic identifiers.  Keep these as constants so command output,
# tests, and future clients do not duplicate string literals.
ACCELERATOR_UNKNOWN = "accelerator_unknown"
URL_SCHEME_NOT_HTTPS = "url_scheme_not_https"
URL_HOST_NOT_PUBLIC = "url_host_not_public"
DESTINATION_OUTSIDE_JOB_DIR = "destination_outside_job_dir"
RESERVED_PATH = "reserved_path"
DUPLICATE_DESTINATION = "duplicate_destination"
DATA_DEST_COLLIDES_WITH_ARTIFACT = "data_dest_collides_with_artifact"
RETRY_NOT_IMPLEMENTED = "retry_not_implemented"
CODE_ENTRY_MISSING = "code_entry_missing"
ENTRY_NOT_UNDER_BUNDLE = "entry_not_under_bundle"
URL_EXPIRY_TOO_SOON = "url_expiry_too_soon"
RANGED_GET_FAILED = "ranged_get_failed"
ARTIFACT_SIZE_UNKNOWN = "artifact_size_unknown"
RANGED_GET_IGNORED_RANGE = "ranged_get_ignored_range"
DATA_SIZE_UNKNOWN = "data_size_unknown"

DIAGNOSTIC_CODES = frozenset(
    {
        ACCELERATOR_UNKNOWN,
        URL_SCHEME_NOT_HTTPS,
        URL_HOST_NOT_PUBLIC,
        DESTINATION_OUTSIDE_JOB_DIR,
        RESERVED_PATH,
        DUPLICATE_DESTINATION,
        DATA_DEST_COLLIDES_WITH_ARTIFACT,
        CODE_ENTRY_MISSING,
        ENTRY_NOT_UNDER_BUNDLE,
        URL_EXPIRY_TOO_SOON,
        RANGED_GET_FAILED,
        ARTIFACT_SIZE_UNKNOWN,
        RANGED_GET_IGNORED_RANGE,
        DATA_SIZE_UNKNOWN,
    }
)

# Files and directories owned by the launcher/supervisor.  A consumer must not
# be able to overwrite one of these records by choosing a destination path.
_RESERVED_FILES = frozenset(
    {"launch.json", "result.json", "exception.json", "watchdog.json", "cancel.json"}
)
_RESERVED_DIRECTORIES = frozenset({"mighty_runtime"})

# The extra fifteen minutes covers staging/offload and the handoff between
# attempts.  Control URLs must cover the complete retry budget instead.
_URL_SLACK_SECONDS = 900


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


def _url_is_private(url: str) -> bool:
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return False
    if not host:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(
        address in network
        for network in (
            ipaddress.ip_network("169.254.0.0/16"),
            ipaddress.ip_network("10.0.0.0/8"),
            ipaddress.ip_network("172.16.0.0/12"),
            ipaddress.ip_network("192.168.0.0/16"),
        )
    )


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


def _expiry_diagnostics(
    spec: JobSpec,
    now: datetime,
) -> tuple[list[Diagnostic], dict[str, str | None]]:
    diagnostics: list[Diagnostic] = []
    expiry_map: dict[str, str | None] = {}
    data_deadline = now + timedelta(seconds=spec.budgets.wall_clock + _URL_SLACK_SECONDS)
    control_deadline = now + timedelta(seconds=spec.retry.budget_seconds + _URL_SLACK_SECONDS)

    for field, url, purpose in _url_items(spec):
        expiry = parse_signed_url_expiry(url)
        expiry_map[field] = expiry.isoformat() if expiry is not None else None
        if expiry is None:
            continue
        deadline = control_deadline if purpose == "control" else data_deadline
        if expiry < deadline:
            diagnostics.append(
                _diagnostic(
                    "error",
                    URL_EXPIRY_TOO_SOON,
                    f"{field} ({url_id(url)}) expires before the job deadline",
                    RetryClass.REFRESH_URLS,
                    f"Re-sign {field} so it remains valid through the required budget.",
                )
            )
    return diagnostics, expiry_map


def _url_diagnostics(spec: JobSpec) -> list[Diagnostic]:
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
        if _url_is_private(url):
            diagnostics.append(
                _diagnostic(
                    "error",
                    URL_HOST_NOT_PUBLIC,
                    f"{field} targets a link-local or private address",
                    RetryClass.FIX_HUMAN,
                    "Use a public storage endpoint; private and link-local hosts are blocked.",
                )
            )
    return diagnostics


def _probe_diagnostics(spec: JobSpec, enabled: bool) -> list[Diagnostic]:
    if not enabled:
        return []
    diagnostics: list[Diagnostic] = []
    for index, item in enumerate(spec.data):
        # Never probe a URL that already fails the scheme or SSRF gate.
        # Apart from avoiding a pointless request, this keeps the guard from
        # becoming an SSRF oracle.
        try:
            safe_scheme = urlsplit(item.url).scheme.lower()
        except ValueError:
            safe_scheme = ""
        if safe_scheme != "https" or _url_is_private(item.url):
            continue
        result = probe_get_url(item.url)
        field = f"data[{index}].url"
        if result.status in {403, 404}:
            diagnostics.append(
                _diagnostic(
                    "error",
                    RANGED_GET_FAILED,
                    f"{field} ({url_id(item.url)}) returned HTTP {result.status}",
                    RetryClass.FIX_HUMAN,
                    "Refresh credentials and provide a GET-signed URL for an existing source object.",
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
    return diagnostics

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


def _shortfall(expiry: datetime, now: datetime) -> str:
    seconds = int((expiry - now).total_seconds())
    if seconds <= 0:
        return "already expired"
    minutes, remainder = divmod(seconds, 60)
    if minutes:
        return f"{minutes}m"
    return f"{remainder}s"


def revalidate_expiry(plan: Plan) -> list[str]:
    """Return durable-plan URL expiry failures immediately before assignment."""

    now = datetime.now(timezone.utc)
    control_deadline = now + timedelta(
        seconds=plan.spec.retry.budget_seconds + _URL_SLACK_SECONDS
    )
    data_deadline = now + timedelta(
        seconds=plan.spec.budgets.wall_clock + _URL_SLACK_SECONDS
    )
    expired: list[str] = []
    for field, url, purpose in _url_items(plan.spec):
        expiry = parse_signed_url_expiry(url)
        if expiry is None:
            # A public/unknown URL is not an expiry failure. A persisted
            # parseable value is useful only if the current URL parser cannot
            # recover it (for example, a legacy plan representation).
            stored = plan.url_expiry.get(field)
            if stored is None:
                continue
            try:
                expiry = datetime.fromisoformat(stored)
            except (TypeError, ValueError):
                continue
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        deadline = control_deadline if purpose == "control" else data_deadline
        if expiry < deadline:
            window_seconds = int((deadline - now).total_seconds())
            window_minutes = max(0, (window_seconds + 59) // 60)
            expired.append(
                f"{field} {url_id(url)} expires in {_shortfall(expiry, now)} "
                f"but the job's budget is {window_minutes}m"
            )
    return expired


def build_plan(spec: JobSpec, job_id: str, probe: bool = True) -> Plan:
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

    # `max_attempts` is accepted by the model but `apply` runs exactly one
    # attempt. Surfacing that here is the only free place to discover it:
    # otherwise a spec asking for 3 attempts silently gets 1, and the
    # caller learns only by not seeing a retry that never comes.
    if spec.retry.max_attempts > 1:
        diagnostics.append(
            _diagnostic(
                "warn",
                RETRY_NOT_IMPLEMENTED,
                f"retry.max_attempts={spec.retry.max_attempts} but `apply` "
                "runs exactly one attempt; retry is not implemented yet",
                RetryClass.DO_NOT_RETRY,
                "Remove retry.max_attempts, or re-run `job apply` yourself "
                "after inspecting the envelope's retry_class.",
            )
        )

    diagnostics.extend(_url_diagnostics(spec))
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
    expiry_diagnostics, expiry_map = _expiry_diagnostics(spec, now)
    diagnostics.extend(expiry_diagnostics)

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

    if spec.data and any(item.size_bytes is None for item in spec.data):
        diagnostics.append(
            _diagnostic(
                "warn",
                DATA_SIZE_UNKNOWN,
                "sum(data[].size_bytes) is unknown",
                RetryClass.RETRY_SAME,
                "Declare size_bytes for every data item to enable disk-space planning.",
            )
        )

    diagnostics.extend(_probe_diagnostics(spec, probe))

    return Plan(
        job_id=job_id,
        spec_hash=spec_hash(spec),
        created_at=now.isoformat(),
        spec=spec,
        diagnostics=diagnostics,
        url_expiry=expiry_map,
    )
