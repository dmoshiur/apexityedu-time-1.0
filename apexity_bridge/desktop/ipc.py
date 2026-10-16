"""Loopback IPC so a second launch raises the already-running window.

No Qt dependency — the second process only needs the stdlib.
"""
from __future__ import annotations

import logging
import socket
import threading

log = logging.getLogger("apexity_bridge.desktop.ipc")

IPC_HOST = "127.0.0.1"
IPC_PORT = 18321
CMD_SHOW = b"SHOW\n"


def notify_running_instance() -> bool:
    """Ask the running app to show its window. True if someone listened."""
    try:
        with socket.create_connection((IPC_HOST, IPC_PORT), timeout=0.8) as sock:
            sock.sendall(CMD_SHOW)
        return True
    except OSError:
        return False


def start_ipc_server(on_show) -> threading.Thread:
    """Background thread: any connection means 'please show the window'."""

    def _serve() -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind((IPC_HOST, IPC_PORT))
            srv.listen(4)
            srv.settimeout(1.0)
        except OSError as exc:
            log.warning("desktop IPC bind failed: %s", exc)
            return
        log.info("desktop IPC listening on %s:%s", IPC_HOST, IPC_PORT)
        while True:
            try:
                conn, _addr = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                conn.settimeout(1.0)
                conn.recv(64)
                conn.close()
            except OSError:
                pass
            try:
                on_show()
            except Exception:
                log.exception("IPC on_show failed")

    thread = threading.Thread(target=_serve, name="desktop-ipc", daemon=True)
    thread.start()
    return thread
