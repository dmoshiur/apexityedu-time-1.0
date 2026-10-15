"""Database layer: schema, migrations, transactions, repositories."""
from __future__ import annotations

import sqlite3
import threading

import pytest

from apexity_bridge.database.connection import Database
from apexity_bridge.database.migrations import CURRENT_VERSION, migrate
from apexity_bridge.database.repositories import open_migrated_database
from apexity_bridge.timeutils import iso_utc, seconds_from_now


def test_migrations_are_idempotent(tmp_path):
    db, _ = open_migrated_database(tmp_path / "b.db")
    migrate(db)
    migrate(db)
    row = db.query_one("SELECT MAX(version) AS v, COUNT(*) AS c FROM schema_migrations")
    assert row["v"] == CURRENT_VERSION
    assert row["c"] == CURRENT_VERSION
    db.close_all()


def test_all_required_tables_exist(repos):
    tables = {r["name"] for r in repos["db"].query(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    required = {
        "devices", "students_cache", "attendance_events", "sync_queue",
        "sync_attempts", "application_settings", "audit_logs",
        "device_user_mappings", "discovery_observations", "schema_migrations",
    }
    assert required <= tables


def test_integrity_check_ok(repos):
    assert repos["db"].check_integrity()


def test_transaction_rolls_back_on_error(repos):
    db = repos["db"]
    with pytest.raises(ValueError):
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO application_settings (key, value, is_secret, updated_at) VALUES ('k1','v1',0,'now')"
            )
            raise ValueError("boom")
    assert db.query_one("SELECT value FROM application_settings WHERE key='k1'") is None


def test_device_serial_is_unique_when_present(repos):
    repos["devices"].upsert_identified(serial_number="AA1", model="K40")
    did, created = repos["devices"].upsert_identified(serial_number="AA1", model="K40")
    assert created is False
    with pytest.raises(sqlite3.IntegrityError):
        with repos["db"].transaction() as conn:
            conn.execute("INSERT INTO devices (serial_number, status, first_seen_at, created_at, updated_at) VALUES ('AA1','pending','n','n','n')")


def test_multiple_unidentified_devices_allowed(repos):
    """Pending rows without serial (identification failed) must coexist."""
    a = repos["devices"].create_pending(ip_address="10.0.0.5", model="unknown")
    b = repos["devices"].create_pending(ip_address="10.0.0.6", model="unknown")
    assert a != b


def test_register_requires_serial(repos):
    did = repos["devices"].create_pending(ip_address="10.0.0.5")
    with pytest.raises(ValueError, match="serial"):
        repos["devices"].register(did)


def test_remove_refused_when_events_exist(repos):
    did, _ = repos["devices"].upsert_identified(serial_number="AA1")
    repos["devices"].register(did)
    ev = _event(device_id=did, serial="AA1")
    repos["events"].insert_new([ev])
    with pytest.raises(ValueError, match="attendance history"):
        repos["devices"].remove(did)
    repos["devices"].set_status(did, "disabled")
    assert repos["devices"].get_by_id(did)["status"] == "disabled"


def _event(**kw):
    base = dict(
        dedup_hash="h" + iso_utc().replace(":", "") + str(kw.get("device_id", "")) + str(kw.get("n", "")),
        device_id=kw.get("device_id", 1),
        device_serial=kw.get("serial", "AA1"),
        device_user_id=kw.get("user", "1"),
        event_timestamp="2026-10-15T08:00:00",
        event_timestamp_tz="Asia/Dhaka",
        punch=1, status=0, verify_mode=1,
        mapping_status="unmapped",
    )
    base.update({k: v for k, v in kw.items() if k != "n"})
    return base


def test_event_insert_dedups_by_hash(repos):
    did, _ = repos["devices"].upsert_identified(serial_number="AA1")
    ev = _event(device_id=did)
    ids1 = repos["events"].insert_new([ev])
    ids2 = repos["events"].insert_new([dict(ev)])
    assert len(ids1) == 1
    assert ids2 == []                      # exact duplicate ignored
    assert repos["events"].counts()["pending"] == 1


def test_event_sync_lifecycle(repos):
    did, _ = repos["devices"].upsert_identified(serial_number="AA1")
    ids = repos["events"].insert_new([_event(device_id=did)])

    due = repos["events"].due_for_sync(iso_utc(), 10)
    assert [e["id"] for e in due] == ids

    repos["events"].mark_attempt_failed(ids, "connection refused", seconds_from_now(3600))
    assert repos["events"].due_for_sync(iso_utc(), 10) == []     # snoozed
    assert repos["events"].due_for_sync(seconds_from_now(7200), 10)     # due later

    repos["events"].retry_now(ids)
    assert [e["id"] for e in repos["events"].due_for_sync(iso_utc(), 10)] == ids

    repos["events"].mark_synced(ids, remote_ids={ids[0]: "7788"})
    row = repos["events"].by_ids(ids)[0]
    assert row["sync_status"] == "synced"
    assert row["remote_id"] == "7788"
    assert row["synced_at"]
    assert repos["events"].due_for_sync(iso_utc(), 10) == []


def test_escalates_to_review_after_retries(repos):
    did, _ = repos["devices"].upsert_identified(serial_number="AA1")
    ids = repos["events"].insert_new([_event(device_id=did)])
    for _ in range(repos["events"].REVIEW_AFTER_ATTEMPTS):
        repos["events"].mark_attempt_failed(ids, "err", seconds_from_now(60))
    row = repos["events"].by_ids(ids)[0]
    assert row["sync_status"] == "review"
    assert repos["events"].due_for_sync(iso_utc(), 10) == []     # human must look at it


def test_queue_retry_escalation(repos):
    tid = repos["queue"].enqueue("fetch_students", {}, max_attempts=2)
    due = repos["queue"].due(iso_utc())
    assert [t["id"] for t in due] == [tid]
    repos["queue"].fail(tid, "err1", seconds_from_now(60))
    assert repos["queue"].due(iso_utc()) == []
    assert repos["queue"].due(seconds_from_now(61))
    repos["queue"].fail(tid, "err2", seconds_from_now(60))
    row = repos["db"].query_one("SELECT status, attempts FROM sync_queue WHERE id=?", (tid,))
    assert row["status"] == "failed" and row["attempts"] == 2


def test_queue_enqueue_once(repos):
    first = repos["queue"].enqueue_once("register_device")
    second = repos["queue"].enqueue_once("register_device")
    assert first is not None and second is None
    other = repos["queue"].enqueue_once("fetch_students")
    assert other is not None


def test_crash_safety_wal_mode(repos, tmp_path):
    """Second connection sees committed data (WAL); db survives process crash."""
    db2 = Database(tmp_path / "bridge.db")
    repos["settings"].set("k", "v")
    assert db2.query_one("SELECT value FROM application_settings WHERE key='k'")["value"] == "v"
    db2.close_all()


def test_threads_share_db_safely(repos):
    """Concurrent writers must serialise instead of corrupting state."""
    errors = []

    def writer(n):
        try:
            for i in range(25):
                repos["settings"].set(f"t{n}-{i}", str(i))
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert repos["settings"].get("t3-24") == "24"


def test_backup_contains_data(repos, tmp_path):
    repos["settings"].set("important", "yes")
    backup = repos["db"].backup_to(tmp_path / "bk.db")
    check = Database(backup)
    row = check.query_one("SELECT value FROM application_settings WHERE key='important'")
    assert row["value"] == "yes"
    check.close_all()


def test_corrupt_db_quarantined_and_recreated(tmp_path):
    bad = tmp_path / "bridge.db"
    bad.write_bytes(b"this is definitely not a sqlite database" * 10)
    db, repos = open_migrated_database(bad)
    quarantined = list(tmp_path.glob("bridge.db.corrupt-*"))
    assert quarantined, "corrupt file must be preserved, never deleted"
    assert quarantined[0].read_bytes().startswith(b"this is definitely")
    assert repos["db"].check_integrity()
    db.close_all()
