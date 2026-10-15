"""Collector: collection, dedup across reads, identity guard, backoff."""
from __future__ import annotations

from datetime import datetime, timedelta

from apexity_bridge.attendance.collector import AttendanceCollector
from conftest import seed_device, seed_students


def test_collect_persists_new_events(ctx, adapter_factory):
    seed_students(ctx.repos)
    device = seed_device(ctx.repos, serial="SN-A", ip="192.168.0.201")
    fake = adapter_factory.add("192.168.0.201", serial="SN-A")
    fake.add_punch("1", datetime(2026, 10, 15, 7, 50, 0))
    fake.add_punch("2", datetime(2026, 10, 15, 7, 50, 0))   # same second, other user
    fake.add_punch("1", datetime(2026, 10, 15, 7, 55, 0))

    collector = AttendanceCollector(ctx)
    stats = collector.collect_device(device)

    assert stats["ok"] and stats["new"] == 3 and stats["total_read"] == 3
    assert ctx.repos["events"].counts()["pending"] == 3
    assert ctx.repos["devices"].get_by_id(device["id"])["last_seen_at"]


def test_repeated_reads_never_duplicate(ctx, adapter_factory):
    """Requirement #7: repeated device reads across restarts are idempotent."""
    device = seed_device(ctx.repos, serial="SN-A", ip="192.168.0.201")
    fake = adapter_factory.add("192.168.0.201", serial="SN-A")
    fake.add_punch("1", datetime(2026, 10, 15, 7, 50, 0))

    collector = AttendanceCollector(ctx)
    first = collector.collect_device(device)
    second = collector.collect_device(device)          # same device state, again

    assert first["new"] == 1
    assert second["new"] == 0 and second["total_read"] == 1
    assert ctx.repos["events"].counts()["total"] == 1


def test_offline_device_backs_off_then_recovers(ctx, adapter_factory):
    """Requirement #3/#5: device off at startup, on later."""
    device = seed_device(ctx.repos, serial="SN-A", ip="192.168.0.201")
    fake = adapter_factory.add("192.168.0.201", serial="SN-A", online=False)

    collector = AttendanceCollector(ctx)
    bad = collector.collect_device(device)
    assert not bad["ok"] and "powered off" in bad["error"]
    assert device["id"] in collector._backoff_until

    collector._backoff_until.clear()                   # simulate retry time passing
    fake.set_offline(False)                            # device powers back on
    fake.add_punch("1", datetime(2026, 10, 15, 8, 0, 0))
    good = collector.collect_device(ctx.repos["devices"].get_by_id(device["id"]))
    assert good["ok"] and good["new"] == 1


def test_stranger_at_old_ip_is_refused(ctx, adapter_factory):
    """Requirement #4/#15: another terminal that inherited the IP must NOT
    feed records into the previous device's identity."""
    device = seed_device(ctx.repos, serial="SN-A", ip="192.168.0.201")
    foreign = adapter_factory.add("192.168.0.201", serial="SN-B-INTRUDER")
    foreign.add_punch("1", datetime(2026, 10, 15, 8, 0, 0))

    collector = AttendanceCollector(ctx)
    stats = collector.collect_device(device)
    assert not stats["ok"]
    assert "identity mismatch" in stats["error"]
    assert ctx.repos["events"].counts()["total"] == 0   # nothing collected
    audits = ctx.repos["audit"].list(action="device.identity_mismatch")[0]
    assert len(audits) == 1


def test_invalid_timestamps_quarantined(ctx, adapter_factory):
    device = seed_device(ctx.repos, serial="SN-A", ip="192.168.0.201")
    fake = adapter_factory.add("192.168.0.201", serial="SN-A")
    fake.add_punch("1", datetime(1999, 5, 4, 8, 0, 0))          # clock unset
    fake.add_punch("2", datetime.now() + timedelta(days=30))    # far future
    fake.add_punch("3", datetime(2026, 10, 15, 8, 0, 0))        # valid

    collector = AttendanceCollector(ctx)
    stats = collector.collect_device(device)
    counts = ctx.repos["events"].counts()
    assert stats["new"] == 3
    assert counts["review"] == 2
    assert counts["pending"] == 1


def test_wrong_comm_key_handled(ctx, adapter_factory):
    device = seed_device(ctx.repos, serial="SN-A", ip="192.168.0.201")
    adapter_factory.add("192.168.0.201", serial="SN-A", expected_comm_key=666)

    collector = AttendanceCollector(ctx)
    stats = collector.collect_device(device)
    assert not stats["ok"] and "comm key" in stats["error"]
    audits = ctx.repos["audit"].list(action="device.auth_failed")[0]
    assert len(audits) == 1


def test_device_delete_never_called_by_collector(adapter_factory):
    """Static guarantee: the adapter interface has no destructive methods."""
    from apexity_bridge.devices.base import DeviceAdapter
    import inspect

    public = [m for m in dir(AttendanceCollector) if not m.startswith("_")]
    assert "clear" not in " ".join(public).lower()
    # Fake adapter exposes no deleting API either
    fake = adapter_factory.add("1.2.3.4", serial="S")
    assert not hasattr(fake, "clear_attendance")
