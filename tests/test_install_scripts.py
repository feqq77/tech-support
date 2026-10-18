"""Installer scripts verified against a local fake release server.

These tests run `get.sh` (the one-line installer) for real, with downloads
served over HTTP from a local server that mimics the GitHub release layout.
"""

from __future__ import annotations

import hashlib
import http.server
import os
import socketserver
import subprocess
import tarfile
import threading
import time
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[1]
GET_SH = PROJECT / "get.sh"
APPIMAGE_NAME = "TechSupport-Customer-x86_64.AppImage"
TAG = "v0.1.0-test"
REPO_PATH = "test/repo"


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):  # keep pytest output clean
        pass


@pytest.fixture()
def release_server(tmp_path):
    """Serves a directory over HTTP; the test builds the fake release tree."""
    root = tmp_path / "www"
    root.mkdir()

    handler = lambda *a, **kw: _QuietHandler(*a, directory=str(root), **kw)  # noqa: E731
    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield root, f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def _add_release(root: Path, payload: bytes, checksum_of: bytes | None = None):
    """Place AppImage + .sha256 in the GitHub release-download layout."""
    asset_dir = root / REPO_PATH / "releases" / "download" / TAG
    asset_dir.mkdir(parents=True, exist_ok=True)
    (asset_dir / APPIMAGE_NAME).write_bytes(payload)
    digest = hashlib.sha256(checksum_of if checksum_of is not None else payload).hexdigest()
    (asset_dir / f"{APPIMAGE_NAME}.sha256").write_text(f"{digest}  {APPIMAGE_NAME}\n")


def _run_get_sh(home: Path, base: str, *args: str, version: str | None = TAG,
                arch: str = "x86_64", api_base: str | None = None,
                extra_env: dict | None = None):
    env = {
        **os.environ,
        "HOME": str(home),
        "DISPLAY": ":999",  # presence of a display enables auto-launch
        "TECH_SUPPORT_REPO": REPO_PATH,
        "TECH_SUPPORT_DOWNLOAD_BASE": base,
        "TECH_SUPPORT_ARCH": arch,
    }
    if version is not None:
        env["TECH_SUPPORT_VERSION"] = version
    if api_base is not None:
        env["TECH_SUPPORT_API_BASE"] = api_base
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(GET_SH), *args],
        capture_output=True, text=True, timeout=90, env=env,
    )


def _wait_for(path: Path, timeout: float = 8.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            return True
        time.sleep(0.1)
    return False


FAKE_APPIMAGE = b"""#!/bin/sh
echo launched > "$HOME/launched.marker"
sleep 3   # stay alive like a real GUI app while the installer checks on it
"""


def test_get_sh_installs_and_launches(tmp_path, release_server):
    root, base = release_server
    _add_release(root, FAKE_APPIMAGE)
    home = tmp_path / "home"
    home.mkdir()

    res = _run_get_sh(home, base)
    assert res.returncode == 0, res.stderr
    assert "integrity verified" in res.stdout
    assert "The app is starting now." in res.stdout

    binary = home / ".local/bin/tech-support"
    assert binary.is_file() and os.access(binary, os.X_OK)
    appimage = home / f".local/share/tech-support/{APPIMAGE_NAME}"
    assert appimage.is_file()
    # auto-launch actually ran the app
    assert _wait_for(home / "launched.marker"), "the app was not auto-launched"


def test_get_sh_resolves_latest_release(tmp_path, release_server):
    """Without a pinned version the installer asks GitHub for the latest tag."""
    root, base = release_server
    _add_release(root, FAKE_APPIMAGE)
    api_dir = root / "repos" / REPO_PATH / "releases"
    api_dir.mkdir(parents=True, exist_ok=True)
    (api_dir / "latest").write_text(f'{{"tag_name": "{TAG}"}}\n')
    home = tmp_path / "home"
    home.mkdir()

    res = _run_get_sh(home, base, version=None, api_base=base)
    assert res.returncode == 0, res.stderr
    assert f"release:  {TAG}" in res.stdout
    assert (home / ".local/share/tech-support" / APPIMAGE_NAME).is_file()


def test_get_sh_no_launch_flag(tmp_path, release_server):
    root, base = release_server
    _add_release(root, FAKE_APPIMAGE)
    home = tmp_path / "home"
    home.mkdir()

    res = _run_get_sh(home, base, "--no-launch")
    assert res.returncode == 0, res.stderr
    assert (home / ".local/bin/tech-support").is_file()
    assert not (home / "launched.marker").exists()
    assert "start it any time with: tech-support" in res.stdout


def test_get_sh_rejects_bad_checksum(tmp_path, release_server):
    root, base = release_server
    _add_release(root, FAKE_APPIMAGE, checksum_of=b"something else entirely")
    home = tmp_path / "home"
    home.mkdir()

    res = _run_get_sh(home, base)
    assert res.returncode != 0
    assert "integrity check failed" in res.stderr
    # nothing was installed
    assert not (home / ".local/bin/tech-support").exists()
    assert not (home / ".local/share/tech-support" / APPIMAGE_NAME).exists()


def test_get_sh_reports_missing_release(tmp_path, release_server):
    root, base = release_server  # empty: nothing published
    home = tmp_path / "home"
    home.mkdir()

    res = _run_get_sh(home, base, version=None, api_base=base)
    assert res.returncode != 0
    assert "no published release found" in res.stderr
    assert "docs/BUILD.md" in res.stderr


def test_get_sh_reports_launch_failure(tmp_path, release_server):
    """If the app cannot start, the installer says so and shows the real error."""
    root, base = release_server
    _add_release(root, b'#!/bin/sh\necho "display exploded" >&2\nexit 3\n')
    home = tmp_path / "home"
    home.mkdir()

    res = _run_get_sh(home, base)
    assert res.returncode == 0, "installing succeeded even if the launch did not"
    assert "the app could not start" in res.stderr
    assert "display exploded" in res.stderr


def test_get_sh_source_fallback_on_other_arch(tmp_path, release_server):
    """Non-x86_64 machines install from the source archive instead."""
    root, base = release_server
    src = tmp_path / "srcpkg" / "tech-support-0.1.0"
    src.mkdir(parents=True)
    (src / "install.sh").write_text(
        '#!/bin/sh\n'
        'echo "$@" > "$HOME/install-args.marker"\n'
        'echo installed > "$HOME/source-installed.marker"\n'
    )
    (src / "run-customer.sh").write_text('#!/bin/sh\necho ran > "$HOME/launched.marker"\n')
    tar_path = root / REPO_PATH / "archive" / "refs" / "tags" / f"{TAG}.tar.gz"
    tar_path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tar_path, "w:gz") as tf:
        tf.add(src, arcname="tech-support-0.1.0")

    home = tmp_path / "home"
    home.mkdir()

    res = _run_get_sh(home, base, arch="aarch64")
    assert res.returncode == 0, res.stderr
    assert _wait_for(home / "source-installed.marker"), "source install.sh never ran"
    # install.sh was invoked without --no-launch (it handles the launch)
    assert (home / "install-args.marker").read_text().strip() == ""
    link = home / ".local/bin/tech-support"
    assert link.is_symlink()
    assert link.resolve() == (home / ".local/share/tech-support/run-customer.sh").resolve()
