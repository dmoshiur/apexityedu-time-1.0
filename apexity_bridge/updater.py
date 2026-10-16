"""Explicit, HTTPS-only updates from this project's public GitHub releases."""
from __future__ import annotations

import ctypes
import hashlib
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import requests

from . import __version__
from .paths import app_dir, is_frozen, is_windows

RELEASE_ROOT = "https://github.com/dmoshiur/apexityedu-time-1.0/releases/download/"
MANIFEST_URL = "https://github.com/dmoshiur/apexityedu-time-1.0/releases/latest/download/update.json"
MAX_INSTALLER_BYTES = 600 * 1024 * 1024


def version_tuple(value: str) -> tuple[int, int, int]:
    if not isinstance(value, str) or not re.fullmatch(r"\d+\.\d+\.\d+", value):
        raise ValueError("Invalid release version")
    return tuple(map(int, value.split(".")))


@dataclass(frozen=True)
class Update:
    version: str
    url: str
    sha256: str
    size: int


def check_update() -> Update | None:
    with requests.get(MANIFEST_URL, timeout=(10, 30)) as response:
        response.raise_for_status()
        if len(response.content) > 16384:
            raise ValueError("Update manifest is too large")
        data = response.json()
    version = data["version"]
    version_tuple(version)
    expected = f"{RELEASE_ROOT}v{version}/ApexityEduBridge-Setup-{version}.exe"
    if data.get("url") != expected:
        raise ValueError("Untrusted update URL")
    digest = data.get("sha256", "")
    size = data.get("size")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("Missing or invalid update checksum")
    if type(size) is not int or not 0 < size <= MAX_INSTALLER_BYTES:
        raise ValueError("Invalid installer size")
    if version_tuple(version) <= version_tuple(__version__):
        return None
    return Update(version, expected, digest, size)


def download_update(update: Update, progress=lambda percent: None) -> Path:
    # Revalidate even when called independently of check_update.
    version_tuple(update.version)
    if update.url != f"{RELEASE_ROOT}v{update.version}/ApexityEduBridge-Setup-{update.version}.exe":
        raise ValueError("Untrusted update URL")
    if not 0 < update.size <= MAX_INSTALLER_BYTES:
        raise ValueError("Invalid installer size")
    folder = Path(tempfile.mkdtemp(prefix="ApexityEdu-update-"))
    target = folder / "setup.exe"
    digest = hashlib.sha256()
    received = 0
    try:
        with requests.get(update.url, stream=True, timeout=(10, 60)) as response:
            response.raise_for_status()
            if not response.url.startswith("https://"):
                raise ValueError("Insecure download redirect")
            with target.open("wb") as output:
                for chunk in response.iter_content(256 * 1024):
                    if not chunk:
                        continue
                    received += len(chunk)
                    if received > update.size:
                        raise ValueError("Installer exceeds declared size")
                    output.write(chunk)
                    digest.update(chunk)
                    progress(int(received * 100 / update.size))
        if received != update.size or digest.hexdigest() != update.sha256:
            raise ValueError("Installer verification failed. Please retry the update.")
        return target
    except Exception:
        target.unlink(missing_ok=True)
        folder.rmdir()
        raise


def launch_update(installer: Path) -> None:
    if not is_windows() or not is_frozen():
        raise RuntimeError("In-app installation requires the installed Windows app.")
    # Inno Setup requests UAC elevation itself. Never use a shell for release data.
    arguments = subprocess.list2cmdline([
        "/SILENT", "/NORESTART", "/CLOSEAPPLICATIONS", "/UPDATE=1", f"/DIR={app_dir()}"])
    result = ctypes.windll.shell32.ShellExecuteW(  # type: ignore[attr-defined]
        None, "runas", str(installer), arguments, str(installer.parent), 1)
    if result <= 32:
        raise RuntimeError("Windows could not start the update, or permission was declined.")
