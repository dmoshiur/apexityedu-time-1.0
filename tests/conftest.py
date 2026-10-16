"""Shared fixtures: temp DB, app context, fake devices, fake EMS servers."""
from __future__ import annotations

import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from apexity_bridge.config import Config, SettingsView, load_config
from apexity_bridge.database.repositories import open_migrated_database
from apexity_bridge.devices.fake import FakeDeviceAdapter
from apexity_bridge.devices.exceptions import DeviceConnectionError
from apexity_bridge.security.secrets import SecretStore
from apexity_bridge.service import BridgeContext
from apexity_bridge.sync.worker import EmsStatus


@pytest.fixture()
def repos(tmp_path):
    db, repos = open_migrated_database(tmp_path / "bridge.db")
    yield repos
    db.close_all()


@pytest.fixture()
def config(tmp_path):
    return Config(
        ems_base_url="http://127.0.0.1:9",   # overridden per test
        ems_protocol="auto",
        ems_allow_insecure_localhost=True,
        db_path=tmp_path / "bridge.db",
        data_dir=tmp_path,
        log_dir=tmp_path / "logs",
        log_level="DEBUG",
        device_timeout=3,
        collect_interval=5,
        sync_retry_base=5,
        sync_retry_max=60,
        roster_interval=60,
        sync_batch_size=50,
    )


class FakeAdapterFactory:
    """Maps IP addresses to FakeDeviceAdapter instances (simulated LAN)."""

    def __init__(self):
        self.devices: dict[str, FakeDeviceAdapter] = {}

    def add(self, ip: str, **kwargs) -> FakeDeviceAdapter:
        device = FakeDeviceAdapter(host=ip, **kwargs)
        self.devices[ip] = device
        return device

    def move(self, old_ip: str, new_ip: str) -> None:
        self.devices[new_ip] = self.devices.pop(old_ip)
        self.devices[new_ip].host = new_ip

    def __call__(self, host: str, port: int = 4370, **kwargs):
        """Construction never fails (like the real factory); connection does."""
        device = self.devices.get(host)
        if device is None:
            device = FakeDeviceAdapter(host=host, online=False)
            self.devices[host] = device
        device.host = host
        device.port = port
        device._comm_key = kwargs.get("comm_key", 0)
        return device


@pytest.fixture()
def adapter_factory():
    return FakeAdapterFactory()


@pytest.fixture()
def ctx(config, repos, adapter_factory):
    context = BridgeContext(
        version="test",
        config=config,
        settings=SettingsView(config, repos["settings"]),
        repos=repos,
        secret_store=SecretStore.in_memory(),
        ems_status=EmsStatus(),
        stop_event=threading.Event(),
        adapter_factory=adapter_factory,
    )
    yield context
    context.stop_event.set()


@pytest.fixture()
def iclock_server():
    """A fake ZKTeco iClock/ADMS server (real HTTP, ephemeral port)."""
    from fakes import FakeIclockServer
    server = FakeIclockServer()
    yield server
    server.stop()


@pytest.fixture()
def rest_server():
    """A fake EMS that speaks the documented REST bridge contract."""
    from fakes import FakeRestServer
    server = FakeRestServer()
    yield server
    server.stop()


def seed_device(repos, *, serial="SN-K40-001", ip="192.168.0.201",
                status="registered", label="Gate K40"):
    device_id, _ = repos["devices"].upsert_identified(
        serial_number=serial, model="K40", ip_address=ip,
    )
    repos["devices"].update_verified_ip(device_id, ip)
    if status == "registered":
        repos["devices"].register(device_id, label=label)
    else:
        repos["devices"].set_status(device_id, status)
    return repos["devices"].get_by_id(device_id)


def seed_students(repos, students=None):
    students = students or [
        {"ems_student_id": "S1001", "name": "Afnan Rahman", "admission_no": "ADM-001",
         "roll_no": "12", "class_name": "Six", "section": "A", "active": True},
        {"ems_student_id": "S1002", "name": "Bithi Akter", "admission_no": "ADM-002",
         "roll_no": "7", "class_name": "Six", "section": "B", "active": True},
        {"ems_student_id": "S1003", "name": "Chinmoy Das", "admission_no": "ADM-003",
         "roll_no": "3", "class_name": "Seven", "section": "A", "active": False},
    ]
    repos["students"].upsert_many(students)
    return students
