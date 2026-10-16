"""Live EMS ingestion verification (read-mostly, no fake punches).

Problem this solves
-------------------
"Everything says OK but nothing reaches ApexityEdu."

An iClock/ADMS server answers a ``cdata`` handshake for *any* serial — that is
how real terminals fetch their configuration, so a successful handshake proves
only that the URL exists. It does **not** prove that:

* the serial the bridge pushes is the serial the school registered in
  apexityedu.com → Biometric Devices (a mismatch is the single most common
  cause of "collected but never uploaded"),
* the server accepts ``POST`` on the ``cdata`` route at all (some deployments
  only implement ``GET``),
* the server can ingest attendance lines for that serial.

So this module stops guessing and asks the server, one safe step at a time:

1. ``GET  …/cdata?SN=<sn>&options=all``   — is the endpoint alive?
2. ``GET  …/getrequest?SN=<sn>``          — is presence polling wired up?
3. ``POST …/cdata?SN=<sn>&table=ATTLOG``  with an **empty** body — does the
   ingestion route exist and accept this serial? No records are written, so
   the school register is never polluted with a test punch.
4. ``POST …/cdata?SN=<sn>&table=USER``    with the terminal's real user table
   (only when the bridge has one cached and ``EMS_PUSH_USERS`` is on) — this
   is what lets apexityedu learn which PIN belongs to which human.

Every step records the server's exact answer so the output can be pasted into
a support ticket verbatim. Secrets are redacted.

CLI:  ``python -m apexity_bridge verify-ems [--sn SERIAL] [--no-users]``
"""
from __future__ import annotations

import logging

from ..database.repositories import ems_serial_for
from ..timeutils import iso_utc

log = logging.getLogger("apexity_bridge.ems.verify")


def _iclock_steps(client, serial: str, users: list[dict] | None, push_users: bool):
    """Run the four iClock checks for one serial."""
    steps: list[dict] = []

    try:
        text = client.handshake(serial)
        steps.append({
            "step": "handshake",
            "request": f"GET …/cdata?SN={serial}&options=all",
            "ok": True,
            "reply": (text or "")[:200],
        })
    except Exception as exc:                     # noqa: BLE001 - reported, not raised
        steps.append({"step": "handshake", "ok": False, "error": str(exc)[:300]})
        return steps, "handshake failed — the EMS URL or network is unreachable"

    try:
        commands = client.poll_commands(serial)
        steps.append({
            "step": "presence",
            "request": f"GET …/getrequest?SN={serial}",
            "ok": True,
            "reply": f"{len(commands)} pending command(s)",
        })
    except Exception as exc:                     # noqa: BLE001
        steps.append({"step": "presence", "ok": False, "error": str(exc)[:300]})

    attlog = client.probe_attlog_path(serial)
    steps.append({
        "step": "attlog_route",
        "request": f"POST …/cdata?SN={serial}&table=ATTLOG (empty body)",
        "ok": bool(attlog.get("ok")),
        "reply": attlog.get("reply"),
        "error": attlog.get("error"),
        "note": attlog.get("note"),
    })

    if push_users and users:
        outcome = client.push_users(serial, users)
        steps.append({
            "step": "user_table",
            "request": f"POST …/cdata?SN={serial}&table=USER ({len(users)} users)",
            "ok": outcome.fatal is None,
            "reply": outcome.detail[:200],
            "error": outcome.fatal,
        })
    elif push_users:
        steps.append({
            "step": "user_table", "ok": None,
            "reply": "skipped — no device users cached yet (read them from the "
                     "Devices → Device Users page first)",
        })

    if not attlog.get("ok"):
        verdict = ("this serial is NOT accepted for attendance uploads — add or "
                   "correct it in apexityedu.com → Biometric Devices, or set the "
                   "device's EMS serial in the bridge so the two match")
    else:
        verdict = ("attendance uploads for this serial are accepted "
                   "(empty ATTLOG acknowledged)")
    return steps, verdict


def _rest_steps(client) -> tuple[list[dict], str]:
    steps: list[dict] = []
    try:
        health = client.health_check()
        steps.append({"step": "health", "ok": health.ok, "reply": health.detail[:200]})
    except Exception as exc:                     # noqa: BLE001
        steps.append({"step": "health", "ok": False, "error": str(exc)[:300]})
        return steps, "REST bridge API unreachable or token rejected"
    try:
        page = client.fetch_people()
        steps.append({
            "step": "people", "ok": True,
            "reply": f"{len(page.students)} people on page 1 "
                     f"(complete={page.complete})",
        })
    except Exception as exc:                     # noqa: BLE001
        steps.append({"step": "people", "ok": False, "error": str(exc)[:300]})
    return steps, ("REST bridge API answered; attendance pushes use "
                   "POST /api/v1/bridge/attendance/batch")


def verify_ems_ingestion(ctx, serial: str | None = None, *,
                         push_users: bool | None = None) -> dict:
    """Ask the live EMS what it accepts. Returns a JSON-serialisable report."""
    client = ctx.ems_client
    if client is None:
        return {"ok": False, "checked_at": iso_utc(),
                "error": "no EMS client is configured (check EMS_BASE_URL / EMS_PROTOCOL)"}

    protocol = getattr(client, "protocol_name", "?")
    push_users = bool(getattr(ctx.settings, "ems_push_users", True)) \
        if push_users is None else push_users

    report: dict = {
        "checked_at": iso_utc(),
        "base_url": getattr(ctx.settings, "ems_base_url", ""),
        "protocol": protocol,
        "devices": [],
        "verdict": "",
    }

    if protocol == "iclock":
        devices = ctx.repos["devices"].list_active() or ctx.repos["devices"].list()
        if serial:
            targets = [(serial, None)]
        elif devices:
            targets = [(ems_serial_for(d), d) for d in devices]
        else:
            targets = [("", None)]
        for target, device in targets:
            if not target:
                report["devices"].append({
                    "serial": None, "ok": False,
                    "error": "no registered device — register a K40 first",
                    "steps": [],
                })
                continue
            users = (ctx.repos["device_users"].users_for_ems(device["serial_number"])
                     if device else None)
            if serial and not users:
                users = None
            steps, verdict = _iclock_steps(client, target, users, push_users)
            report["devices"].append({
                "serial": target,
                "hardware_serial": (device or {}).get("serial_number"),
                "override": bool(device) and target != (device or {}).get("serial_number"),
                "ok": all(step.get("ok") is not False for step in steps),
                "verdict": verdict,
                "steps": steps,
            })
        report["verdict"] = ("; ".join(f"{d['serial']}: {d['verdict']}"
                                       for d in report["devices"]))
        report["ok"] = all(d.get("ok") for d in report["devices"])
    elif protocol == "rest":
        steps, verdict = _rest_steps(client)
        report["devices"] = [{"serial": serial or "(REST token)", "steps": steps,
                              "ok": all(s.get("ok") is not False for s in steps),
                              "verdict": verdict}]
        report["verdict"] = verdict
        report["ok"] = report["devices"][0]["ok"]
    else:
        report["verdict"] = f"unsupported protocol {protocol!r}"
        report["ok"] = False
    return report


def format_report(report: dict) -> str:
    """Human-readable rendering for the CLI / log."""
    lines = [
        f"EMS ingestion verification — {report.get('checked_at')}",
        f"  base URL : {report.get('base_url')}",
        f"  protocol : {report.get('protocol')}",
    ]
    if report.get("error"):
        lines.append(f"  ! {report['error']}")
    for device in report.get("devices") or []:
        lines.append("")
        lines.append(f"  ● SN {device.get('serial')}"
                     + (" (EMS-serial override)" if device.get("override") else ""))
        if device.get("error"):
            lines.append(f"      ! {device['error']}")
        for step in device.get("steps") or []:
            mark = {True: "OK  ", False: "FAIL", None: "skip"}.get(step.get("ok"), "??")
            lines.append(f"      [{mark}] {step.get('step')}: "
                         f"{step.get('reply') or step.get('error') or ''}")
        lines.append(f"      ⇒ {device.get('verdict')}")
    lines.append("")
    lines.append(f"  VERDICT: {report.get('verdict')}")
    return "\n".join(lines)
