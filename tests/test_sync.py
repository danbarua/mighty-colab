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
import subprocess
import shutil
import tarfile
from pathlib import Path
import requests
import pytest
from typer.testing import CliRunner

from colab_cli.cli import app
from colab_cli.commands.sync import (
    _build_sync_extract_script,
    _create_sync_archive,
    _normalize_sync_destination,
)
from colab_cli.state import SessionState

runner = CliRunner()


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.rstrip("\n")


def _init_repo(repo: Path) -> str:
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "sync-test@example.com")
    _git(repo, "config", "user.name", "Sync Test")
    (repo / "experiment").mkdir()
    (repo / "experiment" / "modified.py").write_text("value = 1\n")
    (repo / "experiment" / "deleted.py").write_text("delete = True\n")
    (repo / "experiment" / "staged.py").write_text("staged = 1\n")
    (repo / "experiment" / "staged_deleted.py").write_text("delete = True\n")
    (repo / "experiment" / "both.py").write_text("both = 1\n")
    (repo / "outside.bin").write_bytes(os.urandom(2_000_000))
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "fixture")
    return _git(repo, "rev-parse", "HEAD")


def test_normalize_sync_destination_stays_under_content():
    assert _normalize_sync_destination("project") == (
        "content/project",
        "/content/project",
    )
    assert _normalize_sync_destination("content/project") == (
        "content/project",
        "/content/project",
    )
    assert _normalize_sync_destination("/content/project") == (
        "content/project",
        "/content/project",
    )


@pytest.mark.parametrize("remote", ["", ".", "content", "/content", "../etc"])
def test_normalize_sync_destination_rejects_unsafe_targets(remote):
    with pytest.raises(ValueError):
        _normalize_sync_destination(remote)


def test_create_sync_archive_gzips_directory_and_excludes_git(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "payload.txt").write_text("compress me\n" * 10_000)
    (source / ".git").mkdir()
    (source / ".git" / "config").write_text("must not ship\n")
    archive = tmp_path / "payload.tar.gz"

    stats = _create_sync_archive(source, archive, git_aware=False)

    assert archive.read_bytes().startswith(b"\x1f\x8b")
    assert stats.source_bytes == (source / "payload.txt").stat().st_size
    assert stats.compressed_bytes == archive.stat().st_size
    assert stats.compressed_bytes < stats.source_bytes
    assert stats.git_commit is None
    with tarfile.open(archive, "r:gz") as payload:
        names = payload.getnames()
        assert "payload/payload.txt" in names
        assert not any(".git" in Path(name).parts for name in names)


def test_create_sync_archive_rejects_escaping_symlink(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (tmp_path / "outside.txt").write_text("outside\n")
    (source / "escape").symlink_to("../outside.txt")

    with pytest.raises(ValueError, match="escapes the payload"):
        _create_sync_archive(source, tmp_path / "payload.tar.gz", git_aware=False)


def test_sync_extract_script_replaces_destination_without_stale_files(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "fresh.txt").write_text("fresh\n")
    archive = tmp_path / "payload.tar.gz"
    _create_sync_archive(source, archive, git_aware=False)

    destination = tmp_path / "destination"
    destination.mkdir()
    (destination / "stale.txt").write_text("stale\n")

    exec(
        _build_sync_extract_script(
            str(archive),
            str(destination),
            operation_id="known",
            content_root=str(tmp_path),
        ),
        {},
    )

    assert (destination / "fresh.txt").read_text() == "fresh\n"
    assert not (destination / "stale.txt").exists()
    assert not archive.exists()
    assert not list(tmp_path.glob(".mighty-colab-sync-*"))


def test_git_aware_archive_preserves_sparse_identity_and_status(tmp_path):
    source_repo = tmp_path / "repo"
    commit = _init_repo(source_repo)
    selected = source_repo / "experiment"
    (selected / "modified.py").write_text("value = 2\n")
    (selected / "deleted.py").unlink()
    (selected / "untracked.py").write_text("untracked = True\n")
    (selected / "ignored.py").write_text("ignored = True\n")
    (source_repo / ".gitignore").write_text("experiment/ignored.py\n")
    (selected / "staged.py").write_text("staged = 2\n")
    (selected / "staged_deleted.py").unlink()
    (selected / "staged_added.py").write_text("added = True\n")
    (selected / "both.py").write_text("both = 2\n")
    _git(
        source_repo,
        "add",
        "experiment/staged.py",
        "experiment/staged_deleted.py",
        "experiment/staged_added.py",
        "experiment/both.py",
    )
    (selected / "both.py").write_text("both = 3\n")
    (selected / "intent.py").write_text("intent = True\n")
    _git(source_repo, "add", "-N", "experiment/intent.py")

    archive = tmp_path / "git-payload.tar.gz"
    stats = _create_sync_archive(selected, archive, git_aware=True)
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    with tarfile.open(archive, "r:gz") as payload:
        payload.extractall(extracted, filter="data")
    synced_repo = extracted / "payload"

    assert stats.git_commit == commit
    assert _git(synced_repo, "rev-parse", "HEAD") == commit
    assert _git(synced_repo, "remote", "get-url", "origin") == (
        "offline://mighty-colab/sync"
    )
    status = set(
        _git(synced_repo, "status", "--porcelain", "--", "experiment").splitlines()
    )
    source_status = set(
        _git(source_repo, "status", "--porcelain", "--", "experiment").splitlines()
    )
    assert status == source_status == {
        "MM experiment/both.py",
        " D experiment/deleted.py",
        "A  experiment/staged_added.py",
        "D  experiment/staged_deleted.py",
        "M  experiment/staged.py",
        " M experiment/modified.py",
        " A experiment/intent.py",
        "?? experiment/untracked.py",
    }
    assert not (synced_repo / "experiment" / "ignored.py").exists()
    assert not (synced_repo / "outside.bin").exists()
    assert stats.source_bytes < (source_repo / "outside.bin").stat().st_size
    outside_oid = _git(source_repo, "rev-parse", "HEAD:outside.bin")
    outside_probe = subprocess.run(
        ["git", "-C", str(synced_repo), "cat-file", "-e", outside_oid],
        capture_output=True,
    )
    assert outside_probe.returncode != 0
    local_root = os.fsencode(str(source_repo))
    for metadata in (synced_repo / ".git").rglob("*"):
        if metadata.is_file() and not metadata.is_symlink():
            assert local_root not in metadata.read_bytes()


def test_sync_command_uploads_archive_extracts_and_logs(
    tmp_path, mock_common_state, mocker
):
    source = tmp_path / "source"
    source.mkdir()
    (source / "data.txt").write_text("data\n" * 100)
    session = SessionState(
        name="s1", token="token", url="https://runtime", endpoint="endpoint"
    )
    mock_common_state.resolve_session.return_value = "s1"
    mock_common_state.store.get.return_value = session
    contents_cls = mocker.patch("colab_cli.commands.sync.ContentsClient")
    runtime_cls = mocker.patch("colab_cli.commands.sync.ColabRuntime")
    mocker.patch("colab_cli.commands.sync.uuid.uuid4").return_value.hex = "known"
    runtime_cls.return_value.execute_code.return_value = [
        {
            "output_type": "stream",
            "name": "stdout",
            "text": "MIGHTY_COLAB_SYNC_OK:known\n",
        }
    ]
    captured = {}

    def capture_upload(local_path, remote_path, timeout):
        captured["gzip_magic"] = Path(local_path).read_bytes()[:2]
        captured["remote_path"] = remote_path
        captured["timeout"] = timeout

    contents_cls.return_value.upload.side_effect = capture_upload

    result = runner.invoke(
        app,
        ["sync", str(source), "project", "-s", "s1", "--timeout", "45"],
    )

    assert result.exit_code == 0, result.output
    assert captured["gzip_magic"] == b"\x1f\x8b"
    assert captured["remote_path"].startswith("content/.mighty-colab-sync-")
    assert captured["timeout"] == (10, 45)
    extraction_code = runtime_cls.return_value.execute_code.call_args.args[0]
    assert repr("/content/project") in extraction_code
    assert runtime_cls.return_value.execute_code.call_args.kwargs["timeout"] == 45
    runtime_cls.return_value.stop.assert_called_once_with()
    contents_cls.return_value.rm.assert_called_once_with(
        "content/.mighty-colab-sync-known.tar.gz", timeout=(10, 45)
    )
    event = mock_common_state.history.log_event.call_args
    assert event.args[0:2] == ("s1", "file_operation")
    assert event.args[2]["op"] == "sync"
    assert event.args[2]["remote"] == "content/project"
    assert event.args[2]["git_aware"] is False
    assert "gzip" in result.output


def test_sync_command_reports_remote_extraction_error(
    tmp_path, mock_common_state, mocker
):
    source = tmp_path / "source.txt"
    source.write_text("source\n")
    session = SessionState(
        name="s1", token="token", url="https://runtime", endpoint="endpoint"
    )
    mock_common_state.resolve_session.return_value = "s1"
    mock_common_state.store.get.return_value = session
    contents_cls = mocker.patch("colab_cli.commands.sync.ContentsClient")
    runtime_cls = mocker.patch("colab_cli.commands.sync.ColabRuntime")
    runtime_cls.return_value.execute_code.return_value = [
        {
            "output_type": "error",
            "ename": "OSError",
            "evalue": "disk full",
            "traceback": [],
        }
    ]

    result = runner.invoke(app, ["sync", str(source), "target.txt", "-s", "s1"])

    assert result.exit_code == 1
    assert "Sync failed" in result.output
    assert "OSError: disk full" in result.output
    contents_cls.return_value.rm.assert_called_once_with(
        contents_cls.return_value.upload.call_args.args[1], timeout=(10, 600)
    )
    runtime_cls.return_value.stop.assert_called_once_with()


def test_sync_rejects_invalid_input_before_transfer(tmp_path, mock_common_state, mocker):
    contents_cls = mocker.patch("colab_cli.commands.sync.ContentsClient")

    result = runner.invoke(
        app,
        ["sync", str(tmp_path / "missing"), "../etc", "-s", "s1"],
    )

    assert result.exit_code == 1
    assert "Local path" in result.output
    contents_cls.assert_not_called()
def test_sync_rejects_empty_remote_reply(tmp_path, mock_common_state, mocker):
    source = tmp_path / "source.txt"
    source.write_text("source\n")
    session = SessionState(
        name="s1", token="token", url="https://runtime", endpoint="endpoint"
    )
    mock_common_state.resolve_session.return_value = "s1"
    mock_common_state.store.get.return_value = session
    mocker.patch("colab_cli.commands.sync.ContentsClient")
    runtime_cls = mocker.patch("colab_cli.commands.sync.ColabRuntime")
    runtime_cls.return_value.execute_code.return_value = []

    result = runner.invoke(app, ["sync", str(source), "target.txt", "-s", "s1"])

    assert result.exit_code == 1
    assert "did not confirm completion" in result.output
    mock_common_state.history.log_event.assert_not_called()


def test_sync_extract_rejects_destination_parent_symlink(tmp_path):
    content = tmp_path / "content"
    outside = tmp_path / "outside"
    source = tmp_path / "source"
    content.mkdir()
    outside.mkdir()
    source.mkdir()
    (source / "fresh.txt").write_text("fresh\n")
    (content / "link").symlink_to(outside, target_is_directory=True)
    destination = outside / "destination"
    destination.mkdir()
    (destination / "old.txt").write_text("old\n")
    archive = content / "payload.tar.gz"
    _create_sync_archive(source, archive, git_aware=False)

    with pytest.raises(ValueError, match="outside /content"):
        exec(
            _build_sync_extract_script(
                str(archive),
                str(content / "link" / "destination"),
                operation_id="known",
                content_root=str(content),
            ),
            {},
        )

    assert (destination / "old.txt").read_text() == "old\n"


def test_sync_extract_rejects_symlink_that_escapes_after_relocation(tmp_path):
    content = tmp_path / "content"
    content.mkdir()
    destination = content / "destination"
    destination.mkdir()
    (destination / "old.txt").write_text("old\n")
    (content / "victim.txt").write_text("victim\n")
    archive = content / "payload.tar.gz"
    with tarfile.open(archive, "w:gz") as payload:
        root = tarfile.TarInfo("payload")
        root.type = tarfile.DIRTYPE
        payload.addfile(root)
        link = tarfile.TarInfo("payload/link")
        link.type = tarfile.SYMTYPE
        link.linkname = "../victim.txt"
        payload.addfile(link)

    with pytest.raises(ValueError, match="symlink.*escapes"):
        exec(
            _build_sync_extract_script(
                str(archive),
                str(destination),
                operation_id="known",
                content_root=str(content),
            ),
            {},
        )

    assert (destination / "old.txt").read_text() == "old\n"
    assert (content / "victim.txt").read_text() == "victim\n"


def test_sync_extract_supports_maximum_length_destination_name(tmp_path):
    content = tmp_path / "content"
    source = tmp_path / "source"
    content.mkdir()
    source.mkdir()
    (source / "fresh.txt").write_text("fresh\n")
    archive = content / "payload.tar.gz"
    _create_sync_archive(source, archive, git_aware=False)
    destination = content / ("x" * 240)

    exec(
        _build_sync_extract_script(
            str(archive),
            str(destination),
            operation_id="known",
            content_root=str(content),
        ),
        {},
    )

    assert (destination / "fresh.txt").read_text() == "fresh\n"


def test_git_aware_sync_preserves_source_sparse_checkout_status(tmp_path):
    source_repo = tmp_path / "repo"
    source_repo.mkdir()
    _git(source_repo, "init", "-q")
    _git(source_repo, "config", "user.email", "sync-test@example.com")
    _git(source_repo, "config", "user.name", "Sync Test")
    (source_repo / "selected").mkdir()
    (source_repo / "selected" / "present.txt").write_text("present\n")
    (source_repo / "selected" / "sparse.txt").write_bytes(os.urandom(1_250_000))
    _git(source_repo, "add", ".")
    _git(source_repo, "commit", "-qm", "fixture")
    sparse_oid = _git(source_repo, "rev-parse", "HEAD:selected/sparse.txt")
    _git(source_repo, "sparse-checkout", "set", "--no-cone", "selected/present.txt")
    _git(source_repo, "update-index", "--assume-unchanged", "selected/sparse.txt")
    assert _git(source_repo, "status", "--porcelain", "--", "selected") == ""

    archive = tmp_path / "payload.tar.gz"
    stats = _create_sync_archive(
        source_repo / "selected", archive, git_aware=True
    )
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    with tarfile.open(archive, "r:gz") as payload:
        payload.extractall(extracted, filter="data")

    assert _git(
        extracted / "payload", "status", "--porcelain", "--", "selected"
    ) == ""
    synced_repo = extracted / "payload"
    skipped_probe = subprocess.run(
        ["git", "-C", str(synced_repo), "cat-file", "-e", sparse_oid],
        capture_output=True,
    )
    assert skipped_probe.returncode != 0
    assert stats.source_bytes < 1_250_000


def test_git_aware_sync_rejects_embedded_repository(tmp_path):
    source_repo = tmp_path / "repo"
    _init_repo(source_repo)
    nested = source_repo / "experiment" / "nested"
    nested.mkdir()
    _git(nested, "init", "-q")
    (nested / "file.txt").write_text("nested\n")

    with pytest.raises(ValueError, match="embedded Git repository"):
        _create_sync_archive(
            source_repo / "experiment",
            tmp_path / "payload.tar.gz",
            git_aware=True,
        )


def test_sync_reconciles_terminal_upload_error(tmp_path, mock_common_state, mocker):
    source = tmp_path / "source.txt"
    source.write_text("source\n")
    session = SessionState(
        name="s1", token="token", url="https://runtime", endpoint="endpoint"
    )
    mock_common_state.resolve_session.return_value = "s1"
    mock_common_state.store.get.return_value = session
    response = requests.Response()
    response.status_code = 401
    error = requests.HTTPError("401 expired proxy token", response=response)
    contents_cls = mocker.patch("colab_cli.commands.sync.ContentsClient")
    contents_cls.return_value.upload.side_effect = error
    reconcile = mocker.patch(
        "colab_cli.commands.execution._handle_terminal_session_error"
    )

    result = runner.invoke(app, ["sync", str(source), "target.txt", "-s", "s1"])

    assert result.exit_code == 1
    reconcile.assert_called_once_with("s1")
    contents_cls.return_value.rm.assert_called_once_with(
        contents_cls.return_value.upload.call_args.args[1], timeout=(10, 600)
    )


def test_sync_extract_failure_preserves_old_destination(tmp_path):
    content = tmp_path / "content"
    content.mkdir()
    destination = content / "destination"
    destination.mkdir()
    (destination / "old.txt").write_text("old\n")
    archive = content / "payload.tar.gz"
    archive.write_bytes(b"not a gzip archive")

    with pytest.raises(tarfile.ReadError):
        exec(
            _build_sync_extract_script(
                str(archive),
                str(destination),
                operation_id="known",
                content_root=str(content),
            ),
            {},
        )

    assert (destination / "old.txt").read_text() == "old\n"
    assert not archive.exists()
    assert not list(content.glob(".mighty-colab-sync-*"))


def test_sync_extract_rejects_symlink_payload_root(tmp_path):
    content = tmp_path / "content"
    content.mkdir()
    destination = content / "destination"
    destination.mkdir()
    (destination / "old.txt").write_text("old\n")
    (content / "victim.txt").write_text("victim\n")
    archive = content / "payload.tar.gz"
    with tarfile.open(archive, "w:gz") as payload:
        link = tarfile.TarInfo("payload")
        link.type = tarfile.SYMTYPE
        link.linkname = "victim.txt"
        payload.addfile(link)

    with pytest.raises(ValueError, match="payload root cannot be a symlink"):
        exec(
            _build_sync_extract_script(
                str(archive),
                str(destination),
                operation_id="known",
                content_root=str(content),
            ),
            {},
        )

    assert (destination / "old.txt").read_text() == "old\n"


def test_sync_reconciles_contents_upload_not_found(
    tmp_path, mock_common_state, mocker
):
    source = tmp_path / "source.txt"
    source.write_text("source\n")
    session = SessionState(
        name="s1", token="token", url="https://runtime", endpoint="endpoint"
    )
    mock_common_state.resolve_session.return_value = "s1"
    mock_common_state.store.get.return_value = session
    contents_cls = mocker.patch("colab_cli.commands.sync.ContentsClient")
    contents_cls.return_value.upload.side_effect = FileNotFoundError(
        "File or directory not found: staging archive"
    )
    reconcile = mocker.patch(
        "colab_cli.commands.execution._handle_terminal_session_error"
    )

    result = runner.invoke(app, ["sync", str(source), "target.txt", "-s", "s1"])

    assert result.exit_code == 1
    reconcile.assert_called_once_with("s1")

def test_sync_rewrites_internal_symlink_for_archive_relocation(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "target.txt").write_text("target\n")
    (source / "link.txt").symlink_to("../source/target.txt")
    content = tmp_path / "content"
    content.mkdir()
    archive = content / "payload.tar.gz"
    destination = content / "destination"

    _create_sync_archive(source, archive, git_aware=False)
    exec(
        _build_sync_extract_script(
            str(archive),
            str(destination),
            operation_id="known",
            content_root=str(content),
        ),
        {},
    )

    assert (destination / "link.txt").is_symlink()
    assert (destination / "link.txt").read_text() == "target\n"


def test_sync_extract_pins_destination_parent_against_symlink_swap(
    tmp_path, monkeypatch
):
    content = tmp_path / "content"
    safe = content / "safe"
    moved = content / "moved"
    outside = tmp_path / "outside"
    source = tmp_path / "source"
    safe.mkdir(parents=True)
    outside.mkdir()
    source.mkdir()
    (source / "fresh.txt").write_text("fresh\n")
    archive = content / "payload.tar.gz"
    _create_sync_archive(source, archive, git_aware=False)

    original_mkdir = os.mkdir
    swapped = False

    def swap_parent_then_mkdir(path, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if not swapped and Path(path).name.startswith(".mighty-colab-sync-"):
            swapped = True
            safe.rename(moved)
            safe.symlink_to(outside, target_is_directory=True)
        return original_mkdir(path, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "mkdir", swap_parent_then_mkdir)

    exec(
        _build_sync_extract_script(
            str(archive),
            str(safe / "destination"),
            operation_id="known",
            content_root=str(content),
        ),
        {},
    )

    assert swapped
    assert not (outside / "destination").exists()
    assert (moved / "destination" / "fresh.txt").read_text() == "fresh\n"

@pytest.mark.parametrize(("status", "should_reconcile"), [(401, True), (404, False)])
def test_sync_reconciles_only_cleanup_auth_error(
    tmp_path, mock_common_state, mocker, status, should_reconcile
):
    source = tmp_path / "source.txt"
    source.write_text("source\n")
    session = SessionState(
        name="s1", token="token", url="https://runtime", endpoint="endpoint"
    )
    mock_common_state.resolve_session.return_value = "s1"
    mock_common_state.store.get.return_value = session
    contents_cls = mocker.patch("colab_cli.commands.sync.ContentsClient")
    response = requests.Response()
    response.status_code = status
    contents_cls.return_value.rm.side_effect = requests.HTTPError(response=response)
    runtime_cls = mocker.patch("colab_cli.commands.sync.ColabRuntime")
    runtime_cls.return_value.execute_code.return_value = [
        {
            "output_type": "stream",
            "name": "stdout",
            "text": "MIGHTY_COLAB_SYNC_OK:known\n",
        }
    ]
    mocker.patch("colab_cli.commands.sync.uuid.uuid4").return_value.hex = "known"
    reconcile = mocker.patch(
        "colab_cli.commands.execution._handle_terminal_session_error"
    )

    result = runner.invoke(app, ["sync", str(source), "target.txt", "-s", "s1"])

    assert result.exit_code == 0, result.output
    if should_reconcile:
        reconcile.assert_called_once_with("s1")
    else:
        reconcile.assert_not_called()


def test_sync_cleanup_continues_after_base_exception(tmp_path, monkeypatch):
    content = tmp_path / "content"
    source = tmp_path / "source"
    destination = content / "destination"
    content.mkdir()
    source.mkdir()
    destination.mkdir()
    (source / "fresh.txt").write_text("fresh\n")
    (destination / "old.txt").write_text("old\n")
    archive = content / "payload.tar.gz"
    _create_sync_archive(source, archive, git_aware=False)

    original_rmtree = shutil.rmtree
    interrupted = False

    def interrupt_after_stage_cleanup(path, *args, **kwargs):
        nonlocal interrupted
        result = original_rmtree(path, *args, **kwargs)
        if not interrupted and Path(path).name.startswith(".mighty-colab-sync-"):
            interrupted = True
            raise KeyboardInterrupt("cleanup interrupted")
        return result

    monkeypatch.setattr(shutil, "rmtree", interrupt_after_stage_cleanup)

    with pytest.raises(KeyboardInterrupt, match="cleanup interrupted"):
        exec(
            _build_sync_extract_script(
                str(archive),
                str(destination),
                operation_id="known",
                content_root=str(content),
            ),
            {},
        )

    assert interrupted
    assert not archive.exists()
    assert not list(content.glob(".mighty-colab-sync-*"))
    assert (destination / "fresh.txt").read_text() == "fresh\n"
def test_git_aware_sync_treats_selected_path_as_literal(tmp_path):
    repo = tmp_path / "repo"
    selected = repo / "data*"
    unrelated = repo / "data-private"
    selected.mkdir(parents=True)
    unrelated.mkdir()
    (selected / "selected.txt").write_text("selected\n")
    (unrelated / "secret.txt").write_text("secret\n")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "sync-test@example.com")
    _git(repo, "config", "user.name", "Sync Test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "fixture")
    (selected / "untracked.txt").write_text("untracked\n")
    (unrelated / "unrelated-untracked.txt").write_text("unrelated\n")

    archive = tmp_path / "literal.tar.gz"
    _create_sync_archive(selected, archive, git_aware=True)
    extracted = tmp_path / "extracted-literal"
    extracted.mkdir()
    with tarfile.open(archive, "r:gz") as payload:
        payload.extractall(extracted, filter="data")

    synced = extracted / "payload"
    assert (synced / "data*" / "selected.txt").read_text() == "selected\n"
    assert (synced / "data*" / "untracked.txt").read_text() == "untracked\n"
    assert not (synced / "data-private").exists()


def test_git_aware_sync_preserves_core_filemode_status_semantics(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    selected = repo / "experiment"
    _git(repo, "config", "core.fileMode", "false")
    script = selected / "modified.py"
    script.chmod(0o755)
    assert _git(repo, "status", "--porcelain", "--", "experiment") == ""

    archive = tmp_path / "filemode.tar.gz"
    _create_sync_archive(selected, archive, git_aware=True)
    extracted = tmp_path / "extracted-filemode"
    extracted.mkdir()
    with tarfile.open(archive, "r:gz") as payload:
        payload.extractall(extracted, filter="data")

    synced = extracted / "payload"
    assert _git(synced, "config", "--bool", "core.fileMode") == "false"
    assert _git(synced, "status", "--porcelain", "--", "experiment") == ""
def test_sync_rebuilds_cleanup_client_after_terminal_reconciliation(
    tmp_path, mock_common_state, mocker
):
    source = tmp_path / "source.txt"
    source.write_text("source\n")
    initial = SessionState(
        name="s1", token="expired", url="https://old-runtime", endpoint="endpoint"
    )
    refreshed = SessionState(
        name="s1", token="fresh", url="https://new-runtime", endpoint="endpoint"
    )
    mock_common_state.resolve_session.return_value = "s1"
    mock_common_state.store.get.side_effect = [initial, refreshed]
    initial_contents = mocker.Mock()
    refreshed_contents = mocker.Mock()
    contents_cls = mocker.patch(
        "colab_cli.commands.sync.ContentsClient",
        side_effect=[initial_contents, refreshed_contents],
    )
    response = requests.Response()
    response.status_code = 401
    error = requests.HTTPError("401 expired proxy token", response=response)
    runtime_cls = mocker.patch("colab_cli.commands.sync.ColabRuntime")
    runtime_cls.return_value.execute_code.side_effect = error
    reconcile = mocker.patch(
        "colab_cli.commands.execution._handle_terminal_session_error"
    )

    result = runner.invoke(app, ["sync", str(source), "target.txt", "-s", "s1"])

    assert result.exit_code == 1
    reconcile.assert_called_once_with("s1")
    assert contents_cls.call_args_list == [mocker.call(initial), mocker.call(refreshed)]
    initial_contents.rm.assert_not_called()
    refreshed_contents.rm.assert_called_once_with(
        initial_contents.upload.call_args.args[1], timeout=(10, 600)
    )
def test_default_sync_rejects_selected_git_metadata_root(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)

    with pytest.raises(ValueError, match="Git metadata"):
        _create_sync_archive(
            repo / ".git", tmp_path / "git-metadata.tar.gz", git_aware=False
        )


def test_default_sync_snapshots_before_tar_walk(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    victim = source / "victim.txt"
    victim.write_text("original\n")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n")
    original_gettarinfo = tarfile.TarFile.gettarinfo
    swapped = False

    def swap_after_tar_stat(archive, name=None, arcname=None, fileobj=None):
        nonlocal swapped
        info = original_gettarinfo(archive, name, arcname, fileobj)
        if not swapped and name is not None and Path(name) == victim:
            swapped = True
            victim.unlink()
            victim.symlink_to(outside)
        return info

    monkeypatch.setattr(tarfile.TarFile, "gettarinfo", swap_after_tar_stat)
    archive = tmp_path / "snapshot.tar.gz"
    _create_sync_archive(source, archive, git_aware=False)

    extracted = tmp_path / "snapshot"
    extracted.mkdir()
    with tarfile.open(archive, "r:gz") as payload:
        payload.extractall(extracted, filter="data")
    assert (extracted / "payload" / "victim.txt").read_text() == "original\n"


def test_sync_extract_fails_if_pinned_parent_moves_outside_content(
    tmp_path, monkeypatch
):
    content = tmp_path / "content"
    safe = content / "safe"
    outside = tmp_path / "outside"
    moved = outside / "moved"
    source = tmp_path / "source"
    safe.mkdir(parents=True)
    outside.mkdir()
    source.mkdir()
    (source / "fresh.txt").write_text("fresh\n")
    archive = content / "payload.tar.gz"
    _create_sync_archive(source, archive, git_aware=False)

    original_mkdir = os.mkdir
    swapped = False

    def move_parent_then_mkdir(path, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if not swapped and Path(path).name.startswith(".mighty-colab-sync-"):
            swapped = True
            safe.rename(moved)
        return original_mkdir(path, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "mkdir", move_parent_then_mkdir)

    with pytest.raises(ValueError, match="moved outside /content"):
        exec(
            _build_sync_extract_script(
                str(archive),
                str(safe / "destination"),
                operation_id="known",
                content_root=str(content),
            ),
            {},
        )

    assert swapped
    assert not (moved / "destination").exists()
