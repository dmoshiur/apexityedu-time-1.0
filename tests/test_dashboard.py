"""Dashboard: auth, CSRF, API endpoints, pages, diagnostics."""
from __future__ import annotations

import re
from datetime import datetime

import pytest

from apexity_bridge.dashboard.app import create_app
from apexity_bridge.security.passwords import hash_password
from conftest import seed_device, seed_students


@pytest.fixture()
def app(ctx):
    ctx.repos["settings"].set("dashboard_admin_password_hash",
                              hash_password("admin-pass-123"), is_secret=True)
    application = create_app(ctx)
    application.config.update(TESTING=True)
    return application


@pytest.fixture()
def client(app):
    return app.test_client()


def _login(client) -> str:
    resp = client.post("/login", data={"username": "admin", "password": "admin-pass-123"},
                       follow_redirects=True)
    assert resp.status_code == 200
    assert b"Overview" in resp.data or b"bridge" in resp.data.lower()
    page = client.get("/devices").data.decode()
    return re.search(r'name="csrf-token" content="([a-f0-9]+)"', page).group(1)


def _seed_event(ctx):
    device = seed_device(ctx.repos)
    ids = ctx.repos["events"].insert_new([{
        "dedup_hash": "dh-dash-1", "device_id": device["id"],
        "device_serial": device["serial_number"], "device_user_id": "1",
        "event_timestamp": "2026-10-15T08:00:00", "event_timestamp_tz": "Asia/Dhaka",
        "punch": 1, "status": 0, "verify_mode": 1, "mapping_status": "unmapped",
    }])
    return device, ids[0]


# --------------------------------------------------------------------- auth --
def test_pages_require_login(client):
    assert client.get("/").status_code == 302
    assert client.get("/api/overview").status_code == 401


def test_login_wrong_password_rejected(client):
    resp = client.post("/login", data={"username": "admin", "password": "nope-nope-nope"})
    assert b"Invalid username or password" in resp.data


def test_login_lockout_after_failures(client):
    from apexity_bridge.dashboard import auth as auth_mod
    auth_mod._failed_logins.clear()
    for _ in range(auth_mod.MAX_FAILED_LOGINS):
        client.post("/login", data={"username": "admin", "password": "bad-password"})
    resp = client.post("/login", data={"username": "admin", "password": "admin-pass-123"})
    assert b"Too many failed attempts" in resp.data
    auth_mod._failed_logins.clear()


def test_csrf_required_for_mutations(client, ctx):
    _seed_event(ctx)
    token = _login(client)
    # no token → 403
    assert client.post("/api/scan").status_code == 403
    # with token → passes auth layer (503 here: no discovery in tests)
    resp = client.post("/api/scan", headers={"X-CSRF-Token": token})
    assert resp.status_code == 503


def test_cross_origin_post_blocked(client, ctx):
    token = _login(client)
    resp = client.post("/api/settings", json={"scan_interval": "120"},
                       headers={"X-CSRF-Token": token,
                                "Origin": "http://evil.example.com"})
    assert resp.status_code == 403


def test_logout_clears_session(client):
    _login(client)
    client.get("/logout")
    assert client.get("/api/overview").status_code == 401


# ------------------------------------------------------------------- overview --
def test_overview_api_shape(client, ctx):
    _seed_event(ctx)
    seed_students(ctx.repos)
    token = _login(client)
    d = client.get("/api/overview").get_json()
    assert d["ok"]
    assert d["events"]["pending"] == 1
    assert d["devices"]["registered"] == 1
    assert d["students"]["total"] == 3
    assert "ems" in d and "protocol" in d["ems"]


def test_all_pages_render(client):
    _login(client)
    for path in ("/", "/devices", "/students", "/attendance", "/logs", "/settings"):
        resp = client.get(path)
        assert resp.status_code == 200, path


# -------------------------------------------------------------------- devices --
def test_devices_api_and_actions(client, ctx, adapter_factory):
    from apexity_bridge.devices.base import DeviceIdentity
    from apexity_bridge.discovery.identify import Identification

    device = seed_device(ctx.repos, serial="SN-A", ip="192.168.0.201")
    adapter_factory.add("192.168.0.201", serial="SN-A")
    token = _login(client)
    headers = {"X-CSRF-Token": token}

    d = client.get("/api/devices").get_json()
    assert d["devices"][0]["serial_number"] == "SN-A"

    resp = client.post(f"/api/devices/{device['id']}/test", headers=headers)
    body = resp.get_json()
    assert resp.status_code == 200 and body["identified"] and body["serial_matches_registered"]

    resp = client.post(f"/api/devices/{device['id']}/disable", headers=headers)
    assert ctx.repos["devices"].get_by_id(device["id"])["status"] == "disabled"
    audits = ctx.repos["audit"].list(action="device.disable")[0]
    assert audits and audits[0]["actor"] == "admin"


def test_manual_add_requires_verified_serial(client, ctx, adapter_factory):
    token = _login(client)
    headers = {"X-CSRF-Token": token}

    # unknown host → verification fails → refused
    resp = client.post("/api/devices/manual", data={"ip": "192.168.9.9"}, headers=headers)
    assert resp.status_code == 502
    assert ctx.repos["devices"].list() == []

    # real fake device → accepted as pending, still needing admin registration
    adapter_factory.add("192.168.0.50", serial="SN-MANUAL", model="K40")
    resp = client.post("/api/devices/manual", data={"ip": "192.168.0.50"}, headers=headers)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["device"]["serial_number"] == "SN-MANUAL"
    assert body["device"]["status"] == "pending"


# ------------------------------------------------------------------- students --
def test_students_api_filters_and_pagination(client, ctx):
    seed_students(ctx.repos)
    _login(client)

    data = client.get("/api/students?class=Six&section=B").get_json()
    assert data["total"] == 1
    assert data["students"][0]["name"] == "Bithi Akter"

    data = client.get("/api/students?search=afnan").get_json()
    assert data["total"] == 1 and data["students"][0]["ems_student_id"] == "S1001"

    data = client.get("/api/students?active=0").get_json()
    assert data["total"] == 1 and data["students"][0]["active"] == 0

    data = client.get("/api/students?page=1&page_size=2").get_json()
    assert len(data["students"]) == 2


def test_mapping_api_crud(client, ctx):
    seed_device(ctx.repos, serial="SN-A")
    seed_students(ctx.repos)
    token = _login(client)
    headers = {"X-CSRF-Token": token}

    resp = client.post("/api/mappings", json={
        "device_serial": "SN-A", "device_user_id": "1", "ems_student_id": "S1001",
    }, headers=headers)
    assert resp.status_code == 200
    mid = resp.get_json()["mapping_id"]

    d = client.get("/api/mappings").get_json()
    assert len(d["mappings"]) == 1

    resp = client.post("/api/mappings", json={
        "device_serial": "SN-A", "device_user_id": "1", "ems_student_id": "GHOST",
    }, headers=headers)
    assert resp.status_code == 400

    resp = client.delete(f"/api/mappings/{mid}", headers=headers)
    assert resp.status_code == 200
    assert ctx.repos["mappings"].count() == 0


def test_roster_refresh_reports_missing_api(client, ctx):
    """With no EMS reachable, refresh must fail with a readable message,
    never fake students."""
    token = _login(client)
    resp = client.post("/api/students/refresh", headers={"X-CSRF-Token": token})
    assert resp.status_code in (400, 502)
    assert ctx.repos["students"].count() == 0


# ------------------------------------------------------------------ attendance --
def test_events_api_and_retry(client, ctx):
    _, eid = _seed_event(ctx)
    ctx.repos["events"].mark_attempt_failed([eid], "boom", "2030-01-01T00:00:00+00:00")
    token = _login(client)

    d = client.get("/api/events?status=failed").get_json()
    assert d["total"] == 1

    resp = client.post("/api/events/retry", json={"ids": [eid]},
                       headers={"X-CSRF-Token": token})
    assert resp.get_json()["updated"] == 1
    row = ctx.repos["events"].by_ids([eid])[0]
    assert row["sync_status"] == "pending"


# --------------------------------------------------------------------- misc ---
def test_settings_roundtrip_and_validation(client, ctx):
    token = _login(client)
    headers = {"X-CSRF-Token": token}

    resp = client.post("/api/settings", json={"ems_protocol": "bogus"}, headers=headers)
    assert resp.status_code == 400

    resp = client.post("/api/settings", json={"scan_interval": "120",
                                              "ems_protocol": "iclock"}, headers=headers)
    assert resp.get_json()["changed"]
    d = client.get("/api/settings").get_json()
    assert d["settings"]["scan_interval"] == "120"

    audits = ctx.repos["audit"].list(action="settings.update")[0]
    assert "scan_interval" in audits[0]["detail"]


def test_logs_api(client, ctx):
    ctx.repos["audit"].log(actor="tester", action="something.happened", detail="hello")
    token = _login(client)
    d = client.get("/api/logs?search=something").get_json()
    assert d["total"] >= 1
    assert any(l["action"] == "something.happened" for l in d["logs"])


def test_backup_endpoint(client, ctx):
    token = _login(client)
    resp = client.post("/api/backup", headers={"X-CSRF-Token": token})
    assert resp.status_code == 200
    path = resp.get_json()["backup"]
    assert "bridge-backup" in path


def test_diagnostics_export_and_download(client, ctx):
    token = _login(client)
    resp = client.post("/api/diagnostics/export", headers={"X-CSRF-Token": token})
    assert resp.status_code == 200
    name = resp.get_json()["file"]
    resp = client.get(f"/api/diagnostics/download/{name}")
    assert resp.status_code == 200
    assert resp.data[:2] == b"PK"            # valid zip
    # path traversal refused
    assert client.get("/api/diagnostics/download/..%2f..%2fetc%2fpasswd").status_code == 404


def test_api_unknown_device_is_404(client):
    token = _login(client)
    resp = client.post("/api/devices/99999/register",
                       headers={"X-CSRF-Token": token})
    assert resp.status_code == 404


# ------------------------------------------------------------- device users --
def test_device_users_page_and_api(client, ctx):
    device = seed_device(ctx.repos, serial="SN-DASH")
    ctx.repos["device_users"].replace_for("SN-DASH", [
        {"user_id": "1", "name": "Rahim", "privilege": 0},
        {"user_id": "2", "name": "Karim Sir", "privilege": 0},
    ])
    seed_students(ctx.repos)
    ctx.repos["mappings"].upsert(device_serial="SN-DASH", device_user_id="1",
                                 ems_student_id="S1001", mapped_by="admin")
    token = _login(client)

    assert client.get("/device-users").status_code == 200

    d = client.get("/api/device-users").get_json()
    assert d["total"] == 2
    assert d["unmapped"] == 1
    rows = {r["user_id"]: r for r in d["device_users"]}
    assert rows["1"]["person_name"] == "Afnan Rahman"
    assert rows["2"]["mapping_id"] is None

    d = client.get("/api/device-users?unmapped=1").get_json()
    assert d["total"] == 1 and d["device_users"][0]["user_id"] == "2"

    overview = client.get("/api/overview").get_json()
    assert overview["device_users"]["total"] == 2
    assert overview["device_users"]["unmapped"] == 1
    assert overview["students"]["by_type"]["student"] == 3


def test_people_api_filters_by_person_type(client, ctx):
    ctx.repos["students"].upsert_many([
        {"ems_student_id": "S1", "name": "Anika", "person_type": "student"},
        {"ems_student_id": "T1", "name": "Sir Alam", "person_type": "teacher",
         "designation": "Headmaster"},
    ])
    token = _login(client)
    d = client.get("/api/people?person_type=teacher").get_json()
    assert d["total"] == 1
    assert d["people"][0]["designation"] == "Headmaster"
    assert d["by_type"]["teacher"] == 1


def test_ems_serial_action(client, ctx):
    device = seed_device(ctx.repos, serial="SN-HW")
    token = _login(client)
    resp = client.post(f"/api/devices/{device['id']}/ems-serial",
                       data={"ems_serial": "APEX-9"},
                       headers={"X-CSRF-Token": token})
    assert resp.status_code == 200
    assert ctx.repos["devices"].get_by_id(device["id"])["ems_serial"] == "APEX-9"


def test_ems_verify_endpoint_reports_a_problem(client, ctx):
    """No EMS configured → the report must say so instead of claiming success."""
    seed_device(ctx.repos, serial="SN-VERIFY")
    token = _login(client)
    d = client.get("/api/ems/verify", headers={"X-CSRF-Token": token}).get_json()
    assert d["ok"] is True                 # the endpoint itself worked
    report = d["report"]
    # no EMS client is configured in this fixture → the report must admit it
    # rather than pretending an ingestion check happened.
    if report.get("error"):
        assert "no EMS client" in report["error"]
    else:
        assert report["devices"] and report["ok"] is False
