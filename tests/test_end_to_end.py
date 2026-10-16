"""Full end-to-end: real service threads, fake K40 on a fake LAN, fake EMS.

Scenario: install → register device → students punch while "internet" is
down → internet restored → everything arrives exactly once.
"""
from __future__ import annotations

import time
from datetime import datetime

import pytest

from apexity_bridge.config import load_config
from apexity_bridge.service import BridgeService
from conftest import FakeAdapterFactory
from fakes import FakeRestServer


def _wait_for(predicate, timeout=20.0, interval=0.25):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


@pytest.mark.slow
def test_bridge_end_to_end_offline_recovery(tmp_path, monkeypatch):
    server = FakeRestServer()
    monkeypatch.setenv("BRIDGE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("EMS_BASE_URL", server.url)
    monkeypatch.setenv("EMS_PROTOCOL", "rest")
    monkeypatch.setenv("DASHBOARD_PORT", "0")     # ephemeral — no port conflicts
    monkeypatch.setenv("COLLECT_INTERVAL", "5")
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))

    from apexity_bridge.discovery.service import DiscoveryService, ScanReport
    from apexity_bridge.timeutils import iso_utc
    monkeypatch.setattr(
        DiscoveryService, "run_scan",
        lambda self, **k: ScanReport(started_at=iso_utc(), finished_at=iso_utc()),
    )

    factory = FakeAdapterFactory()

    # -- internet is DOWN at startup -----------------------------------------
    server.mode = "http500"
    config = load_config()
    service = BridgeService(config, adapter_factory=factory)
    ctx = service.start()
    try:
        ctx.secret_store.set_ems_token(FakeRestServer.TOKEN)

        # register the K40 the admin found and plug punches into the fake LAN
        device_id, _ = ctx.repos["devices"].upsert_identified(
            serial_number="SN-E2E", model="K40", ip_address="10.0.0.9")
        ctx.repos["devices"].update_verified_ip(device_id, "10.0.0.9")
        ctx.repos["devices"].register(device_id, label="Gate")
        ctx.collector.wake()

        fake = factory.add("10.0.0.9", serial="SN-E2E")
        fake.add_punch("7", datetime(2026, 10, 15, 7, 50, 0))
        fake.add_punch("8", datetime(2026, 10, 15, 7, 50, 0))
        fake.add_punch("7", datetime(2026, 10, 15, 12, 5, 0))

        # events must be collected to SQLite even with EMS down --------------
        assert _wait_for(lambda: ctx.repos["events"].counts()["total"] == 3)
        time.sleep(1.5)
        assert ctx.repos["events"].counts()["synced"] == 0
        assert server.push_calls == 0  # unreachable — nothing sent

        # -- internet comes back: EMS reachable again -------------------------
        server.mode = "ok"
        assert _wait_for(lambda: ctx.repos["events"].counts()["synced"] == 3,
                         timeout=30)
        assert ctx.repos["events"].counts()["pending"] == 0
        assert len(server.seen) == 3            # exactly-once at the server
        assert ctx.ems_status.online
        assert ctx.repos["settings"].get("ems_registered:SN-E2E") == "1"

        # the people directory also flowed in through the queue
        # (3 students + 2 teachers + 1 staff member)
        assert _wait_for(lambda: ctx.repos["students"].count() == 6, timeout=15)

        # a fourth punch afterwards syncs on the normal cadence
        fake.add_punch("9", datetime(2026, 10, 15, 12, 6, 0))
        assert _wait_for(lambda: ctx.repos["events"].counts()["synced"] == 4,
                         timeout=30)
        assert len(server.seen) == 4
    finally:
        service.stop()
        server.stop()

    # after shutdown the DB must still hold everything (offline persistence)
    from apexity_bridge.database.repositories import open_migrated_database
    db, repos = open_migrated_database(config.db_path)
    counts = repos["events"].counts()
    assert counts["synced"] == 4
    assert repos["students"].count() == 6
    db.close_all()
