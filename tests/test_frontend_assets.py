"""Static dashboard assets are part of the product: a syntax error in app.js
breaks every page's interactive controls (and used to blank the Settings
forms). Run the real JavaScript parser in CI when Node is available."""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
APP_JS = ROOT / "apexity_bridge" / "dashboard" / "static" / "app.js"
SETTINGS_HTML = ROOT / "apexity_bridge" / "dashboard" / "templates" / "settings.html"


def test_settings_page_javascript_blocks_are_not_duplicated():
    source = APP_JS.read_text(encoding="utf-8")
    # A bad merge left two `if (page === "settings") {` lines and one missing
    # closing brace, so the browser rejected the entire file.
    assert source.count('if (page === "settings")') == 1
    assert source.count("addEventListener(\"DOMContentLoaded\"") == 1


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_app_js_parses_with_node():
    result = subprocess.run(
        [shutil.which("node"), "--check", str(APP_JS)],
        text=True, capture_output=True,
    )
    assert result.returncode == 0, result.stderr


def test_every_settings_form_field_is_known_to_the_backend():
    """No dead inputs: each form control must be a DB-managed setting."""
    from apexity_bridge.config import DB_MANAGED_KEYS

    html = SETTINGS_HTML.read_text(encoding="utf-8")
    names = set(re.findall(r'name="([a-z_]+)"', html))
    names.discard("token")
    names.discard("current")
    names.discard("new")
    names.discard("confirm")
    assert names <= set(DB_MANAGED_KEYS), names - set(DB_MANAGED_KEYS)
