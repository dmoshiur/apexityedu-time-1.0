"""Device adapter abstraction.

The bridge NEVER depends on a specific vendor SDK in its business logic.
Collectors and discovery talk only to this interface, so a different ZKTeco
SDK (or the official ZKEM Win32 SDK) can be dropped in later without touching
sync/dedup code.

Hard rules every adapter must follow:
* Read-only by contract: ``read_attendance`` must never clear device data.
* Identity comes from protocol responses (serial number first), never from IP.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Protocol


@dataclass(frozen=True)
class DeviceIdentity:
    """Strong identity signals reported by the terminal itself."""

    serial_number: str | None = None
    model: str | None = None            # e.g. "K40" — may be a name string
    firmware_version: str | None = None
    platform: str | None = None
    mac_address: str | None = None      # network-level signal; may be None/spoofed
    device_name: str | None = None
    device_time: datetime | None = None  # clock as reported by the terminal

    def has_strong_identity(self) -> bool:
        return bool(self.serial_number)


@dataclass(frozen=True)
class RawAttendanceRecord:
    """One punch exactly as reported by the terminal (no interpretation)."""

    user_id: str             # device PIN — NOT the EMS student id
    timestamp: datetime      # device-local naive time as reported
    status: int | None       # device check state code (firmware dependent)
    punch: int | None        # verification method code (firmware dependent)
    record_uid: int | None = None   # internal slot/uid if firmware exposes one (diagnostics only)

    def raw_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "timestamp": self.timestamp.isoformat(timespec="seconds"),
            "status": self.status,
            "punch": self.punch,
            "record_uid": self.record_uid,
        }


@dataclass(frozen=True)
class DeviceUser:
    uid: int | None
    user_id: str
    name: str | None
    privilege: int | None
    card: int | None = None


@dataclass(frozen=True)
class DeviceCapability:
    can_read_attendance: bool = True
    can_read_users: bool = True
    can_read_serial: bool = True
    can_read_mac: bool = False
    can_read_time: bool = True
    notes: tuple[str, ...] = ()


class DeviceAdapter(Protocol):
    """Contract every terminal adapter implements."""

    host: str
    port: int

    def connect(self) -> None: ...
    def disconnect(self) -> None: ...
    def test_connection(self) -> bool: ...

    def get_identity(self) -> DeviceIdentity: ...
    def read_attendance(self) -> list[RawAttendanceRecord]: ...
    def read_users(self) -> list[DeviceUser]: ...
    def capabilities(self) -> DeviceCapability: ...


AdapterFactory = Callable[[str, int], DeviceAdapter]
