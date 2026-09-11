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
"""I/O and non-secret URL identity helpers for job specifications."""

from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from colab_cli.job.models import JobSpec


SECRET_FRAGMENT_PREFIX = "mighty-colab-secret-sha256="


def is_redacted_url(value: str) -> bool:
    """Return whether value is a query-free signed-URL reference."""

    try:
        parsed = urllib.parse.urlsplit(value)
    except (TypeError, ValueError):
        return False
    digest = parsed.fragment.removeprefix(SECRET_FRAGMENT_PREFIX)
    return (
        not parsed.query
        and parsed.fragment.startswith(SECRET_FRAGMENT_PREFIX)
        and re.fullmatch(r"[0-9a-f]{64}", digest) is not None
    )


def _contains_redacted_url(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_contains_redacted_url(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_redacted_url(item) for item in value)
    return isinstance(value, str) and is_redacted_url(value)

@dataclass(frozen=True)
class ProbeResult:
    """Result of the one-byte ranged GET used during planning.

    ``size_bytes`` is authoritative only when the server returned a valid
    206 ``Content-Range`` (or a 416 for an empty object). A 200 response means
    the server ignored the range and therefore has unknown size.
    """

    status: int | None
    size_bytes: int | None = None
    error: str | None = None
    range_honored: bool = False

    @property
    def size(self) -> int | None:
        """Compatibility alias for callers that use the shorter name."""

        return self.size_bytes

    @property
    def empty(self) -> bool:
        return self.status == 416 and self.error is None

    @property
    def range_ignored(self) -> bool:
        return self.status == 200 and not self.range_honored


def load_spec(path: str | Path) -> JobSpec:
    """Load a source spec; redacted record snapshots are not executable."""

    spec_path = Path(path).expanduser().resolve(strict=False)
    try:
        data = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None)
        location = (
            f" at line {mark.line + 1}, column {mark.column + 1}"
            if mark is not None
            else ""
        )
        raise ValueError(f"invalid YAML{location}") from None
    if not isinstance(data, dict):
        raise ValueError("job spec must contain a mapping at the top level")
    if _contains_redacted_url(data):
        raise ValueError("redacted job record cannot be used as a source spec")
    spec = JobSpec.model_validate(data)
    root = Path(spec.code.root) if spec.code.root else spec_path.parent
    if not root.is_absolute():
        root = spec_path.parent / root
    spec.code.root = str(root.resolve(strict=False))
    return spec


def canonical_url(url: str) -> str:
    """Return URL identity without query or fragment credentials."""

    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        # Keep planner diagnostics usable for malformed user input without
        # echoing a signed query string.
        return url.split("?", 1)[0].split("#", 1)[0]
    # Build from hostname rather than netloc so userinfo cannot leak. Keep
    # an explicit port because it can identify a different endpoint.
    host = parsed.hostname or ""
    try:
        port = parsed.port
    except ValueError:
        port = None
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if port is not None:
        host = f"{host}:{port}"
    return f"{parsed.scheme}://{host}{parsed.path}"


def url_id(url: str) -> str:
    """Return a stable, non-secret URL handle for diagnostics and logs."""

    return f"{canonical_url(url)}#{hashlib.sha256(url.encode('utf-8')).hexdigest()[:12]}"


def has_url_query(url: str) -> bool:
    try:
        return bool(urllib.parse.urlsplit(url).query)
    except ValueError:
        return "?" in url


def redacted_url(url: str) -> str:
    """Return a query-free URL reference bound to the exact credential."""

    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return f"{canonical_url(url)}#{SECRET_FRAGMENT_PREFIX}{digest}"

def _query(url: str) -> dict[str, list[str]]:
    try:
        query = urllib.parse.urlsplit(url).query
    except ValueError:
        return {}
    return urllib.parse.parse_qs(query, keep_blank_values=True)


def _one(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    return values[0] if values else None


def _parse_utc_date(value: str, fmt: str) -> datetime | None:
    try:
        parsed = datetime.strptime(value, fmt)
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=timezone.utc)


def _parse_seconds(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 0 else None


def parse_signed_url_expiry(url: str) -> datetime | None:
    """Parse a known signed-URL expiry as an aware UTC datetime.

    Unknown or malformed signatures intentionally return ``None``. Public URLs
    and URL formats which do not advertise an expiry are valid inputs; they
    cannot be rejected merely because this parser does not recognize them.
    """

    query = _query(url)

    # Google Cloud Storage V4 signing.
    gcs_date = _one(query, "X-Goog-Date")
    gcs_expires = _parse_seconds(_one(query, "X-Goog-Expires"))
    if gcs_date is not None or gcs_expires is not None:
        start = _parse_utc_date(gcs_date, "%Y%m%dT%H%M%SZ") if gcs_date else None
        if start is None or gcs_expires is None:
            return None
        return start + timedelta(seconds=gcs_expires)

    # Google Cloud Storage V2 signing uses an epoch-seconds expiry.
    gcs_v2 = _one(query, "Expires")
    if gcs_v2 is not None:
        try:
            timestamp = int(gcs_v2)
        except (TypeError, ValueError):
            return None
        try:
            return datetime.fromtimestamp(timestamp, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None

    # Amazon S3 Signature Version 4.
    s3_date = _one(query, "X-Amz-Date")
    s3_expires = _parse_seconds(_one(query, "X-Amz-Expires"))
    if s3_date is not None or s3_expires is not None:
        start = _parse_utc_date(s3_date, "%Y%m%dT%H%M%SZ") if s3_date else None
        if start is None or s3_expires is None:
            return None
        return start + timedelta(seconds=s3_expires)

    # Azure SAS uses an ISO-8601 expiry, commonly URL-encoded and ending in
    # Z. ``fromisoformat`` handles offsets and fractional seconds; naive
    # values are interpreted as UTC by the SAS contract.
    azure_expiry = _one(query, "se")
    if azure_expiry is not None:
        try:
            parsed = datetime.fromisoformat(azure_expiry.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).astimezone(timezone.utc)

    return None


def _canonicalize(value: Any, key: str | None = None) -> Any:
    if isinstance(value, dict):
        return {name: _canonicalize(item, name) for name, item in value.items()}
    if isinstance(value, list):
        return [_canonicalize(item, key) for item in value]
    if isinstance(value, str) and key is not None and (key == "url" or key.endswith("_url")):
        return {
            "identity": canonical_url(value),
            "credential_required": has_url_query(value) or is_redacted_url(value),
        }
    return value


def spec_hash(spec: JobSpec) -> str:
    """Hash a spec while excluding volatile signed-URL query parameters."""

    payload = _canonicalize(spec.model_dump(mode="json"))
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

def plan_hash(spec: JobSpec, source_spec_path: str | None, source_files=()) -> str:
    """Bind spec identity, source exclusion path, and source bytes."""
    manifest = [
        {"path": item.path, "size_bytes": item.size_bytes, "sha256": item.sha256}
        for item in source_files
    ]
    encoded = json.dumps(
        {
            "spec": spec_hash(spec),
            "source_spec_path": source_spec_path,
            "source_files": manifest,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _response_status(response: Any) -> int | None:
    status = getattr(response, "status", None)
    if status is not None:
        return int(status)
    getcode = getattr(response, "getcode", None)
    if callable(getcode):
        code = getcode()
        return int(code) if code is not None else None
    return None


def _content_range_size(response: Any) -> int | None:
    header = response.headers.get("Content-Range")
    if not header:
        header = next(
            (value for key, value in response.headers.items() if key.lower() == "content-range"),
            None,
        )
    if not header:
        return None
    match = re.fullmatch(r"\s*bytes\s+0-0/(\d+)\s*", header)
    return int(match.group(1)) if match else None


def probe_get_url(url: str, timeout: float = 10) -> ProbeResult:
    """Probe an input URL with a bounded ranged GET.

    The response status is inspected before touching the body. In particular,
    a server which ignores ``Range`` (200) is closed immediately because
    draining a large object would defeat the purpose of planning.
    """

    request = urllib.request.Request(url, headers={"Range": "bytes=0-0"}, method="GET")
    response: Any | None = None
    try:
        response = urllib.request.urlopen(request, timeout=timeout)
        status = _response_status(response)
        if status == 206:
            size = _content_range_size(response)
            # Consume no more than one byte. This is useful for servers that
            # require a read before reusing/closing the connection.
            response.read(1)
            return ProbeResult(status=206, size_bytes=size, range_honored=True)
        if status == 200:
            return ProbeResult(status=200, size_bytes=None, range_honored=False)
        if status == 416:
            return ProbeResult(status=416, size_bytes=0, range_honored=True)
        return ProbeResult(status=status, error=f"unexpected HTTP status {status}")
    except urllib.error.HTTPError as error:
        # HTTPError is also a response object. Close it without reading any
        # body, including for 403/404/416 responses.
        response = error
        status = int(error.code)
        if status == 416:
            return ProbeResult(status=416, size_bytes=0, range_honored=True)
        if status in {403, 404}:
            return ProbeResult(status=status, error=f"HTTP {status}")
        return ProbeResult(status=status, error=f"HTTP {status}")
    except (OSError, TimeoutError, ValueError) as error:
        return ProbeResult(status=None, error=type(error).__name__)
    finally:
        if response is not None:
            response.close()
