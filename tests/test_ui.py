"""Customer UI tests: the exact screen flow required by the shop."""

from __future__ import annotations

import os
import subprocess
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_customer_ui_self_test(xvfb_display):
    """Runs the real Tk app on the virtual display and checks every screen."""
    env = dict(os.environ)
    env["DISPLAY"] = xvfb_display
    proc = subprocess.run(
        [sys.executable, "-m", "app.customer_app",
         "--display", xvfb_display, "--self-test-ui"],
        cwd=PROJECT_ROOT, env=env,
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, f"UI self-test failed:\n{proc.stdout}\n{proc.stderr}"
    assert "SELF-TEST OK" in proc.stdout
    for line in ("tech check", "confirm code", "GETTING READY"):
        assert line in proc.stdout


def test_customer_app_reports_bad_display():
    proc = subprocess.run(
        [sys.executable, "-m", "app.customer_app",
         "--display", "127.0.0.1:199", "--self-test-ui"],
        cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode != 0
    assert "199" in proc.stderr  # the actual failing display is named
