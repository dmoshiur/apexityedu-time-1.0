"""Sync worker: offline-first behavior, backoff, idempotent retries, roster.

These tests exercise the exact failure modes of the acceptance plan:
internet down/restored, EMS down, restart with pending rows, endpoint
rejection, auth expiry, duplicate re-pushes after crashes.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from apexity_bridge.sync.worker import SyncWorker
from apexity_bridge.timeutils import iso_utc, parse_iso, utcnow
from conftest import seed_device
from fakes import FakeIclockServer, FakeRestServer


def _make_events(repos, device, n=3, start_hour=8):
    ids = []
    for i in range(n):
        ids.extend(repos["events"].insert_new([{
            "dedup_hash": f"dh-{device['serial_number']}-{i}-{start_hour}",
            "device_id": device["id"],
            "device_serial": device["serial_number"],
            "device_user_id": str(i + 1),
            "event_timestamp": f"2026-10-15T{start_hour:02d}:0{i}:00",
            "event_timestamp_tz": "Asia/Dhaka",
            "punch": 1, "status": i, "verify_mode": 1,
            "mapping_status": "unmapped",
        }]))
    return ids


def _worker(ctx, url, protocol, token=None):
    ctx.repos["settings"].set("ems_base_url", url)
    ctx.repos["settings"].set("ems_protocol", protocol)
    if token is not None:
        ctx.secret_store.set_ems_token(token)
    worker = SyncWorker(ctx)
    return worker


@pytest.fixture()
def rest_server():
    server = FakeRestServer()
    yield server
    server.stop()


@pytest.fixture()
def iclock_server():
    server = FakeIclockServer()
    yield server
    server.stop()


# ------------------------------------------------------------- connectivity --
def test_offline_then_online_full_recovery(ctx, rest_server):
    """Requirement #5/#6: internet down → queue grows; restored → drains."""
    device = seed_device(ctx.repos)
    ids = _make_events(ctx.repos, device, 4)
    worker = _worker(ctx, rest_server.url, "rest", FakeRestServer.TOKEN)

    rest_server.mode = "http500"                     # EMS unreachable
    worker._next_health = 0
    worker._tick()
    assert not worker.status.online
    assert ctx.repos["events"].counts()["pending"] == 4   # still safely queued

    rest_server.mode = "ok"                          # internet/EMS restored
    worker._next_health = 0
    worker._tick()
    worker._tick()                                   # process queue + push
    counts = ctx.repos["events"].counts()
    assert counts["synced"] == 4
    assert worker.status.online
    rows = ctx.repos["events"].by_ids(ids)
    assert all(r["remote_id"] for r in rows)
    assert ctx.repos["settings"].get("ems_last_ok")


def test_failed_push_retries_with_backoff_then_succeeds(ctx, rest_server):
    device = seed_device(ctx.repos)
    _make_events(ctx.repos, device, 2)
    worker = _worker(ctx, rest_server.url, "rest", FakeRestServer.TOKEN)
    worker.status.update(online=True)                # pretend health is fine

    rest_server.mode = "http500"
    assert worker.push_due_events() == 0
    rows = ctx.repos["events"].due_for_sync(iso_utc(), 10)
    assert rows == []                                # snoozed, not lost
    first = ctx.repos["events"].by_ids([1])[0]
    assert first["sync_status"] == "failed"
    assert first["retry_count"] == 1
    assert parse_iso(first["next_retry_at"]) > utcnow()

    rest_server.mode = "ok"
    ctx.repos["events"].retry_now([1, 2])            # admin hits "retry selected"
    assert worker.push_due_events() == 2
    assert ctx.repos["events"].counts()["synced"] == 2


def test_crash_between_accept_and_mark_repushes_safely(ctx, rest_server):
    """Requirement #7/#8: duplicate protection across restarts."""
    device = seed_device(ctx.repos)
    ids = _make_events(ctx.repos, device, 2)
    worker = _worker(ctx, rest_server.url, "rest", FakeRestServer.TOKEN)
    worker.status.update(online=True)
    assert worker.push_due_events() == 2

    # simulate crash: the EMS accepted the events, but the local mark-synced
    # update was lost (process died in between) → rows are pending again
    assert ids
    marks = ",".join("?" * len(ids))
    ctx.repos["db"].execute(
        f"UPDATE attendance_events SET sync_status='pending', synced_at=NULL, remote_id=NULL "
        f"WHERE id IN ({marks})", ids)
    again = worker.push_due_events()
    assert again == 2                                # duplicates resolved as synced
    assert rest_server.push_calls == 2
    assert len(rest_server.seen) == 2                # server side: no double records

    attempts = ctx.repos["queue"].recent_attempts(5)
    assert any(a["result"] in ("success", "partial") for a in attempts)


def test_auth_failure_penalty_and_no_spam(ctx, rest_server):
    device = seed_device(ctx.repos)
    _make_events(ctx.repos, device, 1)
    worker = _worker(ctx, rest_server.url, "rest", token="EXPIRED-TOKEN")
    worker.status.update(online=True)

    worker.push_due_events()
    row = ctx.repos["events"].by_ids([1])[0]
    assert row["sync_status"] == "failed"
    assert parse_iso(row["next_retry_at"]) > utcnow()  # long penalty delay
    assert not worker.status.online
    audits = ctx.repos["audit"].list(action="ems.auth_failed")[0]
    assert len(audits) == 1
    worker.push_due_events()                           # second pass in same process…
    audits = ctx.repos["audit"].list(action="ems.auth_failed")[0]
    assert len(audits) == 1                            # …does not spam the audit log


def test_rejected_events_not_retried(ctx, rest_server):
    device = seed_device(ctx.repos)
    ctx.repos["events"].insert_new([{
        "dedup_hash": "bad-1", "device_id": device["id"],
        "device_serial": device["serial_number"], "device_user_id": "",
        "event_timestamp": "", "event_timestamp_tz": "Asia/Dhaka",
        "punch": 1, "status": 0, "verify_mode": 1, "mapping_status": "unmapped",
    }])
    worker = _worker(ctx, rest_server.url, "rest", FakeRestServer.TOKEN)
    worker.status.update(online=True)
    worker.push_due_events()
    row = ctx.repos["events"].by_ids([1])[0]
    assert row["sync_status"] == "rejected"
    worker.push_due_events()
    assert rest_server.push_calls == 1                 # no automatic retry


def test_iclock_push_groups_batches_per_serial(ctx, iclock_server):
    dev_a = seed_device(ctx.repos, serial="SN-A", ip="192.168.0.201")
    dev_b = seed_device(ctx.repos, serial="SN-B", ip="192.168.0.202")
    _make_events(ctx.repos, dev_a, 2)
    _make_events(ctx.repos, dev_b, 2, start_hour=9)
    worker = _worker(ctx, iclock_server.url, "iclock")
    worker.status.update(online=True)

    n = worker.push_due_events()
    assert n == 4
    sns = {rec["sn"] for rec in iclock_server.accepted}
    assert sns == {"SN-A", "SN-B"}
    assert ctx.repos["events"].counts()["synced"] == 4


def test_iclock_partial_batch_retries_leftovers(ctx, iclock_server):
    device = seed_device(ctx.repos)
    _make_events(ctx.repos, device, 3)
    iclock_server.mode = "partial"                   # server counts only 1
    worker = _worker(ctx, iclock_server.url, "iclock")
    worker.status.update(online=True)

    worker.push_due_events()
    counts = ctx.repos["events"].counts()
    assert counts["synced"] == 1
    assert counts["failed"] == 2                     # will retry with backoff


# ------------------------------------------------------------------- roster --
def test_roster_refresh_via_queue(ctx, rest_server):
    seed_device(ctx.repos)
    worker = _worker(ctx, rest_server.url, "rest", FakeRestServer.TOKEN)
    worker.status.update(online=True)

    ctx.repos["queue"].enqueue("fetch_students")
    worker._process_queue()
    assert ctx.repos["students"].count() == 3        # full pagination consumed
    assert ctx.repos["students"].get("S1003")["active"] == 0
    assert ctx.repos["settings"].get("roster_last_sync")
    assert worker.status.roster_supported


def test_roster_unsupported_is_surfaced_not_faked(ctx, iclock_server):
    """Requirement: never invent students — report the missing API plainly."""
    worker = _worker(ctx, iclock_server.url, "iclock")
    worker.status.update(online=True)
    ctx.repos["queue"].enqueue("fetch_students")
    worker._process_queue()
    assert ctx.repos["students"].count() == 0
    assert not worker.status.roster_supported
    assert "students" in (ctx.repos["settings"].get("roster_last_error") or "").lower() \
        or "roster" in (ctx.repos["settings"].get("roster_last_error") or "").lower()


def test_device_registration_task(ctx, rest_server):
    device = seed_device(ctx.repos, serial="SN-REG")
    worker = _worker(ctx, rest_server.url, "rest", FakeRestServer.TOKEN)
    worker.status.update(online=True)

    worker._register_devices()
    assert ctx.repos["settings"].get("ems_registered:SN-REG") == "1"
    assert rest_server.registered_devices[0]["serial"] == "SN-REG"

    worker._register_devices()                       # idempotent — no re-post
    assert len(rest_server.registered_devices) == 1
