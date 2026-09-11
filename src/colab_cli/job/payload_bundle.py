"""Build and stage the VM payload containing runtime and user code."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
import urllib.parse
from pathlib import Path

import yaml

from colab_cli.job.spec_io import url_id
from colab_cli.job.store import SECRET_SIDECAR_SUFFIX, SECRET_TEMP_PREFIX


def _contains_typed_url_query(value) -> bool:
    if isinstance(value, list):
        return any(_contains_typed_url_query(item) for item in value)
    if not isinstance(value, dict):
        return False
    for key, item in value.items():
        normalized = key.casefold() if isinstance(key, str) else ""
        if (
            (normalized == "url" or normalized.endswith("_url"))
            and isinstance(item, str)
            and urllib.parse.urlsplit(item).scheme.casefold() in {"http", "https"}
            and urllib.parse.urlsplit(item).query
        ):
            return True
        if _contains_typed_url_query(item):
            return True
    return False


def _reject_structured_url_queries(snapshot: Path) -> None:
    try:
        value = yaml.safe_load(snapshot.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError):
        return
    if _contains_typed_url_query(value):
        raise ValueError("code payload contains a credential-bearing URL query")



_RUNTIME_DIR = Path(__file__).with_name("runtime_payload")
CONTENTS_UPLOAD_CEILING = 250 * 1024 * 1024


def _code_value(spec, name, default=None):
    code = getattr(spec, "code", None)
    if code is None and isinstance(spec, dict):
        code = spec.get("code")
    if code is None:
        return default
    if isinstance(code, dict):
        return code.get(name, default)
    return getattr(code, name, default)


def _job_attr(item, name, default=None):
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _remote_join(root: str, child: str) -> str:
    return f"{root.rstrip('/')}/{child.lstrip('/')}"


def _upload_checked(
    client, local_path: Path, remote_path: str, made_dirs: set | None = None
) -> None:
    size = local_path.stat().st_size
    if size > CONTENTS_UPLOAD_CEILING:
        raise ValueError(
            f"Refusing upload of {local_path}: {size} bytes exceeds the "
            "250 MB Contents ceiling; use a data URL instead."
        )
    # The Contents API does not create parents implicitly, and the Colab
    # backend reports the resulting failure as a bare HTTP 500 -- which the
    # client reasonably attributes to the known upload-size limit. A live
    # run hit exactly that: a 222-byte `mighty_runtime/__init__.py`
    # rejected as "too large" because its directory did not exist yet.
    #
    # `made_dirs` is caller-scoped, never module-global: a cache that
    # outlived one staging pass would skip `makedirs` for a *different* VM
    # reached later in the same process, and the skip only shows up as
    # that same misleading 500.
    parent = remote_path.rsplit("/", 1)[0]
    if parent and (made_dirs is None or parent not in made_dirs):
        client.makedirs(parent)
        if made_dirs is not None:
            made_dirs.add(parent)
    client.upload(str(local_path), remote_path)


def _validate_user_file(path: Path, root: Path) -> None:
    if path.is_symlink():
        raise ValueError("code payload must not contain symbolic links")
    try:
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (FileNotFoundError, ValueError):
        raise ValueError("code payload file escapes its declared root") from None
    if path.lstat().st_nlink != 1:
        raise ValueError("code payload must not contain hard-linked files")


_URL_DELIMITERS = frozenset(b" \t\r\n\"'<>(){}")
_SECRET_QUERY_KEYS = frozenset(
    {
        b"access_token",
        b"awsaccesskeyid",
        b"googleaccessid",
        b"sig",
        b"signature",
        b"token",
        b"x-amz-credential",
        b"x-amz-security-token",
        b"x-amz-signature",
        b"x-goog-credential",
        b"x-goog-signature",
    }
)


def _reject_credential_content(path: Path) -> None:
    recent = bytearray()
    in_url = False
    query_key = None
    try:
        source = path.open("rb")
    except OSError:
        return
    with source:
        for block in iter(lambda: source.read(64 * 1024), b""):
            for byte in block:
                lowered = byte + 32 if ord("A") <= byte <= ord("Z") else byte
                if in_url:
                    if byte in _URL_DELIMITERS:
                        in_url = False
                        query_key = None
                        recent.clear()
                    elif byte == ord("?") and query_key is None:
                        query_key = bytearray()
                    elif isinstance(query_key, bytearray):
                        if byte in b"=&;#":
                            if bytes(query_key) in _SECRET_QUERY_KEYS:
                                raise ValueError(
                                    "code payload contains a credential-bearing URL; "
                                    "move transfer URLs into the active job spec"
                                )
                            query_key = bytearray() if byte in b"&;" else False
                            if byte == ord("#"):
                                in_url = False
                        elif len(query_key) <= 64:
                            query_key.append(lowered)
                    elif query_key is False:
                        if byte in b"&;":
                            query_key = bytearray()
                        elif byte == ord("#"):
                            in_url = False
                    continue
                recent.append(lowered)
                if len(recent) > 8:
                    del recent[0]
                if recent.endswith(b"https://") or recent.endswith(b"http://"):
                    in_url = True
                    query_key = None


def _snapshot_user_file(path: Path) -> Path:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise ValueError("code payload changed while it was being staged")
        with tempfile.NamedTemporaryFile(
            mode="wb", delete=False, prefix=".mighty-colab-code-"
        ) as target:
            snapshot = Path(target.name)
            with os.fdopen(fd, "rb", closefd=False) as source:
                shutil.copyfileobj(source, target, length=64 * 1024)
    finally:
        os.close(fd)
    try:
        _reject_credential_content(snapshot)
        _reject_structured_url_queries(snapshot)
    except Exception:
        snapshot.unlink(missing_ok=True)
        raise
    return snapshot

def _iter_user_files(spec, source_spec_path=None):
    entry = _code_value(spec, "entry")
    root = _code_value(spec, "root", ".") or "."
    root_path = Path(root)
    excluded = (
        {Path(source_spec_path).resolve(strict=False)} if source_spec_path else set()
    )
    kind = _code_value(spec, "kind", "file")
    if kind == "file":
        path = Path(entry)
        if not path.is_absolute():
            path = root_path / path
        if not path.is_file():
            raise FileNotFoundError(f"code entry does not exist: {path}")
        if path.resolve(strict=False) in excluded or (
            path.name.endswith(SECRET_SIDECAR_SUFFIX)
            or path.name.startswith(SECRET_TEMP_PREFIX)
        ):
            raise ValueError("code entry is a reserved credential record")
        _validate_user_file(path, root_path)
        yield path, Path(entry)
        return
    if kind != "bundle":
        raise ValueError(f"unsupported code kind: {kind}")
    if not root_path.is_dir():
        raise FileNotFoundError(f"code bundle root does not exist: {root_path}")
    for path in sorted(root_path.rglob("*")):
        relative = path.relative_to(root_path)
        if any(part in {".git", "__pycache__", ".venv"} for part in relative.parts):
            continue
        if path.is_symlink():
            raise ValueError("code payload must not contain symbolic links")
        if path.is_dir() or path.suffix == ".pyc":
            continue
        if path.resolve(strict=False) in excluded or (
            path.name.endswith(SECRET_SIDECAR_SUFFIX)
            or path.name.startswith(SECRET_TEMP_PREFIX)
        ):
            continue
        _validate_user_file(path, root_path)
        yield path, relative


def _hash_user_file(path: Path) -> tuple[int, str]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        digest = hashlib.sha256()
        size = 0
        with os.fdopen(fd, "rb", closefd=False) as source:
            for chunk in iter(lambda: source.read(64 * 1024), b""):
                size += len(chunk)
                digest.update(chunk)
        return size, digest.hexdigest()
    finally:
        os.close(fd)


def collect_source_files(spec, source_spec_path=None):
    """Snapshot the source payload as relative path, size, and SHA-256."""
    from colab_cli.job.models import SourceFileLock

    rows = []
    for local_path, relative in _iter_user_files(spec, source_spec_path):
        size, digest = _hash_user_file(local_path)
        rows.append(
            SourceFileLock(
                path=relative.as_posix(),
                size_bytes=size,
                sha256=digest,
            )
        )
    rows.sort(key=lambda item: item.path)
    return rows


def verify_source_files(spec, source_files, source_spec_path=None) -> None:
    """Refuse apply when disk bytes differ from the accepted plan."""
    if not source_files:
        raise ValueError("plan has no source lock; re-run job plan")
    current = {
        item.path: item for item in collect_source_files(spec, source_spec_path)
    }
    expected = {item.path: item for item in source_files}
    added = sorted(set(current) - set(expected))
    removed = sorted(set(expected) - set(current))
    changed = sorted(
        path
        for path in set(current) & set(expected)
        if (
            current[path].size_bytes != expected[path].size_bytes
            or current[path].sha256 != expected[path].sha256
        )
    )
    if not added and not removed and not changed:
        return
    parts = []
    if added:
        parts.append("added " + ", ".join(added))
    if removed:
        parts.append("removed " + ", ".join(removed))
    if changed:
        parts.append("changed " + ", ".join(changed))
    raise ValueError(
        "source files changed since planning ("
        + "; ".join(parts)
        + "); re-run job plan"
    )



def _write_manifest(client, remote_dir, name, rows, made_dirs=None):
    if not rows:
        return
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(rows, f)
        f.flush()
        local_path = Path(f.name)
    try:
        _upload_checked(
            client, local_path, _remote_join(remote_dir, name), made_dirs
        )
    finally:
        local_path.unlink(missing_ok=True)


def _write_secret(client, remote_path, value, made_dirs=None):
    with tempfile.NamedTemporaryFile(
        mode="w", delete=False, prefix=SECRET_TEMP_PREFIX
    ) as f:
        f.write(value)
        f.flush()
        local_path = Path(f.name)
    try:
        local_path.chmod(0o600)
        _upload_checked(client, local_path, remote_path, made_dirs)
    finally:
        local_path.unlink(missing_ok=True)


def _url_ref(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def stage_payload(
    *,
    spec,
    job_id: str,
    transport,
    remote_dir: str,
    source_spec_path=None,
    source_files=None,
) -> None:
    """Upload public payload files before the owner-only URL channel."""

    del job_id
    client = transport
    made_dirs: set = set()
    runtime_remote = _remote_join(remote_dir, "mighty_runtime")
    for local_path in sorted(_RUNTIME_DIR.rglob("*.py")):
        relative = local_path.relative_to(_RUNTIME_DIR)
        _upload_checked(
            client,
            local_path,
            _remote_join(runtime_remote, str(relative)),
            made_dirs,
        )

    src_remote = _remote_join(remote_dir, "src")
    expected = (
        {item.path: item for item in source_files} if source_files is not None else None
    )
    uploaded = set()
    for local_path, relative in _iter_user_files(spec, source_spec_path):
        key = relative.as_posix()
        if expected is not None and key not in expected:
            raise ValueError(f"undeclared source file: {key}")
        snapshot = _snapshot_user_file(local_path)
        try:
            if expected is not None:
                size, digest = _hash_user_file(snapshot)
                lock = expected[key]
                if size != lock.size_bytes or digest != lock.sha256:
                    raise ValueError(f"source file changed while staging: {key}")
            _upload_checked(
                client, snapshot, _remote_join(src_remote, str(relative)), made_dirs
            )
        finally:
            snapshot.unlink(missing_ok=True)
        uploaded.add(key)
    if expected is not None and uploaded != set(expected):
        raise ValueError("source payload does not match the plan")

    urls = {}
    data_rows = []
    for item in _job_attr(spec, "data", []) or []:
        url = _job_attr(item, "url")
        reference = _url_ref(url)
        urls[reference] = url
        data_rows.append(
            {
                "url_id": url_id(url),
                "url_ref": reference,
                "dest": _job_attr(item, "dest"),
                "sha256": _job_attr(item, "sha256"),
                "size_bytes": _job_attr(item, "size_bytes"),
            }
        )

    artifact_rows = []
    for item in _job_attr(spec, "artifacts", []) or []:
        url = _job_attr(item, "url")
        reference = _url_ref(url)
        urls[reference] = url
        artifact_rows.append(
            {
                "url_id": url_id(url),
                "url_ref": reference,
                "path": _job_attr(item, "path"),
                "required": _job_attr(item, "required", True),
            }
        )

    _write_manifest(client, remote_dir, "stage.manifest.json", data_rows, made_dirs)
    _write_manifest(
        client, remote_dir, "offload.manifest.json", artifact_rows, made_dirs
    )

    control = _job_attr(spec, "control")
    result_channel = _job_attr(control, "result") if control is not None else None
    result_put_url = _job_attr(result_channel, "put_url")
    result_put_ref = None
    if result_put_url:
        result_put_ref = _url_ref(result_put_url)
        urls[result_put_ref] = result_put_url

    if urls:
        secret_dir = _remote_join(runtime_remote, ".secrets")
        made_dirs.add(secret_dir)
        _write_secret(
            client,
            _remote_join(secret_dir, "transfer.json"),
            json.dumps(
                {
                    "schema_version": 1,
                    "urls": urls,
                    "result_put_ref": result_put_ref,
                }
            ),
            made_dirs,
        )


