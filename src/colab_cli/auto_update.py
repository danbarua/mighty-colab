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

"""Auto-update subsystem.

Owns version detection, the PyPI-style update probe, the on-disk
``latest_version`` cache, and the upgrade-banner UX. The CLI's global
callback (``cli.py``) calls ``check_for_updates`` once per day and
``maybe_show_cached_banner`` on every other invocation; the
``colab update`` Typer command (``commands/utility.py``) delegates to
``check_for_updates``.
"""

import json
import platform
import subprocess
import urllib.request
from datetime import datetime, timezone
from importlib.metadata import (
    PackageNotFoundError,
    distribution,
    version as installed_version,
)
from packaging.version import InvalidVersion, Version
from typing import Optional

import typer

from colab_cli.common import state
from colab_cli.state import Settings

# PyPI distribution name (different from the importable package name `colab`).
PYPI_PACKAGE_NAME = "mighty-colab"
# Hardcoded rather than read from Settings.update_url: this fork intentionally
# diverges from upstream `colab` here, and keeping the divergence local to a
# constant (instead of touching state.py/SettingsStore) keeps merges from
# upstream conflict-free. It also means installs upgrading from a pre-fork
# settings.json with a stale `update_url` on disk still probe the right feed.
PYPI_UPDATE_URL = f"https://pypi.org/pypi/{PYPI_PACKAGE_NAME}/json"


# ---------- Version detection -------------------------------------------


def is_self_install_supported() -> bool:
    """Return True if self-install (--install) is supported on the current platform."""
    return platform.system() in ("Linux", "Darwin")


def _is_editable_install(dist_name: str) -> bool:
    """True if `dist_name` is installed in editable/develop mode.

    Detected via PEP 610's `direct_url.json`, which pip/uv write next to
    any install and mark with `dir_info.editable: true` for `pip install
    -e .` / `uv sync`-style local installs. `Distribution.origin` is the
    typed accessor for this file but isn't available until Python 3.13;
    `read_text` on the raw metadata file works on the 3.12 floor this
    project targets.
    """
    try:
        raw = distribution(dist_name).read_text("direct_url.json")
    except PackageNotFoundError:
        return False
    if not raw:
        return False
    try:
        return bool(json.loads(raw).get("dir_info", {}).get("editable"))
    except (json.JSONDecodeError, AttributeError):
        return False


def get_app_version() -> str:
    """Return the installed package version, falling back to the git short hash.

    An editable/dev install's `importlib.metadata` version is a static
    snapshot hatch-vcs wrote the last time `uv sync`/`pip install -e .`
    ran -- it does not track HEAD on every invocation, so it silently goes
    stale the moment you commit again without resyncing (e.g. still
    reporting a pre-release tag after several dev commits land). Treat an
    editable install the same as "package not found" and go straight to
    the live git short hash, so `colab version` reflects what code is
    actually running rather than a frozen install-time snapshot.
    """
    if not _is_editable_install("mighty-colab"):
        try:
            return installed_version("mighty-colab")
        except (PackageNotFoundError, InvalidVersion):
            pass

    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
            encoding="utf-8",
        ).strip()
    except Exception:
        return "unknown"


# ---------- Source fetchers ---------------------------------------------


def _parse_version(payload: Optional[dict]) -> Optional[str]:
    """Returns ``info.version`` from a PyPI-style payload, or None."""
    return (payload or {}).get("info", {}).get("version")


def _fetch_pypi(url: str, quiet: bool) -> Optional[dict]:
    """Fetches and parses the PyPI-style JSON document at ``url``."""
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as e:
        if not quiet:
            typer.echo(f"[colab] Warning: Failed to fetch update info: {e}")
        return None


# ---------- Version comparison ------------------------------------------


def _is_newer(candidate: Optional[str], current: str) -> bool:
    """True when ``candidate`` strictly exceeds ``current`` (PEP 440)."""
    if not candidate:
        return False
    try:
        return Version(candidate) > Version(current)
    except InvalidVersion:
        return candidate != current


# ---------- UX ----------------------------------------------------------


def announce_upgrade(
    latest: str,
    current: str,
    install_cmd: str,
    *,
    show_disable_hint: bool = False,
) -> None:
    """Print the upgrade banner.

    ``show_disable_hint`` controls whether the trailing line that explains
    how to silence the auto-check is included. It is only added when the
    banner is shown unsolicited (the daily background fetch and the cached
    banner on subsequent invocations); explicit ``colab update`` calls
    omit it because the user already opted in to seeing the result.
    """
    typer.echo(
        f"\n[colab] A new version of Colab CLI is available: {latest} (current: {current})"
    )
    if is_self_install_supported() and ("pip" in install_cmd or "uv" in install_cmd):
        typer.echo("[colab] You can run 'colab update --install' to upgrade in place.")
    typer.echo(f"[colab] Run '{install_cmd}' to update.")
    if show_disable_hint:
        typer.echo(
            "[colab] To silence this check, set "
            '"enable_update_check": false in '
            "~/.config/colab-cli/settings.json"
        )
    typer.echo("")


# ---------- Orchestration -----------------------------------------------


def _get_install_command() -> str:
    """Return the recommended installation command based on the environment."""
    import sys

    if is_self_install_supported() and "/uv/tools/" in sys.executable:
        return f"uv tool install -U {PYPI_PACKAGE_NAME}"
    return f"pip install --upgrade {PYPI_PACKAGE_NAME}"


def check_for_updates(quiet: bool = False) -> None:
    """Check PyPI for updates and print a message if a new version is available.

    The disable-hint is appended to the banner only when ``quiet`` is True
    (the daily background fetch); explicit ``colab update`` invocations
    (``quiet=False``) omit it because the user asked for the check.
    """
    settings = state.settings_store.load()
    current = get_app_version()

    try:
        pypi = _fetch_pypi(PYPI_UPDATE_URL, quiet)
        pypi_v = _parse_version(pypi)

        if _is_newer(pypi_v, current):
            announce_upgrade(
                pypi_v,
                current,
                _get_install_command(),
                show_disable_hint=quiet,
            )
        elif not quiet:
            suffix = f", latest: {pypi_v}" if pypi_v else ""
            typer.echo(f"[colab] Colab CLI is up to date (version: {current}{suffix}).")

        # Cache the highest observed version; never downgrade.
        cached = settings.latest_version or "0"
        if _is_newer(pypi_v, cached):
            settings.latest_version = pypi_v

        settings.last_check = datetime.now(timezone.utc)
        state.settings_store.save(settings)

    except Exception as e:
        if not quiet:
            typer.echo(f"[colab] Failed to check for updates: {e}")


# ---------- Background hooks (called from cli.py) -----------------------


def _is_throttled(settings: Settings, *, now: Optional[datetime] = None) -> bool:
    """True if the once-per-day fetch should be skipped."""
    if settings.last_check is None:
        return False
    now = now or datetime.now(timezone.utc)
    return (now - settings.last_check).days < 1


def maybe_show_cached_banner(settings: Settings) -> None:
    """Print the cached upgrade banner if the cache reports a newer version.

    Called from the global CLI callback when the daily fetch is throttled.
    The banner uses a generic ``colab update`` install hint because the
    cache does not record which source supplied the version; the disable
    hint is shown because this is unsolicited output.
    """
    if not settings.latest_version:
        return
    current = get_app_version()
    if not _is_newer(settings.latest_version, current):
        return
    announce_upgrade(
        settings.latest_version,
        current,
        "colab update",
        show_disable_hint=True,
    )


def run_background_check() -> None:
    """Entry point for the global CLI callback.

    Performs either the daily fetch (which writes the cache) or, if
    throttled, surfaces the cached banner. Honors the
    ``enable_update_check`` master switch.
    """
    settings = state.settings_store.load()
    if not settings.enable_update_check:
        return
    if _is_throttled(settings):
        maybe_show_cached_banner(settings)
    else:
        check_for_updates(quiet=True)


# ---------- Self-install ------------------------------------------------


def self_install() -> None:
    """Upgrade the CLI in place, detecting uv vs pip."""
    import sys

    # If the executable path contains "/uv/", we assume it was installed via
    # `uv tool install` and use `uv` to upgrade it.
    if "/uv/tools/" in sys.executable:
        cmd = ["uv", "tool", "install", "-U", PYPI_PACKAGE_NAME]
    else:
        cmd = [sys.executable, "-m", "pip", "install", "-U", PYPI_PACKAGE_NAME]

    typer.echo(f"[colab] Running: {' '.join(cmd)}")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        raise typer.Exit(code=result.returncode)
