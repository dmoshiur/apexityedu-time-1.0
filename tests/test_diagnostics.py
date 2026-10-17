"""Self-check (`doctor`) must be read-only, honest and safe to paste."""
from __future__ import annotations

import json
from types import SimpleNamespace

from apexity_bridge.diagnostics import build_doctor_report, format_doctor_report
from apexity_bridge.sync.worker import SyncWorker
from conftest import seed_device


def _worker(ctx, url):
    ctx.repos["settings"].set("ems_base_url", url)
    ctx.repos["settings"].set("ems_protocol", "iclock")
    worker = SyncWorker(ctx)
    ctx.sync_worker = worker
    return worker


def test_offline_report_covers_database_and_configuration(ctx):
    report = build_doctor_report(ctx, network=False)

    assert report["network_checked"] is False
    assert report["ok"] is True
    ids = {check["id"] for check in report["checks"]}
    assert {"database.file", "database.integrity", "database.schema",
            "database.writable", "config.valid"} <= ids
    assert any(check["status"] == "warn" for check in report["checks"])
    assert "Celery" in report["server_note"]


def test_report_text_has_status_and_fixes(ctx):
    report = build_doctor_report(ctx, network=False)
    text = format_doctor_report(report)

    assert "ApexityEdu Bridge self-check" in text
    assert "Overall:" in text
    assert "fix:" in text
    assert "Celery" in text


def test_token_is_never_included_in_the_report(ctx):
    ctx.secret_store.set_ems_token("super-secret-device-token")
    report = build_doctor_report(ctx, network=False)
    rendered = json.dumps(report) + format_doctor_report(report)
    assert "super-secret-device-token" not in rendered
    assert "token_configured" in json.dumps(report)


def test_online_report_checks_each_serial_and_classifies_commands(ctx, iclock_server):
    seed_device(ctx.repos, serial="SN-DOC", ip="192.168.0.201")
    _worker(ctx, iclock_server.url)
    iclock_server.poll_body = "C:1:DATA QUERY USERINFO\nC:2:REBOOT"

    report = build_doctor_report(ctx, network=True)

    device = report["ems"]["devices"][0]
    assert device["ems_serial"] == "SN-DOC"
    assert device["handshake"] is True
    assert "REBOOT" in device["commands_refused"]
    assert any(check["id"] == "ems.commands.SN-DOC" for check in report["checks"])
    # Doctor is read-only: it must not upload users or execute the command.
    assert iclock_server.user_uploads == []
    assert iclock_server.command_acks == []


def test_online_report_marks_unreachable_ems(ctx):
    ctx.repos["settings"].set("ems_base_url", "http://127.0.0.1:9")
    ctx.repos["settings"].set("ems_protocol", "iclock")
    ctx.sync_worker = SyncWorker(ctx)

    report = build_doctor_report(ctx, network=True)

    assert report["ok"] is False
    assert any(check["id"] == "ems.probe" and check["status"] == "fail"
               for check in report["checks"])


def test_cli_doctor_json_offline(ctx, config, capsys):
    from apexity_bridge.__main__ import _cmd_doctor

    code = _cmd_doctor(config, SimpleNamespace(no_network=True, json=True))
    output = capsys.readouterr().out
    payload = json.loads(output)

    assert code == 0
    assert payload["network_checked"] is False
    assert payload["version"]


def test_failed_sync_attempt_is_surfaced_for_server_correlation(ctx):
    ctx.repos["queue"].record_attempt(
        attempt_type="push_attendance", event_count=2, result="failed",
        error="HTTP 500 from EMS",
    )
    report = build_doctor_report(ctx, network=False)
    check = next(c for c in report["checks"] if c["id"] == "database.sync_attempts")
    assert check["status"] == "warn"
    assert "HTTP 500" in check["detail"]
    assert report["database"]["recent_sync_attempts"][0]["attempt_type"] == "push_attendance"


def test_doctor_does_not_treat_denied_as_a_successful_handshake(ctx, iclock_server):
    seed_device(ctx.repos, serial="SN-DENY", ip="192.168.0.201")
    _worker(ctx, iclock_server.url)
    iclock_server.denied_serials.update({"SN-DENY", "APEXITY-HEALTHCHECK"})

    report = build_doctor_report(ctx, network=True)

    device = report["ems"]["devices"][0]
    assert device["handshake"] is False
    assert "DENIED" in device["error"] and "SN-DENY" in device["error"]
    checks = {c["id"]: c for c in report["checks"]}
    assert checks["ems.serials"]["status"] == "fail"
    # A DENIED synthetic health serial still proves the server is reachable.
    assert checks["ems.health"]["status"] == "ok"
