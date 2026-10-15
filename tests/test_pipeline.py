"""Attendance pipeline: idempotency keys, timestamp validation, mapping."""
from __future__ import annotations

from datetime import datetime, timedelta

from apexity_bridge.attendance.pipeline import (
    build_event, compute_dedup_hash, validate_record_timestamp,
)
from apexity_bridge.devices.base import RawAttendanceRecord


def test_dedup_hash_stable_for_same_record():
    h1 = compute_dedup_hash("SN1", "7", "2026-10-15T08:00:00", 0, 1)
    h2 = compute_dedup_hash("SN1", "7", "2026-10-15T08:00:00", 0, 1)
    assert h1 == h2 and len(h1) == 64


def test_same_timestamp_different_users_are_distinct():
    """Requirement #9: multiple records sharing a timestamp must survive."""
    ts = "2026-10-15T08:00:00"
    hashes = {
        compute_dedup_hash("SN1", str(u), ts, 0, 1) for u in range(1, 6)
    }
    assert len(hashes) == 5


def test_record_uid_never_affects_hash():
    """pyzk 'uid' is a user slot, not a record id — excluded by design."""
    r = RawAttendanceRecord("7", datetime(2026, 10, 15, 8, 0, 0), 0, 1, record_uid=42)
    h = compute_dedup_hash("SN1", r.user_id, "2026-10-15T08:00:00", r.status, r.punch)
    r2 = RawAttendanceRecord("7", datetime(2026, 10, 15, 8, 0, 0), 0, 1, record_uid=43)
    h2 = compute_dedup_hash("SN1", r2.user_id, "2026-10-15T08:00:00", r2.status, r2.punch)
    assert h == h2


def test_hash_depends_on_all_fields():
    base = compute_dedup_hash("SN1", "7", "2026-10-15T08:00:00", 0, 1)
    assert base != compute_dedup_hash("SN2", "7", "2026-10-15T08:00:00", 0, 1)
    assert base != compute_dedup_hash("SN1", "8", "2026-10-15T08:00:00", 0, 1)
    assert base != compute_dedup_hash("SN1", "7", "2026-10-15T08:00:01", 0, 1)
    assert base != compute_dedup_hash("SN1", "7", "2026-10-15T08:00:00", 1, 1)
    assert base != compute_dedup_hash("SN1", "7", "2026-10-15T08:00:00", 0, 2)


def test_timestamp_validation():
    assert validate_record_timestamp(datetime(2026, 10, 15, 8, 0, 0)) is None
    assert validate_record_timestamp(datetime(1999, 1, 1)) is not None
    far_future = datetime.now() + timedelta(days=10)
    assert validate_record_timestamp(far_future) is not None
    assert validate_record_timestamp("2026-10-15") is not None


def test_build_event_resolves_mapping(repos):
    from conftest import seed_device, seed_students

    device = seed_device(repos)
    seed_students(repos)
    repos["mappings"].upsert(device_serial="SN-K40-001", device_user_id="1",
                             ems_student_id="S1001", mapped_by="t")

    def lookup(serial, user):
        row = repos["mappings"].get(serial, user)
        if row:
            st = repos["students"].get(row["ems_student_id"])
            row["student_active"] = st["active"] if st else None
        return row

    rec = RawAttendanceRecord("1", datetime(2026, 10, 15, 7, 55, 0), 0, 1)
    ev = build_event(rec, device=device, timezone_label="Asia/Dhaka", mapping_lookup=lookup)
    assert ev["ems_student_id"] == "S1001"
    assert ev["mapping_status"] == "resolved"
    assert ev["event_timestamp"] == "2026-10-15T07:55:00"     # verbatim
    assert ev["event_timestamp_tz"] == "Asia/Dhaka"

    rec2 = RawAttendanceRecord("999", datetime(2026, 10, 15, 7, 56, 0), 0, 1)
    ev2 = build_event(rec2, device=device, timezone_label="Asia/Dhaka",
                      mapping_lookup=lookup)
    assert ev2["mapping_status"] == "unmapped"
    assert ev2["ems_student_id"] is None


def test_build_event_flags_inactive_student(repos):
    from conftest import seed_device, seed_students

    device = seed_device(repos)
    seed_students(repos)
    repos["mappings"].upsert(device_serial="SN-K40-001", device_user_id="3",
                             ems_student_id="S1003", mapped_by="t")   # S1003 inactive

    def lookup(serial, user):
        row = repos["mappings"].get(serial, user)
        if row:
            st = repos["students"].get(row["ems_student_id"])
            row["student_active"] = st["active"] if st else None
        return row

    rec = RawAttendanceRecord("3", datetime(2026, 10, 15, 8, 0, 0), 0, 1)
    ev = build_event(rec, device=device, timezone_label="Asia/Dhaka", mapping_lookup=lookup)
    assert ev["mapping_status"] == "inactive_student"
    assert ev["ems_student_id"] == "S1003"      # still attached, EMS decides


def test_build_event_rejects_empty_user(repos):
    from conftest import seed_device
    device = seed_device(repos)
    rec = RawAttendanceRecord("  ", datetime(2026, 10, 15, 8, 0, 0), 0, 1)
    assert build_event(rec, device=device, timezone_label="Asia/Dhaka",
                       mapping_lookup=lambda s, u: None) is None
