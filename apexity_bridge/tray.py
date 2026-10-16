"""Windows system-tray shell around the bridge service.

Double-clicking ApexityEduBridge.exe starts the bridge, shows a tray icon
with the product logo, and opens the local dashboard. A second launch
simply focuses the dashboard instead of starting a duplicate process.
"""
from __future__ import annotations

import logging
import threading
import webbrowser
from pathlib import Path

from .resources import tray_icon_path
from .windows_service.singleton import SingleInstance

log = logging.getLogger("apexity_bridge.tray")

APP_TITLE = "ApexityEdu Attendance Bridge"


def dashboard_url(config) -> str:
    host = config.dashboard_host
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    return f"http://{host}:{config.dashboard_port}"


def open_dashboard(config) -> None:
    url = dashboard_url(config)
    try:
        webbrowser.open(url)
    except Exception as exc:  # pragma: no cover - browser missing
        log.warning("could not open dashboard %s: %s", url, exc)


def run_tray(config, *, open_browser: bool = True) -> int:
    """Start the bridge + tray icon. Returns a process exit code."""
    try:
        with SingleInstance():
            return _run_with_lock(config, open_browser=open_browser)
    except RuntimeError:
        log.info("another instance is already running — opening the dashboard")
        open_dashboard(config)
        return 0


def _run_with_lock(config, *, open_browser: bool) -> int:
    from .service import BridgeService

    service = BridgeService(config)
    service.start()
    if open_browser:
        # Give waitress a moment to bind before the browser hits it.
        threading.Timer(0.8, lambda: open_dashboard(config)).start()

    try:
        return _pystray_loop(config, service)
    except Exception:
        log.exception("system tray unavailable — running in the background without it")
        import time
        try:
            while service.ctx and not service.ctx.stop_event.is_set():
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            service.stop()
        return 0


def _pystray_loop(config, service) -> int:
    import pystray
    from PIL import Image

    icon_file = tray_icon_path()
    if icon_file and Path(icon_file).exists():
        image = Image.open(icon_file).convert("RGBA")
    else:
        image = Image.new("RGBA", (64, 64), (14, 165, 233, 255))

    def on_open(icon, item):  # noqa: ARG001
        open_dashboard(config)

    def on_logs(icon, item):  # noqa: ARG001
        folder = Path(config.log_dir) if config.log_dir else Path(config.data_dir) / "logs"
        folder.mkdir(parents=True, exist_ok=True)
        _open_folder(folder)

    def on_quit(icon, item):  # noqa: ARG001
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("Open dashboard", on_open, default=True),
        pystray.MenuItem("Open logs folder", on_logs),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Exit", on_quit),
    )
    icon = pystray.Icon("ApexityEduBridge", image, APP_TITLE, menu)
    log.info("tray icon ready — dashboard %s", dashboard_url(config))
    icon.run()
    service.stop()
    return 0


def _open_folder(path: Path) -> None:
    import os
    import sys

    try:
        if sys.platform.startswith("win"):
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            webbrowser.open(path.as_uri())
    except OSError as exc:
        log.warning("could not open %s: %s", path, exc)


