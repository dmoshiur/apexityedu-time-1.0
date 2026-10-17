"""Regression tests for the three field failures this release fixes.

1. "the bridge shows punches but never the user"  → Device Users page/cache.
2. "everything says OK but nothing reaches ApexityEdu" → ingestion proof, and
   the EMS-serial override when the school registered the device under a
   different serial than the terminal reports.
3. "I want students, teachers and staff pushed from ApexityEdu" → the people
   directory (person_type) over both protocols.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from apexity_bridge.database.repositories import ems_serial_for
from apexity_bridge.ems.iclock import (
    IclockEmsClient,
    command_is_read_only,
    command_verb,
    parse_commands,
)

format_user_line = IclockEmsClient.format_user_line
from apexity_bridge.sync.worker import SyncWorker
from conftest import seed_device
from fakes import FakeIclockServer, FakeRestServer


@pytest.fixture()
def iclock_server():
    server = FakeIclockServer()
    yield server
    server.stop()


@pytest.fixture()
def rest_server():
    server = FakeRestServer()
    yield server
    server.stop()


def _worker(ctx, url, protocol, token=None):
    ctx.repos["settings"].set("ems_base_url", url)
    ctx.repos["settings"].set("ems_protocol", protocol)
    if token is not None:
        ctx.secret_store.set_ems_token(token)
    worker = SyncWorker(ctx)
    ctx.sync_worker = worker          # ctx.ems_client reads through the worker
    return worker


def _events(repos, device, user_id="7", n=1):
    ids = []
    for i in range(n):
        ids.extend(repos["events"].insert_new([{
            "dedup_hash": f"du-{device['serial_number']}-{user_id}-{i}",
            "device_id": device["id"],
            "device_serial": device["serial_number"],
            "device_user_id": user_id,
            "event_timestamp": f"2026-10-15T08:0{i}:00",
            "event_timestamp_tz": "Asia/Dhaka",
            "punch": 1, "status": 0, "verify_mode": 1,
            "mapping_status": "unmapped",
        }]))
    return ids


def _seed_users(repos, serial, users=(("1", "Rahim"), ("2", "Karim Sir"), ("3", "Bacchu"))):
    return repos["device_users"].replace_for(
        serial, [{"user_id": u, "name": n, "privilege": 0} for u, n in users]
    )


# ----------------------------------------------------------- 1. device users --
def test_collector_caches_device_users(ctx, adapter_factory):
    """Requirement: the bridge must show who the K40 knows about."""
    from apexity_bridge.attendance.collector import AttendanceCollector

    device = seed_device(ctx.repos, serial="SN-USERS", ip="10.0.0.55")
    fake = adapter_factory.add("10.0.0.55", serial="SN-USERS")
    fake.add_punch("7", datetime(2026, 10, 15, 8, 0, 0))
    fake.add_punch("2", datetime(2026, 10, 15, 8, 5, 0))

    collector = AttendanceCollector(ctx)
    stats = collector.collect_device(device)

    assert stats["ok"]
    assert stats["users"]["on_device"] == 2          # add_punch auto-enrols users
    assert ctx.repos["device_users"].count() == 2
    rows, total = ctx.repos["device_users"].list()
    assert total == 2
    assert {r["user_id"] for r in rows} == {"7", "2"}
    assert rows[0]["device_label"] == "Gate K40"


def test_device_user_cache_tracks_removals_and_mappings(ctx):
    device = seed_device(ctx.repos, serial="SN-CACHE")
    _seed_users(ctx.repos, "SN-CACHE")
    assert ctx.repos["device_users"].count() == 3
    assert ctx.repos["device_users"].unmapped_count() == 3

    ctx.repos["students"].upsert_many(
        [{"ems_student_id": "T1", "name": "Karim Sir", "person_type": "teacher"}]
    )
    ctx.repos["mappings"].upsert(device_serial="SN-CACHE", device_user_id="2",
                                 ems_student_id="T1", mapped_by="admin")
    assert ctx.repos["device_users"].unmapped_count() == 2

    rows, _ = ctx.repos["device_users"].list(device_serial="SN-CACHE")
    mapped = next(r for r in rows if r["user_id"] == "2")
    assert mapped["person_name"] == "Karim Sir"
    assert mapped["person_type"] == "teacher"
    assert mapped["mapping_id"]

    # the terminal is the source of truth: a deleted user disappears
    result = _seed_users(ctx.repos, "SN-CACHE", users=(("1", "Rahim"),))
    assert result["removed"] == 2
    assert ctx.repos["device_users"].count() == 1


def test_users_for_ems_never_includes_passwords(ctx):
    _seed_users(ctx.repos, "SN-PRIV")
    payload = ctx.repos["device_users"].users_for_ems("SN-PRIV")
    assert all(set(u) <= {"user_id", "name", "privilege", "card"} for u in payload)


# ------------------------------------------------- 2. user table → EMS (iClock) --
def test_iclock_user_table_format_and_upload(iclock_server):
    client = IclockEmsClient(iclock_server.url)
    line = format_user_line({"user_id": "7", "name": "Rahim", "privilege": 0, "card": "12"})
    assert line.startswith("USER PIN=7")
    assert "Name=Rahim" in line
    assert "Passwd=\t" in line                     # never a device password

    outcome = client.push_users("SN-A", [
        {"user_id": "7", "name": "Rahim", "privilege": 0},
        {"user_id": "2", "name": "Karim Sir", "privilege": 0},
    ])
    assert outcome.fatal is None
    assert len(iclock_server.user_uploads) == 1
    assert iclock_server.user_uploads[0]["sn"] == "SN-A"
    assert iclock_server.user_uploads[0]["users"] == {"7": "Rahim", "2": "Karim Sir"}


def test_wire_lines_survive_hostile_names_and_pins():
    """A tab/newline must never split one record into two on the server."""
    attlog = IclockEmsClient.format_attlog_line({
        "device_user_id": "7\t8", "event_timestamp": "2026-10-15T08:00:00",
        "status": 0, "verify_mode": 1,
    })
    assert attlog.count("\t") == 6
    assert "\n" not in attlog

    user = IclockEmsClient.format_user_line({
        "user_id": "7\t8", "name": "Rahim\nKarim", "privilege": 0,
    })
    assert user.count("\t") == 6
    assert "PIN=7 8" in user
    assert "Name=Rahim Karim" in user


def test_iclock_user_upload_rejects_non_ok_reply(iclock_server):
    iclock_server.user_reply = "<html>login</html>"
    client = IclockEmsClient(iclock_server.url)
    outcome = client.push_users("SN-A", [{"user_id": "1", "name": "x"}])
    assert outcome.fatal and "USER" in outcome.fatal


def test_worker_publishes_users_and_skips_unchanged(ctx, iclock_server):
    device = seed_device(ctx.repos, serial="SN-USYNC")
    _seed_users(ctx.repos, "SN-USYNC")
    worker = _worker(ctx, iclock_server.url, "iclock")
    worker.status.update(online=True)

    worker._sync_device_users()
    assert len(iclock_server.user_uploads) == 1
    assert ctx.repos["devices"].get_by_id(device["id"])["users_pushed_hash"]

    worker._sync_device_users()                    # unchanged → no re-push
    assert len(iclock_server.user_uploads) == 1

    _seed_users(ctx.repos, "SN-USYNC", users=(("1", "Rahim"), ("9", "New person")))
    worker._sync_device_users()
    assert len(iclock_server.user_uploads) == 2
    assert "9" in iclock_server.user_uploads[1]["users"]


def test_rest_client_can_publish_users(rest_server):
    from apexity_bridge.ems.rest import RestEmsClient

    client = RestEmsClient(rest_server.url,
                           token_provider=lambda: FakeRestServer.TOKEN)
    outcome = client.push_users("SN-A", [{"user_id": "1", "name": "Rahim"}])
    assert outcome.fatal is None
    assert rest_server.device_user_uploads[0]["serial"] == "SN-A"


# -------------------------------------------------- 3. EMS serial override --
def test_ems_serial_override_is_used_for_every_push(ctx, iclock_server):
    """The #1 cause of 'collected but never uploaded': serial mismatch."""
    device = seed_device(ctx.repos, serial="HW-SERIAL-1")
    repos = ctx.repos
    repos["devices"].set_ems_serial(device["id"], "APEX-REGISTERED-9")
    device = repos["devices"].get_by_id(device["id"])
    assert ems_serial_for(device) == "APEX-REGISTERED-9"

    _events(repos, device, user_id="7", n=2)
    _seed_users(repos, "HW-SERIAL-1")
    worker = _worker(ctx, iclock_server.url, "iclock")
    worker.status.update(online=True)

    worker.push_due_events()
    assert iclock_server.attlog_posts
    assert {p["sn"] for p in iclock_server.attlog_posts} == {"APEX-REGISTERED-9"}

    worker._sync_device_users()
    assert iclock_server.user_uploads[0]["sn"] == "APEX-REGISTERED-9"

    worker._health_check()
    assert "APEX-REGISTERED-9" in iclock_server.handshakes
    assert "APEX-REGISTERED-9" in iclock_server.polls
    presence = worker.status.snapshot()["device_presence"]["HW-SERIAL-1"]
    assert presence["ems_serial"] == "APEX-REGISTERED-9"
    assert presence["override"] is True


def test_fallback_to_hardware_serial_when_no_override(ctx, iclock_server):
    device = seed_device(ctx.repos, serial="HW-ONLY")
    _events(ctx.repos, device, n=1)
    worker = _worker(ctx, iclock_server.url, "iclock")
    worker.status.update(online=True)
    worker.push_due_events()
    assert iclock_server.attlog_posts[0]["sn"] == "HW-ONLY"


# ------------------------------------------------- 4. honest ingestion proof --
def test_handshake_success_is_not_ingestion_proof(ctx, iclock_server):
    """'Everything says OK' — the dashboard must not claim more than it knows."""
    device = seed_device(ctx.repos, serial="SN-TRUTH")
    worker = _worker(ctx, iclock_server.url, "iclock")

    worker._health_check()
    assert worker.status.online                    # handshake only
    assert worker.status.ingestion_verified is False

    _events(ctx.repos, device, n=2)
    worker.push_due_events()
    assert worker.status.ingestion_verified is True
    assert worker.status.last_push_at


def test_probe_attlog_path_reports_exact_server_reply(iclock_server):
    client = IclockEmsClient(iclock_server.url)
    probe = client.probe_attlog_path("SN-A")
    assert probe["ok"] is True                     # fake server answers OK
    assert probe["status"] == 200
    assert not iclock_server.attlog_posts[0]["lines"]   # no fake punch written

    iclock_server.mode = "error"
    probe = client.probe_attlog_path("SN-A")
    assert probe["ok"] is False
    assert "ERROR" in (probe["reply"] or "")


def test_verify_report_is_honest_when_serial_unknown(ctx, iclock_server):
    from apexity_bridge.ems.verify import format_report, verify_ems_ingestion

    seed_device(ctx.repos, serial="SN-VERIFY")
    worker = _worker(ctx, iclock_server.url, "iclock")
    iclock_server.rejected_serials.add("SN-VERIFY")

    report = verify_ems_ingestion(ctx, push_users=False)
    assert report["ok"] is False
    assert "not accepted" in report["verdict"].lower() or "handshake" in report["verdict"].lower()
    assert "SN-VERIFY" in format_report(report)


# --------------------------------------------------- 5. ADMS command handling --
def test_command_parsing_and_read_only_classification():
    assert parse_commands("OK") == []
    assert parse_commands("C:1:DATA QUERY USERINFO") == ["DATA QUERY USERINFO"]
    assert command_verb("C:2:data query attlog") == "DATA QUERY ATTLOG"
    assert command_is_read_only("C:1:DATA QUERY USERINFO")
    assert command_is_read_only("CHECK")
    assert not command_is_read_only("C:3:CLEAR DATA")
    assert not command_is_read_only("C:4:REBOOT")
    assert not command_is_read_only("C:5:DELETE USER PIN=1")
    assert not command_is_read_only("C:6:SET TIME 2026-10-15 10:00:00")


def test_read_only_commands_are_answered_destructive_are_refused(ctx, iclock_server):
    device = seed_device(ctx.repos, serial="SN-CMD")
    _seed_users(ctx.repos, "SN-CMD")
    worker = _worker(ctx, iclock_server.url, "iclock")
    worker.status.update(online=True)

    worker._handle_commands(device, ["DATA QUERY USERINFO"])
    assert len(iclock_server.user_uploads) == 1

    worker._handle_commands(device, ["CLEAR DATA", "REBOOT", "DATA QUERY BIODATA"])
    assert len(iclock_server.user_uploads) == 1      # nothing destructive ran
    refused = [a for a in _audit(ctx) if a["action"] == "ems.command_refused"]
    assert {r["detail"].split(" —")[0] for r in refused} >= {"'CLEAR DATA'", "'REBOOT'"}

    seen = worker.status.snapshot()["commands_seen"]
    assert "DATA QUERY USERINFO" in seen


def test_attlog_replay_answers_data_query_attlog(ctx, iclock_server):
    device = seed_device(ctx.repos, serial="SN-REPLAY")
    _events(ctx.repos, device, user_id="7", n=2)
    worker = _worker(ctx, iclock_server.url, "iclock")
    worker.status.update(online=True)

    worker._handle_commands(device, ["DATA QUERY ATTLOG"])
    assert iclock_server.attlog_posts
    assert len(iclock_server.attlog_posts[0]["lines"]) == 2
    assert ctx.repos["events"].counts()["synced"] == 2


def test_commands_can_be_disabled(ctx, iclock_server):
    device = seed_device(ctx.repos, serial="SN-OFF")
    _seed_users(ctx.repos, "SN-OFF")
    worker = _worker(ctx, iclock_server.url, "iclock")
    ctx.repos["settings"].set("ems_execute_commands", "0")
    worker.status.update(online=True)
    worker._handle_commands(device, ["DATA QUERY USERINFO"])
    assert iclock_server.user_uploads == []


def test_read_only_commands_are_acknowledged(ctx, iclock_server):
    device = seed_device(ctx.repos, serial="SN-ACK")
    _seed_users(ctx.repos, "SN-ACK")
    worker = _worker(ctx, iclock_server.url, "iclock")
    worker.status.update(online=True)

    worker._handle_commands(device, ["C:41:DATA QUERY USERINFO"])

    assert len(iclock_server.user_uploads) == 1
    ack = iclock_server.command_acks[-1]
    assert ack["sn"] == "SN-ACK"
    assert ack["fields"]["ID"] == "41"
    assert ack["fields"]["Return"] == "0"
    assert ack["fields"]["CMD"] == "DATA QUERY USERINFO"
    assert "DATA QUERY USERINFO" in worker.status.snapshot()["commands_acknowledged"]


def test_repeated_refused_command_is_audited_once_and_acked_as_failure(ctx, iclock_server):
    device = seed_device(ctx.repos, serial="SN-REFUSE")
    worker = _worker(ctx, iclock_server.url, "iclock")
    worker.status.update(online=True)

    for _ in range(3):
        worker._handle_commands(device, ["C:99:REBOOT"])

    refused = [a for a in _audit(ctx) if a["action"] == "ems.command_refused"]
    assert len(refused) == 1                       # one audit, not one per poll
    stats = worker.status.snapshot()["commands_refused"]["REBOOT"]
    assert stats["count"] == 3
    assert len(iclock_server.command_acks) == 3
    assert all(a["fields"]["Return"] == "-1" for a in iclock_server.command_acks)
    assert iclock_server.accepted == []


def test_biometric_query_is_acknowledged_as_unsupported(ctx, iclock_server):
    device = seed_device(ctx.repos, serial="SN-BIO")
    worker = _worker(ctx, iclock_server.url, "iclock")
    worker.status.update(online=True)

    worker._handle_commands(device, ["C:5:DATA QUERY BIODATA"])

    assert iclock_server.command_acks[-1]["fields"]["Return"] == "-1004"
    unsupported = worker.status.snapshot()["commands_unsupported"]
    assert "DATA QUERY BIODATA" in unsupported


def _audit(ctx):
    rows, _ = ctx.repos["audit"].list(limit=100)
    return rows


# ---------------------------------------------- 6. people, not just students --
def test_people_directory_covers_teachers_and_staff(ctx, rest_server):
    from apexity_bridge.students.directory import refresh_people

    worker = _worker(ctx, rest_server.url, "rest", FakeRestServer.TOKEN)
    stats = refresh_people(worker._client, ctx.repos)

    assert stats["fetched"] == 6
    assert stats["by_type"]["teacher"] == 2
    assert stats["by_type"]["staff"] == 1
    counts = ctx.repos["students"].counts_by_type()
    assert counts["student"] == 3 and counts["teacher"] == 2 and counts["staff"] == 1
    teacher = ctx.repos["students"].get("T2001")
    assert teacher["designation"] == "Headmaster"
    assert teacher["employee_no"] == "EMP-01"


def test_people_list_filters_by_type(ctx):
    from conftest import seed_students

    seed_students(ctx.repos)
    ctx.repos["students"].upsert_many([
        {"ems_student_id": "T1", "name": "Sir Alam", "person_type": "teacher"},
        {"ems_student_id": "X1", "name": "Bacchu", "person_type": "staff"},
    ])
    rows, total = ctx.repos["students"].list(person_type="teacher")
    assert total == 1 and rows[0]["name"] == "Sir Alam"
    assert ctx.repos["students"].list(person_type="student")[1] == 3


def test_iclock_uses_rest_roster_side_channel(ctx, rest_server):
    """Split mode: punches over iClock, the directory over REST."""
    seed_device(ctx.repos, serial="SN-SPLIT")
    worker = _worker(ctx, rest_server.url, "iclock", FakeRestServer.TOKEN)
    worker.status.update(online=True)
    ctx.repos["queue"].enqueue("fetch_people")
    worker._process_queue()

    assert ctx.repos["students"].count() == 6
    assert worker.status.roster_supported
    assert ctx.repos["students"].counts_by_type()["teacher"] == 2


def test_mapping_accepts_any_person_type(ctx):
    from apexity_bridge.students.mapping import MappingService

    seed_device(ctx.repos, serial="SN-MAP")
    _seed_users(ctx.repos, "SN-MAP")
    ctx.repos["students"].upsert_many([
        {"ems_student_id": "T1", "name": "Sir Alam", "person_type": "teacher"},
    ])
    service = MappingService(ctx.repos)
    service.assign(device_serial="SN-MAP", device_user_id="2", ems_student_id="T1")
    rows, _ = ctx.repos["device_users"].list(device_serial="SN-MAP")
    row = next(r for r in rows if r["user_id"] == "2")
    assert row["person_name"] == "Sir Alam"
    assert row["person_type"] == "teacher"


# ------------------------------------------------------------- auto-mapping --
def test_auto_map_binds_exact_matches_only(ctx):
    from apexity_bridge.students.mapping import MappingService

    seed_device(ctx.repos, serial="SN-AUTO")
    ctx.repos["device_users"].replace_for("SN-AUTO", [
        {"user_id": "1", "name": "Anika Hossain", "privilege": 0},   # by name
        {"user_id": "12", "name": "", "privilege": 0},               # by roll no
        {"user_id": "99", "name": "Nobody Here", "privilege": 0},    # no match
    ])
    ctx.repos["students"].upsert_many([
        {"ems_student_id": "S1", "name": "anika  hossain", "person_type": "student",
         "roll_no": "12", "class_name": "Nine"},
        {"ems_student_id": "S2", "name": "Anika Hossain", "person_type": "student"},
    ])
    service = MappingService(ctx.repos)

    dry = service.auto_map(apply=False)
    # PIN 12 matches roll number 12 → unambiguous;
    # "Anika Hossain" matches two people → ambiguous, never guessed.
    assert [m["device_user_id"] for m in dry["matched"]] == ["12"]
    assert [a["device_user_id"] for a in dry["ambiguous"]] == ["1"]

    ctx.repos["students"].upsert_many([
        {"ems_student_id": "S2", "name": "Bithi Akter", "person_type": "student"},
    ])
    result = service.auto_map()
    bound = {m["device_user_id"]: m["ems_student_id"] for m in result["matched"]}
    assert bound == {"1": "S1", "12": "S1"}
    assert len(result["unmatched"]) == 1
    assert ctx.repos["device_users"].unmapped_count() == 1


def test_auto_map_handles_teachers_and_bengali_names(ctx):
    from apexity_bridge.students.mapping import MappingService

    seed_device(ctx.repos, serial="SN-BN")
    ctx.repos["device_users"].replace_for("SN-BN", [
        {"user_id": "5", "name": "সাদিয়া ইসলাম", "privilege": 0},
        {"user_id": "9", "name": "Sir Alam", "privilege": 0},
    ])
    ctx.repos["students"].upsert_many([
        {"ems_student_id": "S9", "name": "Sadia Islam", "person_type": "student"},
        {"ems_student_id": "EMP-01", "name": "Sir Alam", "person_type": "teacher",
         "employee_no": "EMP-01"},
    ])
    result = MappingService(ctx.repos).auto_map()
    bound = {m["device_user_id"]: m["ems_student_id"] for m in result["matched"]}
    assert bound == {"9": "EMP-01"}          # the teacher by name
    assert any(u["device_user_id"] == "5" for u in result["unmatched"])
