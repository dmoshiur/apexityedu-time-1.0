"""ZKTeco K40 adapter built on the maintained pure-Python ``pyzk`` library.

Protocol reality, verified against the installed pyzk source (zk/base.py):

* Communicates with the ZK push SDK protocol on UDP/TCP 4370
  (pyzk auto-selects TCP when the port answers TCP, else UDP).
* ``get_attendance()`` yields ``Attendance(user_id, timestamp, status, punch, uid)``
  where ``uid`` is the *enrol table slot of the user* — NOT a unique record id.
  Therefore the bridge does NOT use it as an idempotency key (see
  attendance/pipeline.py); it is kept for diagnostics only.
* Commands we NEVER call (destructive/administrative):
  clear_attendance, clear_data, delete_user, delete_user_template, poweroff,
  restart, set_user, enroll_user, set_time (unless explicitly triggered by an
  admin through a guarded code path — the bridge ships no such path). The pyzk
  handle is kept private so these never leak into bridge code.
* Fingerprint templates are never read (get_templates is not used).
"""
from __future__ import annotations

import logging
import socket
from typing import Any

from .base import (
    DeviceCapability,
    DeviceIdentity,
    DeviceUser,
    RawAttendanceRecord,
)
from .exceptions import (
    DeviceAuthError,
    DeviceCommandError,
    DeviceConnectionError,
    DeviceError,
    DeviceTimeoutError,
)

log = logging.getLogger("apexity_bridge.devices.zk")


def _import_zk():
    try:
        from zk import ZK  # pyzk
        from zk import exception as zk_exc
        return ZK, zk_exc
    except ImportError as exc:  # pragma: no cover - environment issue
        raise DeviceError(
            "pyzk is not installed. Run: pip install -r requirements.txt"
        ) from exc


def _translate(exc: Exception, host: str) -> DeviceError:
    """Map pyzk/socket errors onto the bridge's typed DeviceError tree."""
    text = str(exc)
    low = text.lower()
    name = type(exc).__name__
    if "unauthenticated" in low or "invalid session" in low:
        return DeviceAuthError(f"device {host} rejected the comm key: {text}")
    if "timed out" in low or isinstance(exc, socket.timeout) or "timeout" in low:
        return DeviceTimeoutError(f"device {host} timed out: {text}")
    if name in ("ZKErrorConnection", "ZKNetworkError") or isinstance(
        exc, (ConnectionError, OSError)
    ):
        return DeviceConnectionError(f"cannot reach device {host}: {text}")
    if name == "ZKErrorResponse":
        return DeviceCommandError(f"device {host} command error: {text}")
    return DeviceError(f"device {host} error: {text}")


class ZkDeviceAdapter:
    DEFAULT_PORT = 4370

    def __init__(self, host: str, port: int = DEFAULT_PORT, *, comm_key: int = 0,
                 timeout: int = 10, force_udp: bool = False, ommit_ping: bool = True):
        self.host = host
        self.port = int(port)
        self._comm_key = int(comm_key or 0)
        self._timeout = int(timeout)
        self._force_udp = force_udp
        self._ommit_ping = ommit_ping
        self._zk: Any | None = None   # private: destructive commands stay unreachable
        self._conn: Any | None = None

    # -- lifecycle ----------------------------------------------------------
    def _handle(self):
        return self._conn

    def connect(self) -> None:
        ZK, _ = _import_zk()
        try:
            self._zk = ZK(
                self.host, port=self.port, timeout=self._timeout,
                password=self._comm_key, force_udp=self._force_udp,
                ommit_ping=self._ommit_ping,
            )
            self._conn = self._zk.connect()
        except DeviceError:
            raise
        except Exception as exc:
            self._conn = None
            raise _translate(exc, self.host) from exc

    def disconnect(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.disconnect()
            except Exception as exc:  # pragma: no cover - best effort
                log.debug("disconnect from %s raised %s (ignored)", self.host, exc)

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc_info):
        self.disconnect()
        return False

    def is_connected(self) -> bool:
        return self._conn is not None

    def _require(self):
        if self._conn is None:
            raise DeviceConnectionError(f"not connected to {self.host}")
        return self._conn

    def test_connection(self) -> bool:
        try:
            with self:
                return True
        except DeviceError:
            return False

    # -- identity -----------------------------------------------------------
    def get_identity(self) -> DeviceIdentity:
        """Read identity signals; tolerant: a failing signal never hides others.

        A missing serial number, however, is a hard failure — IP/MAC alone are
        never acceptable proof of device identity.
        """
        conn = self._require()

        def safe(callable_, label):
            try:
                value = callable_()
                return value if value not in ("", 0) else None
            except Exception as exc:
                log.debug("identity signal %s failed on %s: %s", label, self.host, exc)
                return None

        try:
            serial = conn.get_serialnumber()
        except Exception as exc:
            raise _translate(exc, self.host) from exc
        serial = (serial or "").strip() or None
        if not serial:
            raise DeviceCommandError(
                f"device {self.host} did not report a serial number — cannot "
                f"identify it safely (never identify by IP alone)"
            )

        return DeviceIdentity(
            serial_number=serial,
            model=safe(conn.get_device_name, "device_name")
                  or safe(conn.get_platform, "platform"),
            firmware_version=safe(conn.get_firmware_version, "firmware"),
            platform=safe(conn.get_platform, "platform"),
            mac_address=safe(conn.get_mac, "mac"),
            device_time=safe(conn.get_time, "device_time"),
        )

    # -- data (read-only) ----------------------------------------------------
    def read_attendance(self) -> list[RawAttendanceRecord]:
        """Download the full attendance log. Never deletes device data."""
        conn = self._require()
        locked = False
        try:
            try:
                conn.disable_device()   # pause keypad/UI interference while reading
                locked = True
            except Exception as exc:
                log.debug("disable_device failed on %s (%s) — reading anyway", self.host, exc)
            records = conn.get_attendance() or []
        except Exception as exc:
            raise _translate(exc, self.host) from exc
        finally:
            if locked:
                try:
                    conn.enable_device()
                except Exception as exc:
                    log.warning("enable_device failed on %s: %s", self.host, exc)

        out: list[RawAttendanceRecord] = []
        for rec in records:
            try:
                out.append(
                    RawAttendanceRecord(
                        user_id=str(getattr(rec, "user_id", "")).strip(),
                        timestamp=rec.timestamp,
                        status=getattr(rec, "status", None),
                        punch=getattr(rec, "punch", None),
                        record_uid=getattr(rec, "uid", None),
                    )
                )
            except AttributeError as exc:
                log.warning("skipping malformed attendance record from %s: %s", self.host, exc)
        return out

    def read_users(self) -> list[DeviceUser]:
        conn = self._require()
        try:
            users = conn.get_users() or []
        except Exception as exc:
            raise _translate(exc, self.host) from exc
        return [
            DeviceUser(
                uid=u.uid,
                user_id=str(u.user_id).strip(),
                name=(u.name or "").strip() or None,
                privilege=u.privilege,
                card=u.card or None,
            )
            for u in users
        ]

    def capabilities(self) -> DeviceCapability:
        conn = self._require()
        can_mac = False
        try:
            can_mac = bool(conn.get_mac())
        except Exception:
            can_mac = False
        return DeviceCapability(
            can_read_attendance=True,
            can_read_users=True,
            can_read_serial=True,
            can_read_mac=can_mac,
            can_read_time=True,
            notes=("pyzk/ZK-protocol@4370",),
        )
