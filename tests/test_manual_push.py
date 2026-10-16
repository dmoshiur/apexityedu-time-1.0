"""Manual pushes to the EMS — device users, people, and the scheduler.

These tests pin the behaviour the desktop app's manual buttons rely on:

* a manual push happens **now** and reports the server's answer instead of
  silently queueing work that may never run (the original "it does nothing"
  complaint),
* the people directory can be written back to the EMS — and an EMS that has
  no people-write endpoint is reported honestly instead of pretending,
* the scheduler helpers produce a valid periodic task.
"""
from __future__ import annotations

import importlib.util
import os
import sys

import pytest

from apexity_bridge.ems.base import EmsProtocolError
from apexity_bridge.ems.iclock import IclockEmsClient
from apexity_bridge.ems.push import (
    PushError, create_person, format_push_result, push_device_users, push_people,
)
from apexity_bridge.ems.rest import RestEmsClient

from conftest import seed_device, seed_students
from fakes import FakeRestServer


# --------------------------------------------------------------- fixtures ----
def _seed_users(repos, serial: str, users=(("1", "Rahim"), ("2", "Karim"))):
    repos["device_users"].replace_for(serial, [
        {"user_id": uid, "name": name, "privilege": 0, "card": None}
        for uid, name in users
    ])


def _worker(ctx, url: str, protocol: str = "iclock", token: str | None = None):
    """Build a sync worker that talks to the fake EMS server."""
    from apexity_bridge.sync.worker import SyncWorker

    ctx.repos["settings"].set("ems_base_url", url)
    ctx.repos["settings"].set("ems_protocol", protocol)
    if token is not None:
        ctx.secret_store.set_ems_token(token)
    worker = SyncWorker(ctx)
    ctx.sync_worker = worker          # ctx.ems_client reads through the worker
    return worker


# ------------------------------------------------- 1. the EMS client layer --
def test_rest_client_publishes_people(rest_server):
    client = RestEmsClient(rest_server.url,
                           token_provider=lambda: FakeRestServer.TOKEN)
    outcome = client.push_people([
        {"id": "S1001", "name": "Afnan Rahman", "person_type": "student"},
        {"id": "T2001", "name": "Sir Alam", "person_type": "teacher"},
    ])
    assert outcome.fatal is None
    assert outcome.unsupported is False
    assert outcome.accepted == 2
    sent = rest_server.people_uploads[0]["people"]
    assert {p["id"] for p in sent} == {"S1001", "T2001"}


def test_rest_client_reports_a_missing_people_endpoint(rest_server):
    """An EMS without the write endpoint must be reported, not faked."""
    rest_server.people_write_enabled = False
    client = RestEmsClient(rest_server.url,
                           token_provider=lambda: FakeRestServer.TOKEN)
    outcome = client.push_people([{"id": "S1001", "name": "Afnan Rahman"}])
    assert outcome.unsupported is True
    assert outcome.fatal is None
    assert "POST /api/v1/bridge/people" in outcome.detail


def test_iclock_cannot_write_people_without_a_rest_side_channel(iclock_server):
    client = IclockEmsClient(iclock_server.url)
    with pytest.raises(EmsProtocolError) as exc:
        client.push_people([{"id": "S1001", "name": "x"}])
    assert "people" in str(exc.value).lower()


def test_iclock_delegates_people_to_the_rest_side_channel(iclock_server, rest_server):
    client = IclockEmsClient(
        iclock_server.url,
        roster_client=RestEmsClient(rest_server.url,
                                    token_provider=lambda: FakeRestServer.TOKEN),
    )
    outcome = client.push_people([{"id": "S1001", "name": "Afnan Rahman"}])
    assert outcome.fatal is None
    assert rest_server.people_uploads


# ---------------------------------------------------- 2. the push service ----
def test_push_device_users_reports_the_result(ctx, iclock_server):
    seed_device(ctx.repos, serial="SN-PUSH")
    _seed_users(ctx.repos, "SN-PUSH")
    ctx.sync_worker = _worker(ctx, iclock_server.url, "iclock")

    result = push_device_users(ctx)
    assert result["ok"] is True
    assert result["pushed"] == 1
    assert result["devices"][0]["state"] == "pushed"
    assert result["devices"][0]["users"] == 2
    assert iclock_server.user_uploads[0]["users"] == {"1": "Rahim", "2": "Karim"}
    assert "pushed" in format_push_result(result).lower()


def test_push_device_users_says_when_nothing_was_read_yet(ctx, iclock_server):
    seed_device(ctx.repos, serial="SN-EMPTY")
    ctx.sync_worker = _worker(ctx, iclock_server.url, "iclock")

    result = push_device_users(ctx)
    assert result["ok"] is False
    assert result["devices"][0]["state"] == "no users read yet"
    assert "Read from device" in result["devices"][0]["error"]


def test_push_device_users_reports_a_server_refusal(ctx, iclock_server):
    seed_device(ctx.repos, serial="SN-REFUSE")
    _seed_users(ctx.repos, "SN-REFUSE")
    iclock_server.user_reply = "<html>login</html>"
    ctx.sync_worker = _worker(ctx, iclock_server.url, "iclock")

    result = push_device_users(ctx)
    assert result["ok"] is False
    assert result["devices"][0]["state"] == "refused"
    assert "USER" in result["devices"][0]["error"]


def test_manual_push_works_even_when_the_ems_is_not_marked_online(ctx, iclock_server):
    """The original bug: the button only queued work that never ran."""
    seed_device(ctx.repos, serial="SN-OFFLINE")
    _seed_users(ctx.repos, "SN-OFFLINE")
    worker = _worker(ctx, iclock_server.url, "iclock")
    ctx.sync_worker = worker
    assert worker.status.online is False

    result = worker.push_device_users_now()
    assert result["ok"] is True
    assert iclock_server.user_uploads


def test_push_people_publishes_the_whole_directory(ctx, rest_server):
    seed_students(ctx.repos, [
        {"ems_student_id": "S1001", "name": "Afnan Rahman", "person_type": "student"},
        {"ems_student_id": "T2001", "name": "Sir Alam", "person_type": "teacher"},
        {"ems_student_id": "X3001", "name": "Bacchu Mia", "person_type": "staff"},
    ])
    ctx.sync_worker = _worker(ctx, rest_server.url, "rest", token=FakeRestServer.TOKEN)

    result = push_people(ctx)
    assert result["ok"] is True
    assert result["people"] == 3
    assert result["accepted"] == 3
    assert result["by_type"] == {"student": 1, "teacher": 1, "staff": 1}
    sent = rest_server.people_uploads[0]["people"]
    assert {p["id"] for p in sent} == {"S1001", "T2001", "X3001"}


def test_push_people_can_filter_by_type(ctx, rest_server):
    seed_students(ctx.repos, [
        {"ems_student_id": "S1001", "name": "Afnan Rahman", "person_type": "student"},
        {"ems_student_id": "T2001", "name": "Sir Alam", "person_type": "teacher"},
    ])
    ctx.sync_worker = _worker(ctx, rest_server.url, "rest", token=FakeRestServer.TOKEN)

    result = push_people(ctx, person_types=["teacher"])
    assert result["people"] == 1
    assert rest_server.people_uploads[0]["people"][0]["id"] == "T2001"


def test_push_people_reports_an_ems_without_the_endpoint(ctx, rest_server):
    seed_students(ctx.repos)
    rest_server.people_write_enabled = False
    ctx.sync_worker = _worker(ctx, rest_server.url, "rest", token=FakeRestServer.TOKEN)

    result = push_people(ctx)
    assert result["unsupported"] is True
    assert "POST /api/v1/bridge/people" in result["error"]


def test_create_person_writes_to_the_ems_and_caches_locally(ctx, rest_server):
    ctx.sync_worker = _worker(ctx, rest_server.url, "rest", token=FakeRestServer.TOKEN)

    result = create_person(ctx, {
        "id": "T2001", "name": "Sir Alam", "person_type": "teacher",
        "employee_no": "EMP-01", "designation": "Headmaster",
    })
    assert result["ok"] is True
    assert rest_server.people_uploads[0]["people"][0]["id"] == "T2001"
    cached = ctx.repos["students"].get("T2001")
    assert cached and cached["name"] == "Sir Alam"
    assert cached["person_type"] == "teacher"


def test_create_person_needs_an_id_and_a_name(ctx, rest_server):
    ctx.sync_worker = _worker(ctx, rest_server.url, "rest", token=FakeRestServer.TOKEN)
    with pytest.raises(PushError):
        create_person(ctx, {"id": "", "name": "No id"})
    with pytest.raises(PushError):
        create_person(ctx, {"id": "S1", "name": ""})


# ------------------------------------------------------------ 3. the facade --
def test_facade_manual_push_returns_a_readable_result(ctx, iclock_server):
    from apexity_bridge.desktop.facade import DesktopFacade

    seed_device(ctx.repos, serial="SN-FACADE")
    _seed_users(ctx.repos, "SN-FACADE")
    ctx.sync_worker = _worker(ctx, iclock_server.url, "iclock")

    facade = DesktopFacade(ctx)
    result = facade.push_device_users()
    assert result["ok"] is True
    assert "SN-FACADE" in result["text"] or "Gate K40" in result["text"]
    assert iclock_server.user_uploads


def test_facade_create_person_raises_a_readable_error(ctx, rest_server):
    from apexity_bridge.desktop.facade import DesktopFacade, FacadeError

    ctx.sync_worker = _worker(ctx, rest_server.url, "rest", token=FakeRestServer.TOKEN)
    facade = DesktopFacade(ctx)
    with pytest.raises(FacadeError):
        facade.create_person({"id": "", "name": "x"})


# ----------------------------------------------------------- 4. the CLI -----
def _cli_env(monkeypatch, tmp_path, url, protocol="iclock"):
    monkeypatch.setenv("BRIDGE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("EMS_BASE_URL", url)
    monkeypatch.setenv("EMS_PROTOCOL", protocol)
    monkeypatch.setenv("EMS_ROSTER_MODE", "off")
    monkeypatch.setenv("EMS_ALLOW_INSECURE_LOCALHOST", "1")


def test_cli_push_users_publishes_the_tables(tmp_path, monkeypatch, capsys,
                                             iclock_server):
    from apexity_bridge.__main__ import main
    from apexity_bridge.config import load_config
    from apexity_bridge.database.repositories import open_migrated_database

    _cli_env(monkeypatch, tmp_path, iclock_server.url)
    cfg = load_config()
    db, repos = open_migrated_database(cfg.db_path)
    try:
        seed_device(repos, serial="SN-CLI")
        _seed_users(repos, "SN-CLI")
    finally:
        db.close_all()

    rc = main(["push-users"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "pushed" in out.lower()
    assert iclock_server.user_uploads[0]["users"] == {"1": "Rahim", "2": "Karim"}


def test_cli_push_people_reports_an_unsupported_ems(tmp_path, monkeypatch, capsys,
                                                    rest_server):
    from apexity_bridge.__main__ import main
    from apexity_bridge.config import load_config
    from apexity_bridge.database.repositories import open_migrated_database

    rest_server.people_write_enabled = False
    _cli_env(monkeypatch, tmp_path, rest_server.url, protocol="rest")
    cfg = load_config()
    from apexity_bridge.security.secrets import SecretStore
    SecretStore(cfg.data_dir).set_ems_token(FakeRestServer.TOKEN)
    db, repos = open_migrated_database(cfg.db_path)
    try:
        seed_students(repos)
    finally:
        db.close_all()

    rc = main(["push-people"])
    out = capsys.readouterr().out
    assert rc == 2
    assert "NOT PUBLISHED" in out


def test_cli_sync_now_runs_all_three_steps(tmp_path, monkeypatch, capsys,
                                           rest_server):
    from apexity_bridge.__main__ import main
    from apexity_bridge.config import load_config
    from apexity_bridge.database.repositories import open_migrated_database

    _cli_env(monkeypatch, tmp_path, rest_server.url, protocol="rest")
    cfg = load_config()
    from apexity_bridge.security.secrets import SecretStore
    SecretStore(cfg.data_dir).set_ems_token(FakeRestServer.TOKEN)
    db, repos = open_migrated_database(cfg.db_path)
    try:
        seed_device(repos, serial="SN-SYNC")
        _seed_users(repos, "SN-SYNC")
    finally:
        db.close_all()

    rc = main(["sync-now"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "roster" in out
    assert "attendance" in out


# -------------------------------------------------------- 5. the scheduler --
def test_periodic_task_xml_runs_sync_now_periodically():
    from apexity_bridge.windows_service.control import (
        SYNC_TASK_NAME, periodic_task_xml,
    )
    xml = periodic_task_xml(python_exe=r"C:\bridge\python.exe",
                            workdir=r"C:\bridge", interval_minutes=15)
    assert SYNC_TASK_NAME in xml
    assert "sync-now" in xml
    assert "PT15M" in xml
    assert "TimeTrigger" in xml


def test_scheduler_overview_is_honest_on_other_platforms():
    from apexity_bridge.windows_service.control import scheduler_overview
    from apexity_bridge.config import Config

    overview = scheduler_overview(Config())
    if sys.platform.startswith("win"):
        assert overview["supported"] is True
        assert len(overview["tasks"]) == 2
    else:
        assert overview["supported"] is False
        assert "Windows" in overview["message"]


def test_service_action_rejects_unknown_actions(tmp_path):
    from apexity_bridge.config import Config
    from apexity_bridge.windows_service.control import service_action

    if sys.platform.startswith("win"):
        pytest.skip("Windows-only path")
    assert service_action("status", Config(data_dir=tmp_path)) == 1


# --------------------------------------------- 6. the reference EMS server --
def _load_reference_api(tmp_path, monkeypatch):
    monkeypatch.setenv("EMS_DB", str(tmp_path / "ems-reference.db"))
    monkeypatch.setenv("EMS_TOKEN", "test-token")
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "docs", "reference", "ems_reference_api.py")
    spec = importlib.util.spec_from_file_location("ems_reference_api", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    module.init_db()
    return module


def test_reference_api_upserts_people(tmp_path, monkeypatch):
    module = _load_reference_api(tmp_path, monkeypatch)
    client = module.app.test_client()
    headers = {"Authorization": "Bearer test-token"}
    resp = client.post("/api/v1/bridge/people", headers=headers, json={
        "people": [{"id": "T2001", "name": "Sir Alam", "person_type": "teacher",
                    "employee_no": "EMP-01", "designation": "Headmaster"}],
    })
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["ok"] is True
    assert payload["accepted"] == 1
    assert payload["created"] == 1

    # the same person again → update, never a duplicate
    resp = client.post("/api/v1/bridge/people", headers=headers, json={
        "people": [{"id": "T2001", "name": "Alam Sir", "person_type": "teacher"}],
    })
    assert resp.get_json()["created"] == 0
    assert resp.get_json()["updated"] == 1

    listed = client.get("/api/v1/bridge/people", headers=headers).get_json()
    assert [p["name"] for p in listed["people"]] == ["Alam Sir"]
