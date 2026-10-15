"""Student mapping service: assign/unassign, CSV, conflict detection."""
from __future__ import annotations

from datetime import datetime

import pytest

from apexity_bridge.devices.base import RawAttendanceRecord
from apexity_bridge.attendance.pipeline import build_event
from apexity_bridge.students.mapping import MappingError, MappingService
from conftest import seed_device, seed_students


def _make_event(repo, device, user_id, ts):
    rec = RawAttendanceRecord(user_id, ts, 0, 1)
    ev = build_event(rec, device=device, timezone_label="Asia/Dhaka",
                     mapping_lookup=lambda s, u: repo["mappings"].get(s, u))
    ids = repo["events"].insert_new([ev])
    return ids[0]


@pytest.fixture()
def setup(repos):
    device = seed_device(repos)
    seed_students(repos)
    service = MappingService(repos, audit_actor="tester")
    return repos, device, service


def test_assign_and_re_resolve_unsent_events(setup):
    repos, device, service = setup
    eid = _make_event(repos, device, "1", datetime(2026, 10, 15, 8, 0, 0))
    assert repos["events"].by_ids([eid])[0]["mapping_status"] == "unmapped"

    service.assign(device_serial="SN-K40-001", device_user_id="1", ems_student_id="S1001")
    row = repos["events"].by_ids([eid])[0]
    assert row["mapping_status"] == "resolved"
    assert row["ems_student_id"] == "S1001"


def test_synced_history_is_not_rewritten(setup):
    repos, device, service = setup
    eid = _make_event(repos, device, "1", datetime(2026, 10, 15, 8, 0, 0))
    repos["events"].mark_synced([eid])
    service.assign(device_serial="SN-K40-001", device_user_id="1", ems_student_id="S1002")
    row = repos["events"].by_ids([eid])[0]
    assert row["ems_student_id"] is None        # history preserved


def test_mapping_survives_ip_change(setup):
    """Mappings key on device serial, never on mutable IPs."""
    repos, device, service = setup
    service.assign(device_serial="SN-K40-001", device_user_id="1", ems_student_id="S1001")
    repos["devices"].update_verified_ip(device["id"], "192.168.0.222")
    mapping = repos["mappings"].get("SN-K40-001", "1")
    assert mapping is not None and mapping["ems_student_id"] == "S1001"


def test_unassign_restores_unmapped(setup):
    repos, device, service = setup
    eid = _make_event(repos, device, "1", datetime(2026, 10, 15, 8, 0, 0))
    mid = service.assign(device_serial="SN-K40-001", device_user_id="1",
                         ems_student_id="S1001")
    service.unassign(mid)
    row = repos["events"].by_ids([eid])[0]
    assert row["mapping_status"] == "unmapped"
    assert row["ems_student_id"] is None


def test_refuse_unknown_student(setup):
    repos, device, service = setup
    with pytest.raises(MappingError, match="not in the EMS roster"):
        service.assign(device_serial="SN-K40-001", device_user_id="1",
                       ems_student_id="NOPE")
    with pytest.raises(MappingError):
        service.assign(device_serial="SN-K40-001", device_user_id="1",
                       ems_student_id="")


def test_conflict_detection(setup):
    repos, device, service = setup
    service.assign(device_serial="SN-K40-001", device_user_id="1", ems_student_id="S1001")
    service.assign(device_serial="SN-K40-001", device_user_id="2", ems_student_id="S1001")
    conflicts = repos["mappings"].conflicts()
    assert len(conflicts) == 1
    assert conflicts[0]["ems_student_id"] == "S1001"
    assert conflicts[0]["n"] == 2


def test_mapping_to_inactive_student_flagged(setup):
    repos, device, service = setup
    service.assign(device_serial="SN-K40-001", device_user_id="3", ems_student_id="S1003")
    report = service.review_report()
    assert any(m["ems_student_id"] == "S1003" for m in report["unknown_or_inactive_students"])


def test_unmapped_device_users_report(setup):
    repos, device, service = setup
    _make_event(repos, device, "42", datetime(2026, 10, 15, 8, 0, 0))
    report = service.review_report()
    users = report["unmapped_device_users"]
    assert len(users) == 1
    assert users[0]["device_user_id"] == "42"
    assert users[0]["events"] == 1


def test_csv_export_import_roundtrip(setup, tmp_path):
    repos, device, service = setup
    service.assign(device_serial="SN-K40-001", device_user_id="1", ems_student_id="S1001")
    service.assign(device_serial="SN-K40-001", device_user_id="2", ems_student_id="S1002")
    csv_text = service.export_csv()
    assert "device_serial,device_user_id,ems_student_id" in csv_text.splitlines()[0]
    assert "SN-K40-001,1,S1001" in csv_text

    result = service.import_csv(csv_text)
    assert result["imported"] == 2
    assert repos["mappings"].count() == 2


def test_csv_import_validates_and_aborts_atomically(setup):
    repos, device, service = setup
    bad_header = "serial,user,student\nSN-K40-001,1,S1001\n"
    with pytest.raises(MappingError, match="header"):
        service.import_csv(bad_header)

    unknown_student = "device_serial,device_user_id,ems_student_id\nSN-K40-001,1,S1001\nSN-K40-001,2,GHOST\n"
    with pytest.raises(MappingError, match="row 3"):
        service.import_csv(unknown_student)
    # aborted: even the valid row 2 must NOT be applied
    assert repos["mappings"].count() == 0

    dup_inside = ("device_serial,device_user_id,ems_student_id\n"
                  "SN-K40-001,1,S1001\nSN-K40-001,1,S1002\n")
    with pytest.raises(MappingError, match="duplicate"):
        service.import_csv(dup_inside)

    missing_fields = "device_serial,device_user_id,ems_student_id\nSN-K40-001,,S1001\n"
    with pytest.raises(MappingError, match="required"):
        service.import_csv(missing_fields)


def test_import_never_creates_students(setup):
    repos, device, service = setup
    csv_text = ("device_serial,device_user_id,ems_student_id\n"
                "SN-K40-001,1,BUILD-ME-NEW\n")
    with pytest.raises(MappingError):
        service.import_csv(csv_text)
    assert repos["students"].get("BUILD-ME-NEW") is None
