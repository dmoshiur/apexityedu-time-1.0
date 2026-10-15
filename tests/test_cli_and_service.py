"""CLI entry points, service lifecycle, Windows task XML."""
from __future__ import annotations

import sys
from unittest import mock

import pytest

from apexity_bridge.__main__ import main
from apexity_bridge.windows_service.control import TASK_NAME, task_xml


def test_cli_init_db(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BRIDGE_DATA_DIR", str(tmp_path))
    rc = main(["init-db"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "database ready" in out and "ok" in out


def test_cli_set_admin_password(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_DATA_DIR", str(tmp_path))
    assert main(["init-db"]) == 0
    inputs = iter(["topsecret-password", "topsecret-password"])
    monkeypatch.setattr("getpass.getpass", lambda *a, **k: next(inputs))
    rc = main(["set-admin-password"])
    assert rc == 0

    from apexity_bridge.config import load_config
    from apexity_bridge.database.repositories import open_migrated_database
    from apexity_bridge.security.passwords import verify_password
    cfg = load_config(environ={"BRIDGE_DATA_DIR": str(tmp_path)})
    db, repos = open_migrated_database(cfg.db_path)
    assert verify_password("topsecret-password",
                           repos["settings"].get("dashboard_admin_password_hash"))
    db.close_all()


def test_cli_probe_ems_offline(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BRIDGE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("EMS_BASE_URL", "http://127.0.0.1:9")
    rc = main(["probe-ems"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "reachable: False" in out or "reachable          : False" in out


def test_cli_scan_no_targets(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BRIDGE_DATA_DIR", str(tmp_path))
    # limit scan surface: no interfaces, no targets → graceful failure
    with mock.patch("apexity_bridge.discovery.interfaces.list_ipv4_networks", return_value=[]):
        rc = main(["scan", "--subnet", "203.0.113.0/30"])
    # 203.0.113.0/30 has 2 hosts; probing will find nothing open (TEST-NET-3)
    out = capsys.readouterr().out
    assert "port 4370" in out
    assert rc == 1


def test_service_lifecycle(tmp_path, monkeypatch):
    """Service starts all threads and stops cleanly without a dashboard."""
    from apexity_bridge.config import Config
    from apexity_bridge.service import BridgeService
    from apexity_bridge.sync.worker import EmsStatus
    import threading, time

    cfg = Config(
        ems_base_url="http://127.0.0.1:9", ems_protocol="iclock",
        db_path=tmp_path / "b.db", data_dir=tmp_path, log_dir=tmp_path / "logs",
        collect_interval=5, discovery_interval=60,
    )
    from conftest import FakeAdapterFactory
    from apexity_bridge.discovery.service import DiscoveryService, ScanReport
    from apexity_bridge.timeutils import iso_utc

    factory = FakeAdapterFactory()
    service = BridgeService(cfg, adapter_factory=factory, start_dashboard=False)
    # avoid a real subnet probe inside the unit test (hardware-less env)
    monkeypatch.setattr(DiscoveryService, "run_scan",
                        lambda self, **k: ScanReport(started_at=iso_utc(), finished_at=iso_utc()))
    ctx = service.start()
    assert ctx.sync_worker._thread and ctx.sync_worker._thread.is_alive()
    assert ctx.collector._thread.is_alive()
    assert ctx.discovery._thread.is_alive()
    service.stop()
    time.sleep(0.2)
    assert not ctx.collector._thread.is_alive()
    audits = ctx.repos["db"].query(
        "SELECT action FROM audit_logs WHERE action IN ('service.started','service.stopped')")
    assert {r["action"] for r in audits} == {"service.started", "service.stopped"}


def test_task_scheduler_xml():
    xml = task_xml(python_exe=r"C:\bridge\.venv\Scripts\pythonw.exe",
                   workdir=r"C:\bridge")
    assert "ApexityEduBridge" in xml
    assert r"-m apexity_bridge run" in xml
    assert "RestartOnFailure" in xml
    assert r"C:\bridge\.venv\Scripts\pythonw.exe" in xml
    assert "LogonTrigger" in xml


def test_service_command_on_non_windows(capsys):
    if sys.platform.startswith("win"):
        pytest.skip("covered by Windows acceptance tests")
    from apexity_bridge.config import load_config
    from apexity_bridge.windows_service.control import service_action
    rc = service_action("status", load_config(environ={}))
    assert rc == 1
    assert "Task Scheduler" in capsys.readouterr().out
