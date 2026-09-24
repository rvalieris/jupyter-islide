"""Guards: the package version in pyproject.toml must match the frontend.

pyproject.toml is the single source of truth; frontend/package.json and
frontend/package-lock.json are kept in sync by frontend/sync-version.mjs
(which also runs as the npm `prebuild` hook).
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"


def _pyproject_version():
    pyproject = (ROOT / "pyproject.toml").read_text()
    return re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.M).group(1)


def test_versions_synced():
    pyproject = (ROOT / "pyproject.toml").read_text()
    pyver = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.M).group(1)

    pkg = json.loads((ROOT / "frontend" / "package.json").read_text())
    lock = json.loads((ROOT / "frontend" / "package-lock.json").read_text())

    assert pyver == pkg["version"], (
        f"pyproject.toml version {pyver!r} != frontend/package.json version {pkg['version']!r}"
    )
    assert pyver == lock["version"], (
        f"pyproject.toml version {pyver!r} != frontend/package-lock.json version {lock['version']!r}"
    )
    assert pyver == lock["packages"][""]["version"], (
        f"pyproject.toml version {pyver!r} != package-lock.json root package version "
        f"{lock['packages']['']['version']!r}"
    )


def test_sync_version_script_replaces_stale_versions():
    """Corrupt the frontend versions and verify sync-version.mjs restores them.

    This proves the script actually replaces stale values rather than merely
    reading them; the original files are always restored, even on failure.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")

    pyver = _pyproject_version()
    pkg_path = FRONTEND / "package.json"
    lock_path = FRONTEND / "package-lock.json"

    pkg_original = pkg_path.read_text()
    lock_original = lock_path.read_text()
    pkg_after = lock_after = None
    try:
        pkg = json.loads(pkg_original)
        pkg["version"] = "9.9.9"
        pkg_path.write_text(json.dumps(pkg, indent=2) + "\n")

        lock = json.loads(lock_original)
        lock["version"] = "9.9.9"
        lock["packages"][""]["version"] = "9.9.9"
        lock_path.write_text(json.dumps(lock, indent=2) + "\n")

        subprocess.run(
            [node, "sync-version.mjs"], cwd=FRONTEND, check=True, capture_output=True
        )
        pkg_after = pkg_path.read_text()
        lock_after = lock_path.read_text()
    finally:
        pkg_path.write_text(pkg_original)
        lock_path.write_text(lock_original)

    assert json.loads(pkg_after)["version"] == pyver
    lock = json.loads(lock_after)
    assert lock["version"] == pyver
    assert lock["packages"][""]["version"] == pyver
