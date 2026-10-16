"""Local dashboard — Flask app factory + waitress runner.

Bind policy: 127.0.0.1 unless the admin explicitly opts into LAN access.
All mutating endpoints: session + CSRF + same-origin (see auth.py).
"""
from __future__ import annotations

import io
import json
import logging
import threading
import time
import zipfile
from datetime import timedelta
from pathlib import Path

from flask import (
    Flask, abort, jsonify, redirect, render_template, request,
    send_file, session, url_for,
)

from ..config import DB_MANAGED_KEYS
from ..students.mapping import MappingError, MappingService
from ..timeutils import iso_utc
from . import auth

log = logging.getLogger("apexity_bridge.dashboard")


def create_app(ctx) -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
        MAX_CONTENT_LENGTH=2 * 1024 * 1024,
        JSON_SORT_KEYS=False,
    )
    app.config["CTX"] = ctx
    repos = ctx.repos
    auth.ensure_secret_key(app, repos["settings"])
    auth.csrf_protect(app)

    def settings_repo():
        return repos["settings"]

    def mapping_service() -> MappingService:
        return MappingService(repos, audit_actor=auth.current_actor())

    def ok(data=None, **kw):
        payload = {"ok": True}
        if data:
            payload.update(data)
        payload.update(kw)
        return jsonify(payload)

    def fail(message: str, status: int = 400):
        return jsonify({"ok": False, "error": message}), status

    # ------------------------------------------------------------------ auth --
    @app.route("/setup", methods=["GET", "POST"])
    def setup():
        """First-run wizard: set the dashboard password (and optional EMS token)."""
        if auth.password_is_configured(settings_repo()):
            return redirect(url_for("login"))
        error = None
        if request.method == "POST":
            from ..security.passwords import hash_password
            pw1 = request.form.get("password") or ""
            pw2 = request.form.get("password_confirm") or ""
            token = (request.form.get("ems_token") or "").strip()
            if pw1 != pw2:
                error = "Passwords do not match."
            else:
                try:
                    hashed = hash_password(pw1)
                except ValueError as exc:
                    error = str(exc)
                else:
                    repos["settings"].set(
                        "dashboard_admin_password_hash", hashed, is_secret=True)
                    username = (request.form.get("username") or "admin").strip()[:64] or "admin"
                    repos["settings"].set("dashboard_admin_username", username)
                    if token and ctx.secret_store:
                        ctx.secret_store.set_ems_token(token)
                    repos["audit"].log(actor=username, action="dashboard.initial_setup")
                    return redirect(url_for("login"))
        return render_template("setup.html", error=error)

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if not auth.password_is_configured(settings_repo()):
            return redirect(url_for("setup"))
        error = None
        if request.method == "POST":
            allowed, wait = auth.login_allowed()
            if not allowed:
                error = f"Too many failed attempts — try again in {wait}s."
            else:
                username = request.form.get("username", "")[:64]
                password = request.form.get("password", "")
                if auth.check_credentials(settings_repo(), username, password):
                    auth.clear_login_failures()
                    session.clear()
                    session["admin"] = username
                    session["csrf_token"] = __import__("secrets").token_hex(32)
                    repos["audit"].log(actor=username, action="dashboard.login")
                    return redirect(request.args.get("next") or url_for("overview"))
                auth.record_login_failure()
                repos["audit"].log(actor=username or "?", action="dashboard.login_failed")
                error = "Invalid username or password."
        return render_template("login.html", setup_required=False, error=error)

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    # ----------------------------------------------------------------- pages --
    @app.route("/")
    @auth.login_required
    def overview():
        return render_template("overview.html")

    @app.route("/devices")
    @auth.login_required
    def devices_page():
        return render_template("devices.html")

    @app.route("/students")
    @auth.login_required
    def students_page():
        return render_template("students.html")

    @app.route("/device-users")
    @auth.login_required
    def device_users_page():
        return render_template("device_users.html")

    @app.route("/attendance")
    @auth.login_required
    def attendance_page():
        return render_template("attendance.html")

    @app.route("/logs")
    @auth.login_required
    def logs_page():
        return render_template("logs.html")

    @app.route("/settings")
    @auth.login_required
    def settings_page():
        return render_template(
            "settings.html",
            editable_keys=sorted(DB_MANAGED_KEYS.keys()),
        )

    # -------------------------------------------------------------- overview --
    @app.route("/api/overview")
    @auth.login_required
    def api_overview():
        events = repos["events"].counts()
        devices = repos["devices"].counts()
        online_known = [
            d for d in repos["devices"].list_active()
            if _device_online(ctx, d)
        ]
        return ok(
            service={"version": ctx.version, "uptime_seconds": int(ctx.uptime_seconds()),
                     "timezone": ctx.settings.device_timezone},
            ems=ctx.ems_status.snapshot(),
            devices={**devices, "online_now": len(online_known)},
            students={"total": repos["students"].count(),
                      "active": repos["students"].count(active_only=True),
                      "mappings": repos["mappings"].count(),
                      "by_type": repos["students"].counts_by_type()},
            device_users={"total": repos["device_users"].count(),
                          "unmapped": repos["device_users"].unmapped_count()},
            events=events,
            queue_pending=repos["queue"].pending_count(),
            last_synced_at=repos["events"].last_synced_at(),
            last_attempts=repos["queue"].recent_attempts(5),
            needs_password_setup=not auth.password_is_configured(settings_repo()),
        )

    # --------------------------------------------------------------- devices --
    @app.route("/api/devices")
    @auth.login_required
    def api_devices():
        rows = repos["devices"].list()
        for d in rows:
            d["online_now"] = _device_online(ctx, d)
            d["event_count"] = repos["devices"].count_events_for(d["id"])
        return ok(devices=rows,
                  last_scan=(ctx.discovery.last_report.to_dict()
                             if ctx.discovery and ctx.discovery.last_report else None))

    @app.route("/api/scan", methods=["POST"])
    @auth.login_required
    def api_scan_now():
        if ctx.discovery is None:
            return fail("discovery service is not running", 503)
        repos["audit"].log(actor=auth.current_actor(), action="scan.manual")
        if request.form.get("blocking") == "1" or (request.is_json and (request.json or {}).get("blocking")):
            report = ctx.discovery.run_scan(blocking=True)
            return ok(report=report.to_dict())
        ctx.discovery.request_scan()
        return ok(started=True)

    @app.route("/api/scan/last")
    @auth.login_required
    def api_scan_last():
        report = ctx.discovery.last_report if ctx.discovery else None
        return ok(report=report.to_dict() if report else None)

    @app.route("/api/devices/<int:device_id>/<action>", methods=["POST"])
    @auth.login_required
    def api_device_action(device_id: int, action: str):
        device = repos["devices"].get_by_id(device_id)
        if not device:
            return fail("device not found", 404)
        actor = auth.current_actor()
        try:
            if action == "register":
                label = (request.form.get("label") or "").strip() or device.get("label")
                repos["devices"].register(device_id, label=label)
                repos["audit"].log(actor=actor, action="device.register",
                                   entity="device", entity_id=str(device.get("serial_number")),
                                   detail=f"ip={device.get('ip_address')}")
                repos["queue"].enqueue_once("register_device", {})
                if ctx.collector:
                    ctx.collector.wake()
            elif action == "disable":
                repos["devices"].set_status(device_id, "disabled")
                repos["audit"].log(actor=actor, action="device.disable",
                                   entity="device", entity_id=str(device.get("serial_number")))
            elif action == "enable":
                repos["devices"].set_status(device_id, "registered")
                repos["audit"].log(actor=actor, action="device.enable",
                                   entity="device", entity_id=str(device.get("serial_number")))
            elif action == "label":
                repos["devices"].set_label(device_id, (request.form.get("label") or "")[:100])
            elif action == "remove":
                repos["devices"].remove(device_id)
                repos["audit"].log(actor=actor, action="device.remove",
                                   entity="device", entity_id=str(device.get("serial_number")))
            elif action == "ems-serial":
                value = (request.form.get("ems_serial")
                         or (request.is_json and (request.json or {}).get("ems_serial"))
                         or "")
                repos["devices"].set_ems_serial(device_id, str(value)[:100])
                repos["devices"].set_users_pushed_hash(device_id, None)
                repos["audit"].log(
                    actor=actor, action="device.ems_serial",
                    entity="device", entity_id=str(device.get("serial_number")),
                    detail=f"EMS serial → {(str(value).strip() or '(hardware serial)')}")
                if ctx.sync_worker:
                    ctx.sync_worker.rebuild_client()
            elif action == "test":
                return _device_test(ctx, device, repos)
            else:
                return fail(f"unknown action {action!r}", 404)
        except ValueError as exc:
            return fail(str(exc))
        return ok(device=repos["devices"].get_by_id(device_id))

    def _device_test(ctx, device, repos):
        from ..discovery.identify import identify_host
        started = time.monotonic()
        result = identify_host(
            device["ip_address"], int(device.get("port") or 4370),
            timeout=ctx.config.device_timeout,
            comm_key=int(device.get("comm_key") or 0),
            adapter_factory=ctx.adapter_factory,
        )
        elapsed = int((time.monotonic() - started) * 1000)
        repos["audit"].log(actor=auth.current_actor(), action="device.test",
                           entity="device", entity_id=str(device.get("serial_number")),
                           detail=f"{device['ip_address']} → identified={result.identified} "
                                  f"({result.error or 'OK'}) in {elapsed}ms")
        if not result.identified:
            return fail(f"connection test failed: {result.error}", 502)
        identity = result.identity
        match = identity.serial_number == (device.get("serial_number") or "")
        return ok(identified=True, serial=identity.serial_number, model=identity.model,
                  firmware=identity.firmware_version, device_time=str(identity.device_time),
                  serial_matches_registered=match, elapsed_ms=elapsed)

    @app.route("/api/devices/manual", methods=["POST"])
    @auth.login_required
    def api_device_manual():
        """Manual IP/serial registration fallback (devices that discovery misses,
        e.g. across subnets/VLANs). Identity verification happens immediately —
        a device is never registered on the strength of an IP alone."""
        from ..discovery.identify import identify_host

        ip = (request.form.get("ip") or "").strip()
        port = int(request.form.get("port") or 4370)
        comm_key = int(request.form.get("comm_key") or 0)
        if not ip:
            return fail("ip is required")
        result = identify_host(ip, port, timeout=ctx.config.device_timeout,
                               comm_key=comm_key, adapter_factory=ctx.adapter_factory)
        if not result.identified or not (result.identity and result.identity.serial_number):
            return fail(f"device at {ip} did not verify its identity: {result.error}", 502)

        identity = result.identity
        existing = repos["devices"].get_by_serial(identity.serial_number)
        if existing:
            old_ip = existing["ip_address"]
            repos["devices"].update_verified_ip(existing["id"], ip)
            repos["audit"].log(actor=auth.current_actor(), action="device.manual_verify",
                               entity="device", entity_id=identity.serial_number,
                               detail=f"verified at {ip} (was {old_ip})")
            return ok(device=repos["devices"].get_by_id(existing["id"]), updated=True)

        device_id, _ = repos["devices"].upsert_identified(
            serial_number=identity.serial_number, model=identity.model,
            mac_address=identity.mac_address, firmware_version=identity.firmware_version,
            platform=identity.platform, ip_address=ip, port=port,
        )
        repos["devices"].update_verified_ip(device_id, ip)
        repos["audit"].log(actor=auth.current_actor(), action="device.manual_add",
                           entity="device", entity_id=identity.serial_number,
                           detail=f"identified at {ip} ({identity.model})")
        return ok(device=repos["devices"].get_by_id(device_id), created=True)

    # --------------------------------------------------------------- students --
    @app.route("/api/students")
    @auth.login_required
    def api_students():
        page = max(1, int(request.args.get("page") or 1))
        size = min(200, max(1, int(request.args.get("page_size") or 50)))
        active_q = request.args.get("active")
        active = None if active_q in (None, "", "all") else active_q == "1"
        person_type = (request.args.get("person_type") or "").strip() or None
        rows, total = repos["students"].list(
            search=request.args.get("search") or None,
            class_name=request.args.get("class") or None,
            section=request.args.get("section") or None,
            person_type=person_type,
            active=active, page=page, page_size=size,
            sort=request.args.get("sort") or "name",
            desc=request.args.get("order") == "desc",
        )
        return ok(students=rows, people=rows, total=total, page=page, page_size=size,
                  classes=repos["students"].classes(),
                  by_type=repos["students"].counts_by_type(),
                  roster_last_sync=settings_repo().get("roster_last_sync"),
                  roster_last_error=settings_repo().get("roster_last_error"))

    @app.route("/api/people")
    @auth.login_required
    def api_people():
        """Directory alias — students, teachers and staff in one list."""
        return api_students()

    @app.route("/api/students/refresh", methods=["POST"])
    @auth.login_required
    def api_students_refresh():
        from ..students.directory import refresh_people
        from ..ems.base import EmsProtocolError, EmsAuthError, EmsUnavailableError
        repos["audit"].log(actor=auth.current_actor(), action="roster.refresh")
        try:
            stats = refresh_people(ctx.ems_client, repos)
        except EmsProtocolError as exc:
            return fail(str(exc), 502)
        except EmsAuthError as exc:
            return fail(f"EMS authentication failed: {exc}", 502)
        except EmsUnavailableError as exc:
            return fail(f"EMS unavailable: {exc}", 502)
        except Exception as exc:
            return fail(f"roster refresh failed: {exc}", 502)
        return ok(stats=stats)

    @app.route("/api/people/refresh", methods=["POST"])
    @auth.login_required
    def api_people_refresh():
        return api_students_refresh()

    # ----------------------------------------------------------- device users --
    @app.route("/api/device-users")
    @auth.login_required
    def api_device_users():
        page = max(1, int(request.args.get("page") or 1))
        size = min(500, max(1, int(request.args.get("page_size") or 200)))
        rows, total = repos["device_users"].list(
            device_serial=request.args.get("device_serial") or None,
            search=request.args.get("search") or None,
            unmapped_only=request.args.get("unmapped") == "1",
            page=page, page_size=size,
        )
        return ok(device_users=rows, total=total, page=page, page_size=size,
                  unmapped=repos["device_users"].unmapped_count(),
                  devices=repos["devices"].list_active())

    @app.route("/api/device-users/read", methods=["POST"])
    @auth.login_required
    def api_device_users_read():
        """Read one terminal's user table right now (read-only on the device)."""
        payload = request.get_json(silent=True) or request.form
        device_id = int(payload.get("device_id") or 0)
        serial = str(payload.get("serial") or "").strip()
        device = repos["devices"].get_by_id(device_id) if device_id else None
        if device is None and serial:
            device = repos["devices"].get_by_serial(serial)
        if not device:
            return fail("device not found", 404)
        device_id = device["id"]
        if ctx.collector is None:
            return fail("collector is not running", 503)
        try:
            result = ctx.collector.refresh_users(device_id)
        except ValueError as exc:
            return fail(str(exc))
        except Exception as exc:  # pragma: no cover - device dependent
            return fail(f"could not read the device user table: {exc}", 502)
        repos["audit"].log(actor=auth.current_actor(), action="device_users.read",
                           entity="device", entity_id=str(device.get("serial_number")),
                           detail=json.dumps(result, default=str)[:400])
        if ctx.sync_worker:
            ctx.sync_worker._next_users = 0.0
            ctx.sync_worker.wake()
        return ok(result=result)

    @app.route("/api/device-users/push", methods=["POST"])
    @auth.login_required
    def api_device_users_push():
        """Force publication of the cached user tables to the EMS."""
        if ctx.sync_worker is None:
            return fail("sync worker is not running", 503)
        payload = request.get_json(silent=True) or request.form
        device_id = payload.get("device_id")
        if device_id:
            device = repos["devices"].get_by_id(int(device_id))
            if not device:
                return fail("device not found", 404)
            repos["devices"].set_users_pushed_hash(device["id"], None)
        else:
            for device in repos["devices"].list_active():
                repos["devices"].set_users_pushed_hash(device["id"], None)
        repos["audit"].log(actor=auth.current_actor(), action="device_users.push_requested")
        ctx.sync_worker._next_users = 0.0
        ctx.sync_worker.wake()
        return ok(queued=True)

    @app.route("/api/ems/verify", methods=["GET", "POST"])
    @auth.login_required
    def api_ems_verify():
        """Live proof of what the EMS actually accepts, serial by serial.

        Answers the question the dashboard cannot answer on its own: is the
        server merely shaking hands, or does it genuinely accept attendance
        for the serial this bridge pushes?
        """
        from ..ems.verify import verify_ems_ingestion

        serial_override = ((request.args.get("sn") or "")
                           or ((request.get_json(silent=True) or {}) or {}).get("sn", ""))
        serial_override = str(serial_override).strip()
        report = verify_ems_ingestion(ctx, serial_override or None)
        repos["audit"].log(actor=auth.current_actor(), action="ems.verify",
                           detail=json.dumps(report, default=str)[:800])
        return ok(report=report)

    # --------------------------------------------------------------- mappings --
    @app.route("/api/mappings", methods=["GET", "POST"])
    @auth.login_required
    def api_mappings():
        if request.method == "GET":
            return ok(mappings=repos["mappings"].list(
                device_serial=request.args.get("device_serial") or None
            ))
        payload = request.get_json(silent=True) or request.form
        try:
            mapping_id = mapping_service().assign(
                device_serial=str(payload.get("device_serial") or ""),
                device_user_id=str(payload.get("device_user_id") or ""),
                ems_student_id=str(payload.get("ems_student_id") or ""),
            )
        except MappingError as exc:
            return fail(str(exc))
        return ok(mapping_id=mapping_id)

    @app.route("/api/mappings/auto", methods=["POST"])
    @auth.login_required
    def api_mappings_auto():
        """Bind every device user whose PIN/name matches exactly one person."""
        payload = request.get_json(silent=True) or request.form
        serial = (payload.get("device_serial") or "").strip() or None
        apply_changes = str(payload.get("apply", "1")).lower() not in ("0", "false", "no")
        result = mapping_service().auto_map(device_serial=serial, apply=apply_changes)
        return ok(result=result, matched=len(result["matched"]),
                  ambiguous=len(result["ambiguous"]), unmatched=len(result["unmatched"]))

    @app.route("/api/mappings/<int:mapping_id>", methods=["DELETE"])
    @auth.login_required
    def api_mapping_delete(mapping_id: int):
        try:
            mapping_service().unassign(mapping_id)
        except MappingError as exc:
            return fail(str(exc), 404)
        return ok()

    @app.route("/api/mappings/review")
    @auth.login_required
    def api_mappings_review():
        return ok(review=mapping_service().review_report())

    @app.route("/api/mappings/export.csv")
    @auth.login_required
    def api_mappings_export():
        csv_text = mapping_service().export_csv()
        repos["audit"].log(actor=auth.current_actor(), action="mapping.csv_export")
        return send_file(
            io.BytesIO(csv_text.encode("utf-8-sig")), mimetype="text/csv",
            as_attachment=True, download_name="device-user-mappings.csv",
        )

    @app.route("/api/mappings/import", methods=["POST"])
    @auth.login_required
    def api_mappings_import():
        file = request.files.get("file")
        text = None
        if file:
            text = file.read().decode("utf-8-sig", errors="replace")
        elif request.is_json:
            text = (request.json or {}).get("csv")
        if not text:
            return fail("provide a CSV file (multipart 'file') or JSON {'csv': ...}")
        try:
            result = mapping_service().import_csv(text)
        except MappingError as exc:
            return fail(str(exc))
        return ok(result=result)

    # -------------------------------------------------------------- attendance --
    @app.route("/api/events")
    @auth.login_required
    def api_events():
        page = max(1, int(request.args.get("page") or 1))
        size = min(200, max(5, int(request.args.get("page_size") or 50)))
        rows, total = repos["events"].list_recent(
            limit=size, offset=(page - 1) * size,
            status=request.args.get("status") or None,
            search=request.args.get("search") or None,
        )
        return ok(events=rows, total=total, page=page, page_size=size)

    @app.route("/api/events/retry", methods=["POST"])
    @auth.login_required
    def api_events_retry():
        payload = request.get_json(silent=True) or {}
        ids = [int(i) for i in payload.get("ids", [])][:500]
        updated = repos["events"].retry_now(ids)
        repos["audit"].log(actor=auth.current_actor(), action="events.retry",
                           detail=f"{updated} events re-queued")
        if ctx.sync_worker:
            ctx.sync_worker.wake()
        return ok(updated=updated)

    # -------------------------------------------------------------------- logs --
    @app.route("/api/logs")
    @auth.login_required
    def api_logs():
        page = max(1, int(request.args.get("page") or 1))
        size = min(200, max(5, int(request.args.get("page_size") or 50)))
        rows, total = repos["audit"].list(
            limit=size, offset=(page - 1) * size,
            search=request.args.get("search") or None,
            action=request.args.get("action") or None,
        )
        return ok(logs=rows, total=total, actions=repos["audit"].actions(),
                  attempts=repos["queue"].recent_attempts(20))

    @app.route("/api/logs/app")
    @auth.login_required
    def api_app_logs():
        needle = (request.args.get("search") or "").lower()
        lines = tail_log_file(ctx.log_file(), max_lines=400)
        if needle:
            lines = [ln for ln in lines if needle in ln.lower()]
        return ok(lines=lines[-200:])

    # ---------------------------------------------------------------- settings --
    @app.route("/api/settings", methods=["GET", "POST"])
    @auth.login_required
    def api_settings():
        if request.method == "GET":
            editable = {k: repos["settings"].get(k) for k in DB_MANAGED_KEYS}
            return ok(settings=editable,
                      effective={k: str(getattr(ctx.settings, attr))
                                 for k, (attr, _c) in DB_MANAGED_KEYS.items()},
                      env_base_url=ctx.config.ems_base_url,
                      token_configured=ctx.secret_store.has_ems_token() if ctx.secret_store else False,
                      dashboard_host=ctx.config.dashboard_host,
                      dashboard_port=ctx.config.dashboard_port)
        payload = request.get_json(silent=True) or request.form
        changed = []
        for key in DB_MANAGED_KEYS:
            if key in payload:
                value = str(payload.get(key) or "").strip()[:300]
                if key == "ems_protocol" and value not in ("auto", "iclock", "rest"):
                    return fail("ems_protocol must be auto, iclock or rest")
                old = repos["settings"].get(key)
                if value in ("", "None"):
                    repos["settings"].delete(key)
                elif value != old:
                    repos["settings"].set(key, value)
                changed.append(key)
        if changed:
            repos["audit"].log(actor=auth.current_actor(), action="settings.update",
                               detail=",".join(changed))
            ctx.apply_settings_overrides()
        return ok(changed=changed)

    @app.route("/api/settings/token", methods=["POST"])
    @auth.login_required
    def api_set_token():
        payload = request.get_json(silent=True) or request.form
        token = (payload.get("token") or "").strip()
        if ctx.secret_store is None:
            return fail("credential store is not available", 503)
        if not token:
            ctx.secret_store.delete_ems_token()
            repos["audit"].log(actor=auth.current_actor(), action="settings.token_cleared")
            if ctx.sync_worker:
                ctx.sync_worker.rebuild_client()
            return ok(token_configured=False)
        ctx.secret_store.set_ems_token(token)
        repos["audit"].log(actor=auth.current_actor(), action="settings.token_set",
                           detail="EMS device token updated")
        if ctx.sync_worker:
            ctx.sync_worker.rebuild_client()
            ctx.sync_worker.wake()
        return ok(token_configured=True)

    @app.route("/api/settings/password", methods=["POST"])
    @auth.login_required
    def api_change_password():
        from ..security.passwords import hash_password
        payload = request.get_json(silent=True) or request.form
        current = payload.get("current") or ""
        new = payload.get("new") or payload.get("password") or ""
        confirm = payload.get("confirm") or payload.get("password_confirm") or new
        actor = auth.current_actor()
        if not auth.check_credentials(settings_repo(), actor, current):
            return fail("current password is incorrect", 403)
        if new != confirm:
            return fail("new passwords do not match")
        try:
            hashed = hash_password(new)
        except ValueError as exc:
            return fail(str(exc))
        repos["settings"].set("dashboard_admin_password_hash", hashed, is_secret=True)
        repos["audit"].log(actor=actor, action="dashboard.password_changed")
        return ok()

    @app.route("/api/settings/ems-test", methods=["POST"])
    @auth.login_required
    def api_ems_test():
        from ..ems.probe import probe_ems
        from ..ems.base import EmsError
        base = repos["settings"].get("ems_base_url") or ctx.config.ems_base_url
        report = probe_ems(base, iclock_path=ctx.config.ems_iclock_path,
                           timeout=ctx.config.ems_timeout)
        result = report.to_dict()
        health = None
        if ctx.ems_client is not None:
            try:
                h = ctx.ems_client.health_check()
                health = {"ok": h.ok, "detail": h.detail, "server_time": h.server_time}
            except EmsError as exc:
                health = {"ok": False, "detail": str(exc)[:300]}
        repos["audit"].log(actor=auth.current_actor(), action="settings.ems_test",
                           detail=f"{base} → probe={report.recommended_protocol}, "
                                  f"health={'n/a' if health is None else health.get('ok')}")
        return ok(probe=result, health=health)

    @app.route("/api/backup", methods=["POST"])
    @auth.login_required
    def api_backup():
        from ..paths import backup_dir
        target = backup_dir() / f"bridge-backup-{time.strftime('%Y%m%d-%H%M%S')}.db"
        repos["db"].backup_to(target)
        repos["audit"].log(actor=auth.current_actor(), action="backup.created",
                           detail=str(target))
        return ok(backup=str(target))

    @app.route("/api/diagnostics/export", methods=["POST"])
    @auth.login_required
    def api_diagnostics_export():
        path = build_diagnostic_zip(ctx)
        repos["audit"].log(actor=auth.current_actor(), action="diagnostics.export",
                           detail=path.name)
        return ok(file=path.name)

    @app.route("/api/diagnostics/download/<name>")
    @auth.login_required
    def api_diagnostics_download(name: str):
        import re
        if not re.fullmatch(r"bridge-diag-[\w.-]+\.zip", name):
            abort(404)
        from ..paths import backup_dir
        path = backup_dir() / name
        if not path.exists():
            abort(404)
        return send_file(path, as_attachment=True)

    @app.errorhandler(403)
    def forbidden(err):
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": getattr(err, "description", "forbidden")}), 403
        return render_template("error.html", code=403,
                               message=getattr(err, "description", "Forbidden")), 403

    @app.errorhandler(404)
    def not_found(err):
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": "not found"}), 404
        return render_template("error.html", code=404, message="Not found"), 404

    return app


# --------------------------------------------------------------------------- --
def _device_online(ctx, device: dict) -> bool:
    if device.get("status") != "registered":
        return False
    if not device.get("last_seen_at"):
        return False
    try:
        from ..timeutils import parse_iso, utcnow
        age = (utcnow() - parse_iso(device["last_seen_at"])).total_seconds()
    except Exception:
        return False
    return age < max(120, 3 * ctx.settings.collect_interval)


def tail_log_file(path: Path | None, max_lines: int = 400) -> list[str]:
    if not path or not Path(path).exists():
        return []
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 256 * 1024))
            text = fh.read().decode("utf-8", errors="replace")
        lines = text.splitlines()
        return lines[-max_lines:]
    except OSError:
        return []


def build_diagnostic_zip(ctx) -> Path:
    """A support bundle with secrets redacted (never contains tokens/passwords)."""
    from ..paths import backup_dir

    target = backup_dir() / f"bridge-diag-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    target.parent.mkdir(parents=True, exist_ok=True)

    snapshot_db = backup_dir() / "diag-temp.db"
    ctx.repos["db"].backup_to(snapshot_db)

    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(snapshot_db, "bridge.db")
        log_path = ctx.log_file()
        if log_path and Path(log_path).exists():
            zf.write(log_path, "logs/bridge.log")
        overview = {
            "version": ctx.version,
            "config": _redacted_config(ctx),
            "settings": ctx.repos["settings"].all_public(),
            "counts": {
                "devices": ctx.repos["devices"].counts(),
                "events": ctx.repos["events"].counts(),
                "students": ctx.repos["students"].count(),
                "queue_pending": ctx.repos["queue"].pending_count(),
            },
            "ems_status": ctx.ems_status.snapshot(),
            "devices": ctx.repos["devices"].list(),
            "last_scan": (ctx.discovery.last_report.to_dict()
                          if ctx.discovery and ctx.discovery.last_report else None),
            "exported_at": iso_utc(),
        }
        zf.writestr("overview.json", json.dumps(overview, indent=2, default=str))
    snapshot_db.unlink(missing_ok=True)
    for suffix in ("-wal", "-shm"):  # pragma: no cover - platform dependent
        Path(str(snapshot_db) + suffix).unlink(missing_ok=True)
    return target


def _redacted_config(ctx) -> dict:
    cfg = ctx.config
    return {
        "ems_base_url": cfg.ems_base_url,
        "ems_protocol": cfg.ems_protocol,
        "ems_iclock_path": cfg.ems_iclock_path,
        "dashboard_host": cfg.dashboard_host,
        "dashboard_port": cfg.dashboard_port,
        "device_timezone": cfg.device_timezone,
        "collect_interval": cfg.collect_interval,
        "discovery_interval": cfg.discovery_interval,
        "sync_retry_base": cfg.sync_retry_base,
        "roster_interval": cfg.roster_interval,
        "secret_backend": "OS credential store (value not included)",
    }


class DashboardServer:
    """Run the dashboard via waitress in a background thread."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.app = create_app(ctx)
        self._server = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        cfg = self.ctx.config
        return f"http://{cfg.dashboard_host}:{cfg.dashboard_port}"

    def start(self) -> None:
        import waitress

        cfg = self.ctx.config
        self._server = waitress.create_server(
            self.app, host=cfg.dashboard_host, port=cfg.dashboard_port,
            threads=8, clear_untrusted_proxy_headers=True,
        )
        self._thread = threading.Thread(target=self._server.run, name="dashboard", daemon=True)
        self._thread.start()
        log.info("dashboard listening on %s (LAN access %s)",
                 self.url, "ENABLED" if cfg.dashboard_allow_lan else "disabled")

    def stop(self) -> None:
        if self._server is not None:
            try:
                self._server.close()
            except Exception:
                pass
