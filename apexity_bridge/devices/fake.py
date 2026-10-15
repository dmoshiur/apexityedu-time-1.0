"""In-memory fake device adapter.

Used by:
* the automated test-suite (simulated K40 without hardware),
* development on machines with no device on the LAN,
* the acceptance-test protocol fixture (docs/ACCEPTANCE_TEST_PLAN.md #16).

It behaves like a real terminal: keeps its records across "restarts",
supports IP changes, offline/online simulation, and wrong comm keys.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .base import DeviceCapability, DeviceIdentity, DeviceUser, RawAttendanceRecord
from .exceptions import DeviceAuthError, DeviceConnectionError


class FakeDeviceAdapter:
    def __init__(self, host: str = "127.0.0.1", port: int = 4370, *,
                 comm_key: int = 0, timeout: int = 10,
                 serial: str = "FAKE-K40-0001", model: str = "K40",
                 firmware: str = "Ver 6.60 fake", platform: str = "ZMM220-TFT",
                 mac: str = "00:17:61:AA:00:01", online: bool = True,
                 expected_comm_key: int = 0, **_ignored):
        self.host = host
        self.port = int(port)
        self._comm_key = comm_key
        self._expected_key = expected_comm_key
        self.serial = serial
        self.model = model
        self.firmware = firmware
        self.platform = platform
        self.mac = mac
        self.online = online
        self.records: list[RawAttendanceRecord] = []
        self.users: list[DeviceUser] = []
        self._connected = False
        self.clock_skew_seconds = 0

    # lifecycle -------------------------------------------------------------
    def connect(self) -> None:
        if not self.online:
            raise DeviceConnectionError(f"fake device {self.host} is powered off")
        if self._comm_key != self._expected_key:
            raise DeviceAuthError("fake device: comm key rejected")
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.disconnect()
        return False

    def test_connection(self) -> bool:
        try:
            with self:
                return True
        except Exception:
            return False

    # data --------------------------------------------------------------------
    def get_identity(self) -> DeviceIdentity:
        if not self._connected:
            raise DeviceConnectionError("not connected")
        return DeviceIdentity(
            serial_number=self.serial,
            model=self.model,
            firmware_version=self.firmware,
            platform=self.platform,
            mac_address=self.mac,
            device_time=datetime.now() + timedelta(seconds=self.clock_skew_seconds),
        )

    def read_attendance(self) -> list[RawAttendanceRecord]:
        if not self._connected:
            raise DeviceConnectionError("not connected")
        return list(self.records)

    def read_users(self) -> list[DeviceUser]:
        if not self._connected:
            raise DeviceConnectionError("not connected")
        return list(self.users)

    def capabilities(self) -> DeviceCapability:
        return DeviceCapability(
            True, True, True, True, True, notes=("fake-adapter",),
        )

    # fixtures helpers ---------------------------------------------------------
    def add_punch(self, user_id: str, when: datetime, status: int = 0, punch: int = 1):
        """Simulate a physical fingerprint punch on the terminal."""
        self.records.append(
            RawAttendanceRecord(str(user_id), when.replace(microsecond=0), status, punch)
        )
        if not any(u.user_id == str(user_id) for u in self.users):
            self.users.append(
                DeviceUser(uid=len(self.users) + 1, user_id=str(user_id),
                           name=f"Device user {user_id}", privilege=0)
            )

    def set_offline(self, offline: bool = True):
        self.online = not offline
