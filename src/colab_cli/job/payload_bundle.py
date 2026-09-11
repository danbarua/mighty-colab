"""Build and stage the VM payload containing runtime and user code."""

from __future__ import annotations

import gzip
import io
import json
import os
import tarfile
import tempfile
from pathlib import Path

from colab_cli.contents import ContentsClient
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


def _normalise_info(info):
    """Strip local ownership and timestamps for reproducible archive bytes."""
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = 0
    return info


def _add_runtime(archive):
    for path in sorted(_RUNTIME_DIR.rglob("*")):
        if path.is_dir() or path.name == "__pycache__" or path.suffix == ".pyc":
            continue
        relative = path.relative_to(_RUNTIME_DIR)
        archive.add(
            path,
            arcname=Path("mighty_runtime") / relative,
            recursive=False,
            filter=_normalise_info,
        )


def _add_user(archive, source, entry):
    source = Path(source)
    if not source.exists():
        raise FileNotFoundError(source)
    entry = Path(entry)
    if source.is_file():
        archive.add(source, arcname=entry, recursive=False, filter=_normalise_info)
        return

    # A bundle directory is copied as its contents, not as an absolute or
    # host-specific parent directory. This keeps sibling imports and the
    # declared entry path intact after extraction on the VM.
    for path in sorted(source.rglob("*")):
        if (
            path.is_dir()
            or path.name in {".git", "__pycache__", ".venv"}
            or path.suffix == ".pyc"
        ):
            continue
        relative = path.relative_to(source)
        if any(part in {".git", "__pycache__", ".venv"} for part in relative.parts):
            continue
        archive.add(
            path,
            arcname=relative,
            recursive=False,
            filter=_normalise_info,
        )


def build_payload_tarball(spec, entry_src_path) -> bytes:
    """Return a gzip tarball containing runtime and the user's code.

    Runtime files are always rooted at ``mighty_runtime/``. A file payload is
    stored at ``spec.code.entry``; a bundle directory is stored at the bundle
    root so its relative imports remain valid. ``entry_src_path`` is never
    transmitted or interpreted as an archive path.
    """
    entry = _code_value(spec, "entry")
    if not isinstance(entry, str) or not entry or os.path.isabs(entry):
        raise ValueError("spec.code.entry must be a relative path")

    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, mode="wb", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w") as archive:
            _add_runtime(archive)
            _add_user(archive, entry_src_path, entry)
    return output.getvalue()


def _job_attr(item, name, default=None):
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _remote_join(root: str, child: str) -> str:
    return f"{root.rstrip('/')}/{child.lstrip('/')}"


def _upload_checked(client, local_path: Path, remote_path: str) -> None:
    size = local_path.stat().st_size
    if size > CONTENTS_UPLOAD_CEILING:
        raise ValueError(
            f"Refusing upload of {local_path}: {size} bytes exceeds the "
            "250 MB Contents ceiling; use a data URL instead."
        )
    client.upload(str(local_path), remote_path)


def _iter_user_files(spec):
    entry = _code_value(spec, "entry")
    root = _code_value(spec, "root", ".") or "."
    root_path = Path(root)
    kind = _code_value(spec, "kind", "file")
    if kind == "file":
        path = Path(entry)
        if not path.is_absolute():
            path = root_path / path
        if not path.is_file():
            raise FileNotFoundError(f"code entry does not exist: {path}")
        yield path, Path(entry)
        return
    if kind != "bundle":
        raise ValueError(f"unsupported code kind: {kind}")
    if not root_path.is_dir():
        raise FileNotFoundError(f"code bundle root does not exist: {root_path}")
    for path in sorted(root_path.rglob("*")):
        if path.is_dir() or path.suffix == ".pyc":
            continue
        relative = path.relative_to(root_path)
        if any(part in {".git", "__pycache__", ".venv"} for part in relative.parts):
            continue
        yield path, relative


def _write_manifest(client, remote_dir, name, rows):
    if not rows:
        return
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(rows, f)
        f.flush()
        local_path = Path(f.name)
    try:
        _upload_checked(client, local_path, _remote_join(remote_dir, name))
    finally:
        local_path.unlink(missing_ok=True)


def stage_payload(*, spec, job_id: str, session, remote_dir: str) -> None:
    """Upload runtime/code and signed manifests through the Contents API.

    Every upload is checked before calling the API. This prevents the
    Contents chunked PUT path from receiving a payload above its known live
    ceiling; callers should put large inputs behind ``spec.data`` URLs.
    """
    del job_id  # The remote directory already contains the orchestrator ID.

    client = ContentsClient(session)
    runtime_remote = _remote_join(remote_dir, "mighty_runtime")
    for local_path in sorted(_RUNTIME_DIR.rglob("*.py")):
        relative = local_path.relative_to(_RUNTIME_DIR)
        _upload_checked(client, local_path, _remote_join(runtime_remote, str(relative)))

    src_remote = _remote_join(remote_dir, "src")
    for local_path, relative in _iter_user_files(spec):
        _upload_checked(client, local_path, _remote_join(src_remote, str(relative)))

    data_rows = [
        {
            "url": _job_attr(item, "url"),
            "dest": _job_attr(item, "dest"),
            "sha256": _job_attr(item, "sha256"),
            "size_bytes": _job_attr(item, "size_bytes"),
        }
        for item in (_job_attr(spec, "data", []) or [])
    ]
    artifact_rows = [
        {
            "path": _job_attr(item, "path"),
            "url": _job_attr(item, "url"),
            "required": _job_attr(item, "required", True),
        }
        for item in (_job_attr(spec, "artifacts", []) or [])
    ]
    _write_manifest(client, remote_dir, "stage.manifest.json", data_rows)
    _write_manifest(client, remote_dir, "offload.manifest.json", artifact_rows)
