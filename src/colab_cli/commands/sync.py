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
import posixpath
import shlex
import shutil
import subprocess
import stat
import tarfile
import tempfile
import textwrap
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import typer
from typing_extensions import Annotated

from colab_cli.contents import ContentsClient
from colab_cli.utils import get_status_code, is_terminal_error
from colab_cli.runtime import ColabRuntime

_OFFLINE_ORIGIN = "offline://mighty-colab/sync"


@dataclass(frozen=True)
class SyncArchiveStats:
    source_bytes: int
    compressed_bytes: int
    git_commit: Optional[str] = None


def _normalize_sync_destination(remote_path: str) -> tuple[str, str]:
    """Return Contents API and VM filesystem paths below /content."""
    raw = remote_path.strip()
    if not raw or raw == ".":
        raise ValueError("Remote path must name a destination below /content")
    if raw.startswith("/"):
        filesystem_path = posixpath.normpath(raw)
    elif raw == "content" or raw.startswith("content/"):
        filesystem_path = posixpath.normpath(f"/{raw}")
    else:
        filesystem_path = posixpath.normpath(f"/content/{raw}")
    if filesystem_path == "/content" or not filesystem_path.startswith("/content/"):
        raise ValueError("Remote path must name a destination below /content")
    return filesystem_path.lstrip("/"), filesystem_path


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _validate_payload_symlinks(root: Path, *, exclude_git: bool) -> None:
    root = root.resolve()
    for directory, directories, files in os.walk(root, followlinks=False):
        if exclude_git:
            directories[:] = [name for name in directories if name != ".git"]
            files = [name for name in files if name != ".git"]
        for name in [*directories, *files]:
            path = Path(directory, name)
            if not path.is_symlink():
                continue
            target = os.readlink(path)
            if os.path.isabs(target):
                raise ValueError(f"Symlink '{path}' escapes the payload")
            resolved_target = (path.parent / target).resolve(strict=False)
            try:
                resolved_target.relative_to(root)
            except ValueError as exc:
                raise ValueError(f"Symlink '{path}' escapes the payload") from exc

def _tree_size(root: Path, *, exclude_git: bool) -> int:
    if root.is_file():
        return root.stat().st_size
    total = 0
    for directory, directories, files in os.walk(root, followlinks=False):
        if exclude_git:
            directories[:] = [name for name in directories if name != ".git"]
        for filename in files:
            path = Path(directory, filename)
            if path.is_symlink() or (exclude_git and filename == ".git"):
                continue
            total += path.stat().st_size
    return total


def _git_environment() -> dict[str, str]:
    env = os.environ.copy()
    env.update({"GIT_LFS_SKIP_SMUDGE": "1", "GIT_TERMINAL_PROMPT": "0"})
    return env


def _run_git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=_git_environment(),
    )
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
        command = shlex.join(["git", *args])
        raise RuntimeError(f"{command} failed: {detail}")
    return result.stdout.rstrip("\n")


def _git_selection(source: Path) -> tuple[Path, Path, str]:
    lookup = source if source.is_dir() else source.parent
    repo_root = Path(_run_git(lookup, "rev-parse", "--show-toplevel")).resolve()
    source = source.resolve()
    try:
        relative = source.relative_to(repo_root)
    except ValueError as exc:
        raise ValueError(f"Local path '{source}' is outside its Git worktree") from exc
    commit = _run_git(repo_root, "rev-parse", "HEAD")
    return repo_root, relative, commit


def _git_selector(relative: Path) -> str:
    if relative == Path("."):
        return "."
    return f":(top,literal){relative.as_posix()}"


def _reject_submodules(repo_root: Path, relative: Path) -> None:
    selector = _git_selector(relative)
    stage = _run_git(repo_root, "ls-files", "--stage", "-z", "--", selector)
    for entry in stage.split("\0"):
        if entry.startswith("160000 "):
            path = entry.split("\t", 1)[-1]
            raise ValueError(f"Git-aware sync does not support submodule '{path}'")


def _apply_staged_selection(repo_root: Path, relative: Path, payload: Path) -> None:
    selector = _git_selector(relative)
    diff = subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "diff",
            "--cached",
            "--binary",
            "--full-index",
            "--no-ext-diff",
            "--no-renames",
            "--",
            selector,
        ],
        capture_output=True,
        env=_git_environment(),
    )
    if diff.returncode:
        detail = diff.stderr.decode(errors="replace").strip() or "unknown error"
        raise RuntimeError(f"git diff --cached failed: {detail}")
    if not diff.stdout:
        return
    apply = subprocess.run(
        [
            "git",
            "-C",
            str(payload),
            "apply",
            "--cached",
            "--binary",
            "--whitespace=nowarn",
            "-",
        ],
        input=diff.stdout,
        capture_output=True,
        env=_git_environment(),
    )
    if apply.returncode:
        detail = apply.stderr.decode(errors="replace").strip() or "unknown error"
        raise RuntimeError(f"git apply --cached failed: {detail}")


def _source_index_flags(repo_root: Path, selector: str) -> tuple[list[str], list[str]]:
    entries = _run_git(repo_root, "ls-files", "-v", "-z", "--", selector)
    skip_worktree = []
    assume_unchanged = []
    for entry in filter(None, entries.split("\0")):
        if len(entry) < 3 or entry[1] != " ":
            continue
        if entry[0].lower() == "s":
            skip_worktree.append(entry[2:])
        if entry[0].islower():
            assume_unchanged.append(entry[2:])
    return skip_worktree, assume_unchanged


def _intent_to_add_paths(repo_root: Path, selector: str) -> list[str]:
    status = _run_git(
        repo_root,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--",
        selector,
    )
    return [entry[3:] for entry in status.split("\0") if entry.startswith(" A ")]


def _update_index_flag(payload: Path, flag: str, paths: list[str]) -> None:
    if not paths:
        return
    update = subprocess.run(
        [
            "git",
            "-C",
            str(payload),
            "update-index",
            flag,
            "-z",
            "--stdin",
        ],
        input="\0".join(paths).encode() + b"\0",
        capture_output=True,
        env=_git_environment(),
    )
    if update.returncode:
        detail = update.stderr.decode(errors="replace").strip() or "unknown error"
        raise RuntimeError(f"git update-index {flag} failed: {detail}")


def _restore_intent_to_add(payload: Path, paths: list[str]) -> None:
    if not paths:
        return
    add = subprocess.run(
        [
            "git",
            "--literal-pathspecs",
            "-C",
            str(payload),
            "add",
            "-N",
            "--pathspec-from-file=-",
            "--pathspec-file-nul",
        ],
        input="\0".join(paths).encode() + b"\0",
        capture_output=True,
        env=_git_environment(),
    )
    if add.returncode:
        detail = add.stderr.decode(errors="replace").strip() or "unknown error"
        raise RuntimeError(f"git add -N failed: {detail}")


def _copy_git_selection(repo_root: Path, relative: Path, payload: Path) -> None:
    selector = _git_selector(relative)
    selected = _run_git(
        repo_root,
        "ls-files",
        "-z",
        "--cached",
        "--others",
        "--exclude-standard",
        "--",
        selector,
    )
    skip_worktree, assume_unchanged = _source_index_flags(repo_root, selector)
    intent_to_add = _intent_to_add_paths(repo_root, selector)

    target = payload if relative == Path(".") else payload / relative
    if relative == Path("."):
        for child in payload.iterdir():
            if child.name != ".git":
                _remove_path(child)
    else:
        _remove_path(target)
        if (repo_root / relative).is_dir():
            target.mkdir(parents=True, exist_ok=True)

    for name in filter(None, selected.split("\0")):
        source_path = repo_root / name
        if not os.path.lexists(source_path):
            continue
        destination = payload / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source_path.is_symlink():
            destination.symlink_to(os.readlink(source_path))
        elif source_path.is_file():
            shutil.copy2(source_path, destination)
        elif source_path.is_dir():
            raise ValueError(
                f"Git-aware sync does not support embedded Git repository '{name}'"
            )

    indexed = set(
        filter(
            None,
            _run_git(payload, "ls-files", "-z", "--", selector).split("\0"),
        )
    )
    skipped = [path for path in skip_worktree if path in indexed]
    skipped_set = set(skipped)
    visible = [path for path in indexed if path not in skipped_set]
    _update_index_flag(payload, "--no-skip-worktree", visible)
    _update_index_flag(payload, "--skip-worktree", skipped)
    _restore_intent_to_add(payload, intent_to_add)
    _update_index_flag(payload, "--assume-unchanged", assume_unchanged)


def _create_git_payload(source: Path, payload: Path) -> str:
    repo_root, relative, commit = _git_selection(source)
    source_file_mode = _run_git(repo_root, "config", "--bool", "core.fileMode")
    _reject_submodules(repo_root, relative)
    clone = subprocess.run(
        [
            "git",
            "-c",
            "protocol.file.allow=always",
            "clone",
            "--quiet",
            "--depth",
            "1",
            "--filter=blob:none",
            "--no-checkout",
            "--upload-pack=git -c uploadpack.allowFilter=true upload-pack",
            repo_root.as_uri(),
            str(payload),
        ],
        capture_output=True,
        text=True,
        env=_git_environment(),
    )
    if clone.returncode or "filtering not recognized by server" in clone.stderr:
        detail = clone.stderr.strip() or clone.stdout.strip() or "unknown error"
        raise RuntimeError(f"git clone with blob filtering failed: {detail}")
    _run_git(payload, "config", "core.fileMode", source_file_mode)

    _run_git(payload, "reset", "--mixed", "--quiet", commit)
    all_indexed = list(
        filter(None, _run_git(payload, "ls-files", "-z").split("\0"))
    )
    _update_index_flag(payload, "--skip-worktree", all_indexed)
    selector = _git_selector(relative)
    source_skipped, _ = _source_index_flags(repo_root, selector)
    source_skipped_set = set(source_skipped)
    selected_head = list(
        filter(
            None,
            _run_git(payload, "ls-files", "-z", "--", selector).split("\0"),
        )
    )
    _update_index_flag(
        payload,
        "--no-skip-worktree",
        [path for path in selected_head if path not in source_skipped_set],
    )
    _apply_staged_selection(repo_root, relative, payload)
    _run_git(payload, "remote", "set-url", "origin", _OFFLINE_ORIGIN)
    reflogs = payload / ".git" / "logs"
    if reflogs.exists():
        shutil.rmtree(reflogs)
    _copy_git_selection(repo_root, relative, payload)
    _validate_payload_symlinks(payload, exclude_git=False)
    return commit


def _copy_snapshot_metadata(target: Path, source_stat: os.stat_result) -> None:
    os.chmod(target, stat.S_IMODE(source_stat.st_mode), follow_symlinks=False)
    os.utime(
        target,
        ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns),
        follow_symlinks=False,
    )


def _verify_opened_entry(
    name: str, listed: os.stat_result, opened: os.stat_result
) -> None:
    if (listed.st_dev, listed.st_ino, listed.st_mode) != (
        opened.st_dev,
        opened.st_ino,
        opened.st_mode,
    ):
        raise RuntimeError(f"Local path changed while sync was snapshotting: {name}")


def _copy_snapshot_directory(source_fd: int, target: Path, *, exclude_git: bool) -> None:
    for name in os.listdir(source_fd):
        if exclude_git and name == ".git":
            continue
        listed = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
        destination = target / name
        if stat.S_ISLNK(listed.st_mode):
            link_target = os.readlink(name, dir_fd=source_fd)
            verified = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
            _verify_opened_entry(name, listed, verified)
            destination.symlink_to(link_target)
        elif stat.S_ISDIR(listed.st_mode):
            child_fd = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=source_fd,
            )
            try:
                _verify_opened_entry(name, listed, os.fstat(child_fd))
                destination.mkdir(mode=0o700)
                _copy_snapshot_directory(
                    child_fd, destination, exclude_git=exclude_git
                )
            finally:
                os.close(child_fd)
            _copy_snapshot_metadata(destination, listed)
        elif stat.S_ISREG(listed.st_mode):
            file_fd = os.open(
                name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=source_fd
            )
            _verify_opened_entry(name, listed, os.fstat(file_fd))
            with os.fdopen(file_fd, "rb") as source_file:
                with destination.open("xb") as destination_file:
                    shutil.copyfileobj(source_file, destination_file)
            _copy_snapshot_metadata(destination, listed)
        else:
            raise ValueError(f"Unsupported local file type in sync payload: {name}")

def _copy_payload_snapshot(source: Path, target: Path, *, exclude_git: bool) -> None:
    listed = os.stat(source, follow_symlinks=False)
    if stat.S_ISDIR(listed.st_mode):
        source_fd = os.open(
            source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        try:
            _verify_opened_entry(str(source), listed, os.fstat(source_fd))
            target.mkdir(mode=0o700)
            _copy_snapshot_directory(source_fd, target, exclude_git=exclude_git)
        finally:
            os.close(source_fd)
        _copy_snapshot_metadata(target, listed)
    elif stat.S_ISREG(listed.st_mode):
        source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
        _verify_opened_entry(str(source), listed, os.fstat(source_fd))
        with os.fdopen(source_fd, "rb") as source_file:
            with target.open("xb") as destination_file:
                shutil.copyfileobj(source_file, destination_file)
        _copy_snapshot_metadata(target, listed)
    else:
        raise ValueError(f"Unsupported local file type for sync: {source}")


def _create_sync_archive(
    source: Path, archive_path: Path, *, git_aware: bool
) -> SyncArchiveStats:
    source = source.expanduser()
    if not os.path.lexists(source):
        raise FileNotFoundError(f"Local path '{source}' not found")
    if source.is_symlink():
        raise ValueError("The sync source itself cannot be a symlink")
    source = source.resolve()
    if not git_aware and ".git" in source.parts:
        raise ValueError("Default sync does not accept a Git metadata path")
    archive_path.parent.mkdir(parents=True, exist_ok=True)

    git_commit = None
    with tempfile.TemporaryDirectory(prefix="mighty-colab-sync-payload-") as temp_dir:
        payload = Path(temp_dir, "payload")
        if git_aware:
            git_commit = _create_git_payload(source, payload)
            resolve_root = payload
        else:
            _validate_payload_symlinks(source, exclude_git=True)
            _copy_payload_snapshot(source, payload, exclude_git=True)
            resolve_root = source
        source_bytes = _tree_size(payload, exclude_git=False)
        archive_source = payload
        archive_root = resolve_root.resolve()

        def archive_filter(member: tarfile.TarInfo) -> Optional[tarfile.TarInfo]:
            if member.issym():
                member_path = Path(member.name)
                relative = member_path.relative_to("payload")
                resolved_target = (
                    (resolve_root / relative).parent
                    / os.readlink(archive_source / relative)
                ).resolve(strict=False)
                try:
                    target_in_payload = Path("payload") / resolved_target.relative_to(
                        archive_root
                    )
                except ValueError as exc:
                    raise ValueError(
                        f"Symlink '{archive_source / relative}' escapes the payload"
                    ) from exc
                member.linkname = os.path.relpath(
                    target_in_payload, member_path.parent
                )
            return member

        with tarfile.open(archive_path, "w:gz") as archive:
            archive.add(archive_source, arcname="payload", filter=archive_filter)

    return SyncArchiveStats(
        source_bytes=source_bytes,
        compressed_bytes=archive_path.stat().st_size,
        git_commit=git_commit,
    )


def _build_sync_extract_script(
    archive_path: str,
    destination: str,
    *,
    operation_id: str,
    content_root: str = "/content",
) -> str:
    """Build remote Python that safely installs one /content target."""
    return textwrap.dedent(
        f"""
        import ctypes
        import errno
        import fcntl
        import os
        import posixpath
        import shutil
        import stat
        import tarfile

        archive_path = {archive_path!r}
        destination = {destination!r}
        operation_id = {operation_id!r}
        content_root = os.path.abspath({content_root!r})
        root_fd = None
        parent_fd = None
        stage_fd = None
        stage_name = ".mighty-colab-sync-" + operation_id + ".stage"
        archive_name = os.path.basename(archive_path)
        committed = False
        failure = None
        cleanup_warnings = []

        def fd_path(fd):
            if os.path.exists("/proc/self/fd"):
                return os.path.join("/proc/self/fd", str(fd))
            raw = fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024))
            return os.fsdecode(raw.split(bytes(1), 1)[0])

        def open_directory(name, directory_fd=None):
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            if directory_fd is None:
                return os.open(name, flags)
            return os.open(name, flags, dir_fd=directory_fd)

        def assert_fd_under_content(fd):
            relative = os.path.relpath(
                os.path.realpath(fd_path(fd)), content_root
            )
            if relative == ".." or relative.startswith(".." + os.sep):
                raise ValueError("sync path moved outside /content")

        def entry_exists(directory_fd, name):
            try:
                os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                return False
            return True

        def remove_entry(directory_fd, name):
            try:
                info = os.stat(
                    name, dir_fd=directory_fd, follow_symlinks=False
                )
            except FileNotFoundError:
                return
            if stat.S_ISDIR(info.st_mode):
                shutil.rmtree(os.path.join(fd_path(directory_fd), name))
            else:
                os.unlink(name, dir_fd=directory_fd)

        def exchange_entries(source_fd, source_name, target_fd, target_name):
            libc = ctypes.CDLL(None, use_errno=True)
            renameat2 = getattr(libc, "renameat2", None)
            if renameat2 is not None:
                renameat2.argtypes = [
                    ctypes.c_int,
                    ctypes.c_char_p,
                    ctypes.c_int,
                    ctypes.c_char_p,
                    ctypes.c_uint,
                ]
                renameat2.restype = ctypes.c_int
                result = renameat2(
                    source_fd,
                    os.fsencode(source_name),
                    target_fd,
                    os.fsencode(target_name),
                    2,
                )
            else:
                renamex = getattr(libc, "renamex_np", None)
                if renamex is None:
                    raise OSError(
                        errno.ENOSYS, "atomic path exchange is unavailable"
                    )
                renamex.argtypes = [
                    ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint
                ]
                renamex.restype = ctypes.c_int
                source = os.path.join(fd_path(source_fd), source_name)
                target = os.path.join(fd_path(target_fd), target_name)
                result = renamex(os.fsencode(source), os.fsencode(target), 2)
            if result:
                error = ctypes.get_errno()
                raise OSError(error, os.strerror(error), target_name)

        def validate_payload_links(payload):
            payload_root = os.path.realpath(payload)
            if os.path.islink(payload):
                raise ValueError("sync payload root cannot be a symlink")
            for directory, directories, files in os.walk(
                payload, followlinks=False
            ):
                for name in [*directories, *files]:
                    path = os.path.join(directory, name)
                    if not os.path.islink(path):
                        continue
                    target = os.readlink(path)
                    resolved = os.path.realpath(path)
                    if os.path.isabs(target) or os.path.commonpath(
                        [payload_root, resolved]
                    ) != payload_root:
                        raise ValueError(
                            "sync payload symlink escapes payload: " + path
                        )

        try:
            destination_path = os.path.abspath(destination)
            destination_relative = os.path.relpath(
                destination_path, content_root
            )
            if destination_relative == ".." or destination_relative.startswith(
                ".." + os.sep
            ):
                raise ValueError("sync destination resolves outside /content")
            parts = destination_relative.split(os.sep)
            basename = parts[-1]
            if basename in ("", ".", ".."):
                raise ValueError("sync destination resolves outside /content")

            archive_path = os.path.abspath(archive_path)
            archive_relative = os.path.relpath(archive_path, content_root)
            if os.path.dirname(archive_relative) not in ("", "."):
                raise ValueError("sync archive must be directly under /content")

            root_fd = open_directory(content_root)
            parent_fd = os.dup(root_fd)
            for part in parts[:-1]:
                try:
                    next_fd = open_directory(part, parent_fd)
                except FileNotFoundError:
                    try:
                        os.mkdir(part, 0o755, dir_fd=parent_fd)
                    except FileExistsError:
                        pass
                    next_fd = open_directory(part, parent_fd)
                except OSError as exc:
                    if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                        raise ValueError(
                            "sync destination resolves outside /content"
                        ) from exc
                    raise
                os.close(parent_fd)
                parent_fd = next_fd

            remove_entry(parent_fd, stage_name)
            os.mkdir(stage_name, 0o700, dir_fd=parent_fd)
            stage_fd = open_directory(stage_name, parent_fd)
            assert_fd_under_content(parent_fd)
            assert_fd_under_content(stage_fd)
            stage_path = fd_path(stage_fd)

            archive_fd = os.open(
                archive_name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=root_fd
            )
            with os.fdopen(archive_fd, "rb") as archive_file:
                with tarfile.open(
                    fileobj=archive_file, mode="r:gz"
                ) as payload_archive:
                    members = payload_archive.getmembers()
                    for member in members:
                        normalized = posixpath.normpath(member.name)
                        if member.name.startswith("/") or not (
                            normalized == "payload"
                            or normalized.startswith("payload/")
                        ):
                            raise ValueError(
                                "sync archive member is outside payload/: "
                                + member.name
                            )
                    payload_archive.extractall(
                        stage_path, members=members, filter="data"
                    )
            payload = os.path.join(stage_path, "payload")
            if not os.path.lexists(payload):
                raise RuntimeError("sync archive has no payload root")
            validate_payload_links(payload)
            assert_fd_under_content(parent_fd)
            if entry_exists(parent_fd, basename):
                exchange_entries(stage_fd, "payload", parent_fd, basename)
            else:
                os.rename(
                    "payload",
                    basename,
                    src_dir_fd=stage_fd,
                    dst_dir_fd=parent_fd,
                )
            committed = True
        except BaseException as exc:
            failure = exc
        finally:
            cleanup_items = [
                ("close", stage_fd),
                ("entry", (parent_fd, stage_name)),
                ("unlink", (root_fd, archive_name)),
                ("close", parent_fd),
                ("close", root_fd),
            ]
            for kind, value in cleanup_items:
                if value is None:
                    continue
                try:
                    if kind == "close":
                        os.close(value)
                    elif kind == "entry":
                        directory_fd, name = value
                        if directory_fd is not None:
                            remove_entry(directory_fd, name)
                    else:
                        directory_fd, name = value
                        if directory_fd is not None:
                            try:
                                os.unlink(name, dir_fd=directory_fd)
                            except FileNotFoundError:
                                pass
                except BaseException as cleanup_error:
                    if committed and isinstance(cleanup_error, Exception):
                        cleanup_warnings.append(
                            type(cleanup_error).__name__
                            + ": "
                            + str(cleanup_error)
                        )
                    elif failure is None:
                        failure = cleanup_error
        if failure is not None:
            raise failure
        for warning in cleanup_warnings:
            print("MIGHTY_COLAB_SYNC_WARNING:" + warning)
        print("MIGHTY_COLAB_SYNC_OK:" + operation_id)
        """
    ).strip()


def _remote_error(outputs: list[dict]) -> Optional[str]:
    for output in outputs:
        if output.get("output_type") == "error":
            name = output.get("ename", "Error")
            value = output.get("evalue", "Unknown error")
            return f"{name}: {value}"
    return None


def _remote_stream_lines(outputs: list[dict]) -> list[str]:
    lines = []
    for output in outputs:
        if output.get("output_type") != "stream":
            continue
        text = output.get("text", "")
        if isinstance(text, list):
            text = "".join(text)
        lines.extend(str(text).splitlines())
    return lines

def sync(
    session: Annotated[
        Optional[str], typer.Option("-s", "--session", help="Session name")
    ] = None,
    local_path: Annotated[
        str, typer.Argument(help="Local file or directory to synchronize")
    ] = ...,
    remote_path: Annotated[
        str, typer.Argument(help="Destination below the VM's /content directory")
    ] = ...,
    git_aware: Annotated[
        bool,
        typer.Option(
            "--git-aware",
            help="Include sparse Git metadata, HEAD, and selected working-tree status",
        ),
    ] = False,
    timeout: Annotated[
        int,
        typer.Option(
            "--timeout",
            min=1,
            help="Per-upload-request and remote extraction timeout in seconds",
        ),
    ] = 600,
):
    """Synchronize a gzip-compressed local payload to a session."""
    from colab_cli.common import state

    source = Path(local_path).expanduser()
    if not os.path.lexists(source):
        typer.echo(f"[colab] Local path '{local_path}' not found.", err=True)
        raise typer.Exit(1)
    try:
        remote_api_path, remote_filesystem_path = _normalize_sync_destination(
            remote_path
        )
    except ValueError as exc:
        typer.echo(f"[colab] Sync failed: {exc}", err=True)
        raise typer.Exit(1)

    name = state.resolve_session(session)
    session_state = state.store.get(name)
    if not session_state:
        typer.echo(f"[colab] Session '{name}' not found.", err=True)
        raise typer.Exit(1)

    contents = ContentsClient(session_state)
    operation_id = uuid.uuid4().hex
    staging_api_path = f"content/.mighty-colab-sync-{operation_id}.tar.gz"
    runtime = None
    upload_in_progress = False
    terminal_reconciled = False
    try:
        with tempfile.TemporaryDirectory(prefix="mighty-colab-sync-") as temp_dir:
            archive_path = Path(temp_dir, "payload.tar.gz")
            stats = _create_sync_archive(
                source, archive_path, git_aware=git_aware
            )
            upload_in_progress = True
            contents.upload(
                str(archive_path), staging_api_path, timeout=(10, timeout)
            )
            upload_in_progress = False

        def on_kernel_started(kernel_id):
            session_state.kernel_id = kernel_id
            state.store.add(session_state)

        def on_session_started(session_id):
            session_state.session_id = session_id
            state.store.add(session_state)

        runtime = ColabRuntime(
            session_state.url,
            session_state.token,
            kernel_id=session_state.kernel_id,
            session_id=session_state.session_id,
            on_kernel_started=on_kernel_started,
            on_session_started=on_session_started,
        )
        outputs = runtime.execute_code(
            _build_sync_extract_script(
                f"/{staging_api_path}",
                remote_filesystem_path,
                operation_id=operation_id,
            ),
            timeout=timeout,
        )
        if error := _remote_error(outputs):
            raise RuntimeError(error)
        stream_lines = _remote_stream_lines(outputs)
        completion = f"MIGHTY_COLAB_SYNC_OK:{operation_id}"
        if completion not in stream_lines:
            raise RuntimeError("remote extraction did not confirm completion")
        for line in stream_lines:
            if line.startswith("MIGHTY_COLAB_SYNC_WARNING:"):
                typer.echo(f"[colab] Warning: {line.split(':', 1)[1]}", err=True)
        state.history.log_event(
            name,
            "file_operation",
            {
                "op": "sync",
                "local": str(source),
                "remote": remote_api_path,
                "git_aware": git_aware,
                "source_bytes": stats.source_bytes,
                "compressed_bytes": stats.compressed_bytes,
                "git_commit": stats.git_commit,
            },
        )
        typer.echo(
            f"[colab] Synced '{local_path}' to '{remote_api_path}' "
            f"({stats.source_bytes} -> {stats.compressed_bytes} bytes gzip)"
        )
    except Exception as exc:
        if is_terminal_error(exc) or (
            upload_in_progress and isinstance(exc, FileNotFoundError)
        ):
            from colab_cli.commands.execution import _handle_terminal_session_error

            _handle_terminal_session_error(name)
            terminal_reconciled = True
        else:
            typer.echo(f"[colab] Sync failed: {exc}", err=True)
        raise typer.Exit(1)
    finally:
        if runtime is not None:
            runtime.stop()
        if terminal_reconciled:
            refreshed_session = state.store.get(name)
            if refreshed_session is not None:
                contents = ContentsClient(refreshed_session)
        try:
            contents.rm(staging_api_path, timeout=(10, timeout))
        except Exception as cleanup_error:
            if not terminal_reconciled and get_status_code(cleanup_error) == 401:
                from colab_cli.commands.execution import (
                    _handle_terminal_session_error,
                )

                _handle_terminal_session_error(name)
                terminal_reconciled = True


def register(app: typer.Typer):
    app.command()(sync)
