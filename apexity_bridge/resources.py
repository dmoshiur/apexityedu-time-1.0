"""Frozen-aware resource and install-directory resolution.

When packaged with PyInstaller:
    sys.frozen is True
    sys.executable is ApexityEduBridge.exe
    sys._MEIPASS is the extracted/bundled data directory

Source checkouts keep the original layout (package dir + repo root).
"""
from __future__ import annotations

import sys
from pathlib import Path


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """Directory that contains the running executable (or the repo root)."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def meipass_dir() -> Path:
    """PyInstaller bundle dir, or the package directory when running from source."""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent


def resource_path(*parts: str) -> Path:
    """A file bundled next to the package (branding, templates, …)."""
    bundled = meipass_dir().joinpath(*parts)
    if bundled.exists():
        return bundled
    # Branding lives at the repo root in source checkouts.
    alt = app_dir().joinpath(*parts)
    if alt.exists():
        return alt
    return bundled


def icon_path() -> Path | None:
    for candidate in (
        resource_path("branding", "app-icon.ico"),
        resource_path("branding", "app-icon.png"),
        resource_path("branding", "logo-mark.png"),
        app_dir() / "ApexityEduBridge.exe",
    ):
        if candidate.exists():
            return candidate
    return None


def tray_icon_path() -> Path | None:
    for candidate in (
        resource_path("branding", "logo-mark.png"),
        resource_path("branding", "app-icon.png"),
        meipass_dir() / "apexity_bridge" / "dashboard" / "static" / "logo.png",
    ):
        if candidate.exists():
            return candidate
    return None
