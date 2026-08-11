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

from importlib.metadata import PackageNotFoundError
from typer.testing import CliRunner
from unittest.mock import patch

from colab_cli.cli import app

runner = CliRunner()


def test_version_installed():
    with (
        patch("colab_cli.auto_update._is_editable_install", return_value=False),
        patch("colab_cli.auto_update.installed_version") as mock_version,
    ):
        mock_version.return_value = "0.2.0"
        result = runner.invoke(app, ["version"])
        assert result.exit_code == 0
        assert "Version: 0.2.0" in result.output
        # Guard against a silent regression if the PyPI distribution name
        # changes again: importlib.metadata.version() must be called with
        # the distribution name exactly as it appears in pyproject.toml.
        mock_version.assert_called_with("mighty-colab")


def test_version_git_fallback():
    with (
        patch("colab_cli.auto_update._is_editable_install", return_value=False),
        patch("colab_cli.auto_update.installed_version") as mock_version,
    ):
        mock_version.side_effect = PackageNotFoundError

        with patch("subprocess.check_output") as mock_git:
            mock_git.return_value = "abc1234"
            result = runner.invoke(app, ["version"])
            assert result.exit_code == 0
            assert "Version: abc1234" in result.output


def test_version_unknown():
    with (
        patch("colab_cli.auto_update._is_editable_install", return_value=False),
        patch("colab_cli.auto_update.installed_version") as mock_version,
    ):
        mock_version.side_effect = PackageNotFoundError

        with patch("subprocess.check_output") as mock_git:
            mock_git.side_effect = Exception("git not found")
            result = runner.invoke(app, ["version"])
            assert result.exit_code == 0
            assert "Version: unknown" in result.output


def test_version_editable_install_prefers_git_hash_over_stale_metadata():
    """An editable/dev install's `importlib.metadata` version is a frozen
    snapshot from the last `uv sync` -- it can be valid yet still stale
    (e.g. still reporting v0.2.1 after several undeployed commits). Editable
    installs should skip it entirely and report the live git hash instead,
    the same as the "package not found" fallback."""
    with (
        patch("colab_cli.auto_update._is_editable_install", return_value=True),
        patch("colab_cli.auto_update.installed_version") as mock_version,
    ):
        mock_version.return_value = "0.2.1"  # valid, just stale

        with patch("subprocess.check_output") as mock_git:
            mock_git.return_value = "a63dbc3"
            result = runner.invoke(app, ["version"])
            assert result.exit_code == 0
            assert "Version: a63dbc3" in result.output
            assert "0.2.1" not in result.output
            # The stale metadata must never even be consulted for an
            # editable install -- not read-and-discarded, skipped outright.
            mock_version.assert_not_called()


def test_is_editable_install_true_when_direct_url_marks_editable(mocker):
    from colab_cli.auto_update import _is_editable_install

    mock_dist = mocker.MagicMock()
    mock_dist.read_text.return_value = (
        '{"url": "file:///repo", "dir_info": {"editable": true}}'
    )
    mocker.patch("colab_cli.auto_update.distribution", return_value=mock_dist)

    assert _is_editable_install("mighty-colab") is True
    mock_dist.read_text.assert_called_once_with("direct_url.json")


def test_is_editable_install_false_for_regular_install(mocker):
    from colab_cli.auto_update import _is_editable_install

    mock_dist = mocker.MagicMock()
    mock_dist.read_text.return_value = '{"url": "https://pypi.org/..."}'
    mocker.patch("colab_cli.auto_update.distribution", return_value=mock_dist)

    assert _is_editable_install("mighty-colab") is False


def test_is_editable_install_false_when_no_direct_url_file(mocker):
    from colab_cli.auto_update import _is_editable_install

    mock_dist = mocker.MagicMock()
    # importlib.metadata.Distribution.read_text returns None for a missing file.
    mock_dist.read_text.return_value = None
    mocker.patch("colab_cli.auto_update.distribution", return_value=mock_dist)

    assert _is_editable_install("mighty-colab") is False


def test_is_editable_install_false_when_package_not_found(mocker):
    from colab_cli.auto_update import _is_editable_install

    mocker.patch(
        "colab_cli.auto_update.distribution", side_effect=PackageNotFoundError
    )

    assert _is_editable_install("mighty-colab") is False


def test_is_editable_install_false_on_malformed_json(mocker):
    from colab_cli.auto_update import _is_editable_install

    mock_dist = mocker.MagicMock()
    mock_dist.read_text.return_value = "not json"
    mocker.patch("colab_cli.auto_update.distribution", return_value=mock_dist)

    assert _is_editable_install("mighty-colab") is False
