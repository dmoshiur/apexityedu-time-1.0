# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for ApexityEduBridge.exe (Windows onedir build)."""
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

SPECDIR = Path(SPEC).resolve().parent
ROOT = SPECDIR.parent
ICON = ROOT / "branding" / "app-icon.ico"
VERSION = SPECDIR / "file_version_info.txt"

datas = [
    (str(ROOT / "apexity_bridge" / "dashboard" / "templates"), "apexity_bridge/dashboard/templates"),
    (str(ROOT / "apexity_bridge" / "dashboard" / "static"), "apexity_bridge/dashboard/static"),
    (str(ROOT / "branding"), "branding"),
    (str(ROOT / ".env.example"), "."),
]
binaries = []
hiddenimports = [
    "waitress",
    "flask",
    "jinja2",
    "werkzeug",
    "dotenv",
    "requests",
    "urllib3",
    "certifi",
    "charset_normalizer",
    "idna",
    "psutil",
    "keyring",
    "keyring.backends.Windows",
    "keyring.backends.fail",
    "keyring.backends.null",
    "zk",
    "pystray",
    "pystray._win32",
    "PIL",
    "PIL.Image",
    "apexity_bridge",
    "apexity_bridge.__main__",
    "apexity_bridge.tray",
    "apexity_bridge.desktop",
    "apexity_bridge.desktop.gui",
    "apexity_bridge.desktop.facade",
    "apexity_bridge.dashboard.app",
    "apexity_bridge.windows_service.control",
    "PySide6",
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "shiboken6",
]

for pkg in ("apexity_bridge", "flask", "waitress", "zk", "keyring", "pystray", "PySide6"):
    try:
        extra_datas, extra_binaries, extra_hidden = collect_all(pkg)
        datas += extra_datas
        binaries += extra_binaries
        hiddenimports += extra_hidden
    except Exception:
        try:
            hiddenimports += collect_submodules(pkg)
        except Exception:
            pass

a = Analysis(
    [str(SPECDIR / "entry.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "tkinter", "PySide6.QtWebEngine", "PySide6.QtWebEngineCore",
              "PySide6.QtWebEngineWidgets", "PySide6.Qt3DCore", "PySide6.QtQuick",
              "PySide6.QtQml", "PySide6.QtMultimedia"],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ApexityEduBridge",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ICON) if ICON.exists() else None,
    version=str(VERSION) if VERSION.exists() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="ApexityEduBridge",
)
