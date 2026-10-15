"""Single-instance guard — the bridge must never run twice at once.

Windows: named global mutex via Win32 (CreateMutex).
POSIX (dev only): an flock'd lock file.
"""
from __future__ import annotations

import logging
import sys

log = logging.getLogger("apexity_bridge.windows_service.singleton")

MUTEX_NAME = "Global\\ApexityEduBridge-SingleInstance"


class SingleInstance:
    def __init__(self, name: str = MUTEX_NAME):
        self.name = name
        self._handle = None
        self._file = None

    def acquire(self) -> bool:
        if sys.platform.startswith("win"):
            return self._acquire_windows()
        return self._acquire_posix()

    def _acquire_windows(self) -> bool:
        import ctypes
        import ctypes.wintypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, False, self.name)
        if not handle:
            log.error("CreateMutex failed")
            return False
        ERROR_ALREADY_EXISTS = 183
        if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            return False
        self._handle = handle
        return True

    def _acquire_posix(self) -> bool:
        import fcntl
        import tempfile
        from pathlib import Path

        lock_path = Path(tempfile.gettempdir()) / "apexity-bridge.lock"
        self._file = open(lock_path, "w")
        try:
            fcntl.flock(self._file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        self._file.write(str(__import__("os").getpid()))
        self._file.flush()
        return True

    def release(self) -> None:
        if self._handle and sys.platform.startswith("win"):
            import ctypes
            ctypes.windll.kernel32.ReleaseMutex(self._handle)
            ctypes.windll.kernel32.CloseHandle(self._handle)
            self._handle = None
        if self._file:
            self._file.close()
            self._file = None

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError(
                "another ApexityEdu Bridge instance is already running — refusing to start"
            )
        return self

    def __exit__(self, *exc):
        self.release()
        return False
