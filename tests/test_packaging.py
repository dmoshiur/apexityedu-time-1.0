"""Frozen-install helpers, first-run wizard, settings token/password APIs."""
from __future__ import annotations

import re
from pathlib import Path

from apexity_bridge.dashboard.app import create_app
from apexity_bridge.resources import app_dir, icon_path, is_frozen, resource_path
from apexity_bridge.security.passwords import verify_password
from apexity_bridge.windows_service.control import launch_command, task_xml


def test_not_frozen_in_tests():
    assert is_frozen() is False
    assert (app_dir() / "apexity_bridge").is_dir()


def test_branding_resources_exist():
    logo = resource_path("branding", "logo-mark.png")
    assert logo.exists(), logo
    ico = icon_path()
    assert ico is not None and ico.exists()


def test_launch_command_source_checkout():
    exe, args, workdir = launch_command()
    assert "-m apexity_bridge run" in args
    assert Path(exe).name.lower().startswith("python")


def test_task_xml_accepts_frozen_arguments():
    xml = task_xml(
        python_exe=r"C:\Program Files\ApexityEdu\Attendance Bridge\ApexityEduBridge.exe",
        workdir=r"C:\Program Files\ApexityEdu\Attendance Bridge",
        arguments="tray --hidden",
    )
    assert "tray --hidden" in xml
    assert "ApexityEduBridge.exe" in xml
    assert "-m apexity_bridge run" not in xml


def test_find_env_file_prefers_data_dir(tmp_path, monkeypatch):
    from apexity_bridge.config import find_env_file

    monkeypatch.setenv("BRIDGE_DATA_DIR", str(tmp_path))
    env = tmp_path / ".env"
    env.write_text("EMS_BASE_URL=https://example.test\n", encoding="utf-8")
    found = find_env_file()
    assert found == env


def test_setup_wizard_sets_password_and_token(ctx):
    app = create_app(ctx)
    app.config.update(TESTING=True)
    client = app.test_client()

    assert client.get("/login").status_code == 302
    page = client.get("/setup")
    assert page.status_code == 200
    assert b"Welcome to ApexityEdu" in page.data
    assert b"logo.png" in page.data

    resp = client.post("/setup", data={
        "username": "admin",
        "password": "school-pass-99",
        "password_confirm": "school-pass-99",
        "ems_token": "tok-from-setup",
    }, follow_redirects=False)
    assert resp.status_code == 302
    hashed = ctx.repos["settings"].get("dashboard_admin_password_hash")
    assert verify_password("school-pass-99", hashed)
    assert ctx.secret_store.get_ems_token() == "tok-from-setup"

    # second visit is rejected — password already configured
    assert client.get("/setup").status_code == 302


def test_setup_rejects_mismatch_and_short_password(ctx):
    app = create_app(ctx)
    app.config.update(TESTING=True)
    client = app.test_client()
    resp = client.post("/setup", data={
        "password": "abcdefghi", "password_confirm": "different1",
    })
    assert b"do not match" in resp.data
    resp = client.post("/setup", data={
        "password": "short", "password_confirm": "short",
    })
    assert resp.status_code == 200
    assert ctx.repos["settings"].get("dashboard_admin_password_hash") is None


def _login(client) -> str:
    resp = client.post("/login", data={"username": "admin", "password": "admin-pass-123"},
                       follow_redirects=True)
    assert resp.status_code == 200
    page = client.get("/settings").data.decode()
    return re.search(r'name="csrf-token" content="([a-f0-9]+)"', page).group(1)


def test_settings_token_and_password_api(ctx):
    from apexity_bridge.security.passwords import hash_password
    ctx.repos["settings"].set("dashboard_admin_password_hash",
                              hash_password("admin-pass-123"), is_secret=True)
    app = create_app(ctx)
    app.config.update(TESTING=True)
    client = app.test_client()
    token = _login(client)

    resp = client.post("/api/settings/token", json={"token": "ems-secret"},
                       headers={"X-CSRF-Token": token})
    assert resp.status_code == 200
    assert resp.get_json()["token_configured"] is True
    assert ctx.secret_store.get_ems_token() == "ems-secret"

    resp = client.post("/api/settings/password",
                       json={"current": "admin-pass-123", "new": "newer-pass-123",
                             "confirm": "newer-pass-123"},
                       headers={"X-CSRF-Token": token})
    assert resp.status_code == 200
    assert verify_password("newer-pass-123",
                           ctx.repos["settings"].get("dashboard_admin_password_hash"))

    bad = client.post("/api/settings/password",
                      json={"current": "wrong-password", "new": "another-pass",
                            "confirm": "another-pass"},
                      headers={"X-CSRF-Token": token})
    assert bad.status_code == 403


def test_branded_login_and_base_pages(ctx):
    from apexity_bridge.security.passwords import hash_password
    ctx.repos["settings"].set("dashboard_admin_password_hash",
                              hash_password("admin-pass-123"), is_secret=True)
    app = create_app(ctx)
    app.config.update(TESTING=True)
    client = app.test_client()
    login = client.get("/login")
    assert b"logo.png" in login.data
    assert b"favicon.ico" in login.data
    client.post("/login", data={"username": "admin", "password": "admin-pass-123"})
    overview = client.get("/")
    assert overview.status_code == 200
    assert b"logo.png" in overview.data
    assert b"favicon.ico" in overview.data
