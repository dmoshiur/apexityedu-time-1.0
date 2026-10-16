"""Native desktop GUI (PySide6). Never opens a web browser."""

from __future__ import annotations


def run_desktop(service, *, show_window: bool = True) -> int:
    from .gui import run_desktop as _run
    return _run(service, show_window=show_window)
