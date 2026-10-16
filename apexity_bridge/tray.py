"""Launch the native desktop app + background service (never a web browser).

Double-clicking ApexityEduBridge.exe starts the bridge in the background,
shows a tray icon, and opens the native Qt window. A second launch raises
the existing window. Closing the window hides it — the service keeps running
and reconnects devices after a PC restart via Task Scheduler auto-start.
"""
from __future__ import annotations

import logging

from .desktop.ipc import notify_running_instance
from .windows_service.singleton import SingleInstance

log = logging.getLogger("apexity_bridge.tray")

APP_TITLE = "ApexityEdu Attendance Bridge"


def run_tray(config, *, open_browser: bool = True) -> int:
    """Start the bridge + native desktop UI. ``open_browser`` now means
    'show the window' (kept for CLI flag compatibility)."""
    try:
        with SingleInstance():
            return _run_with_lock(config, show_window=open_browser)
    except RuntimeError:
        log.info("another instance is already running — raising its window")
        if notify_running_instance():
            return 0
        log.warning("running instance did not answer IPC")
        return 0


def _run_with_lock(config, *, show_window: bool) -> int:
    from .service import BridgeService

    # Native desktop window replaces the Flask browser dashboard.
    service = BridgeService(config, start_dashboard=False)
    service.start()
    try:
        from .desktop import run_desktop
        return run_desktop(service, show_window=show_window)
    except ImportError:
        log.exception("PySide6 is required for the desktop application")
        print("PySide6 is not installed. Install it with:")
        print("    pip install PySide6")
        service.stop()
        return 1
