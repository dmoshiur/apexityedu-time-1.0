"""Read-only self-check: database, credential store, EMS and last errors.

The three layers that fail independently in the field are:

1. **SQLite** — a locked/corrupt/missing database or a pending migration.
2. **The device token** — Windows Credential Manager unavailable, or a token
   the EMS rejects (``401/403``).
3. **The EMS backend** — DNS/TLS, an unregistered serial, a missing ``POST``
   route, or ADMS commands the server keeps re-queueing.

``doctor`` answers those in one pass and returns an actionable checklist. It
never writes to a terminal, never posts attendance, never changes settings and
never prints or stores a token. ``network=False`` skips every EMS call so the
report can be generated offline or embedded in a support bundle.

CLI: ``python -m apexity_bridge doctor [--no-network] [--json]``
"""
from __future__ import annotations

import logging
import sqlite3
import tempfile
from pathlib import Path

from . import __version__
from .database.migrations import CURRENT_VERSION
from .ems.base import EmsError
from .timeutils import iso_utc

log = logging.getLogger("apexity_bridge.diagnostics")

REQUIRED_TABLES = {
    "devices", "attendance_events", "device_users_cache", "device_user_mappings",
    "students_cache", "sync_queue", "sync_attempts", "application_settings",
    "audit_logs", "schema_migrations",
}

ERROR_WORDS = ("traceback", "critical", "celery", " 500 ", "error:")
MAX_LOG_ERROR_SAMPLES = 5


# --------------------------------------------------------------------------- --
def _add(checks: list[dict], check_id: str, status: str, title: str,
         detail: str = "", fix: str = "") -> dict:
    check = {"id": check_id, "status": status, "title": title,
             "detail": detail, "fix": fix}
    checks.append(check)
    return check


def _tail(path: Path | None, max_lines: int = 400) -> list[str]:
    if not path or not Path(path).exists():
        return []
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 256 * 1024))
            text = fh.read().decode("utf-8", errors="replace")
        return text.splitlines()[-max_lines:]
    except OSError:
        return []


# ------------------------------------------------------------------- database --
def _database_checks(ctx, checks: list[dict], report: dict) -> None:
    db = ctx.repos["db"]
    path = Path(ctx.config.db_path)
    info: dict = {"path": str(path), "exists": path.exists(),
                  "size_bytes": path.stat().st_size if path.exists() else 0}
    report["database"] = info

    if not path.exists():
        _add(checks, "database.file", "fail", "Database file is missing",
             f"{path} does not exist",
             "Run `python -m apexity_bridge init-db`, then restore from "
             "backups if this was an existing installation.")
    else:
        _add(checks, "database.file", "ok", "Database file exists",
             f"{path} ({info['size_bytes']:,} bytes)")

    try:
        integrity = db.check_integrity()
        info["integrity"] = "ok" if integrity else "problems"
        if integrity:
            _add(checks, "database.integrity", "ok", "SQLite integrity check passed")
        else:
            _add(checks, "database.integrity", "fail",
                 "SQLite integrity check reported problems",
                 "Run `python -m apexity_bridge backup` now, then follow the "
                 "database restore section of the troubleshooting guide.",
                 "Stop the bridge, copy the last good backup to "
                 "RESTORE_REQUESTED.db, and keep the damaged file for inspection.")
    except (sqlite3.DatabaseError, OSError) as exc:
        info["integrity"] = f"error: {exc}"
        _add(checks, "database.integrity", "fail",
             "Could not read the SQLite database", str(exc)[:300],
             "Check disk space and file permissions; restore a backup if the "
             "file is locked by another process.")

    try:
        rows = db.query("SELECT version FROM schema_migrations ORDER BY version")
        versions = [int(r["version"]) for r in rows]
        info["schema_version"] = max(versions) if versions else 0
        info["schema_expected"] = CURRENT_VERSION
        if info["schema_version"] >= CURRENT_VERSION:
            _add(checks, "database.schema", "ok", "Database schema is up to date",
                 f"version {info['schema_version']}")
        else:
            _add(checks, "database.schema", "fail", "Database schema is behind",
                 f"found v{info['schema_version']}, expected v{CURRENT_VERSION}",
                 "Stop the bridge, make a backup, then start it again — pending "
                 "migrations are applied automatically.")
    except sqlite3.DatabaseError as exc:
        _add(checks, "database.schema", "fail",
             "Could not read the schema version", str(exc)[:300])

    try:
        present = {r["name"] for r in db.query(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        missing = sorted(REQUIRED_TABLES - present)
        info["missing_tables"] = missing
        if missing:
            _add(checks, "database.tables", "fail", "Required tables are missing",
                 ", ".join(missing), "Restore a backup from a working version.")
        else:
            _add(checks, "database.tables", "ok", "All required tables exist")
    except sqlite3.DatabaseError as exc:
        _add(checks, "database.tables", "fail", "Could not list tables", str(exc)[:300])

    journal = None
    try:
        row = db.query_one("PRAGMA journal_mode")
        journal = str(row[0]).lower() if row else None
        info["journal_mode"] = journal
        if journal == "wal":
            _add(checks, "database.journal", "ok", "WAL journal mode is active")
        else:
            _add(checks, "database.journal", "warn", "WAL journal mode is not active",
                 f"journal_mode={journal!r}",
                 "Keep the database on a local NTFS disk (not a network share or "
                 "OneDrive-synced folder) and restart the bridge.")
    except sqlite3.DatabaseError as exc:
        info["journal_mode"] = f"error: {exc}"
        _add(checks, "database.journal", "warn", "Could not read journal mode",
             str(exc)[:300])

    # Exercise a real write transaction without leaving anything behind.
    try:
        with db.transaction() as conn:
            conn.execute("CREATE TEMP TABLE IF NOT EXISTS _apexity_doctor_probe(x)")
            conn.execute("DROP TABLE _apexity_doctor_probe")
        _add(checks, "database.writable", "ok", "Database accepts write transactions")
    except (sqlite3.DatabaseError, OSError) as exc:
        _add(checks, "database.writable", "fail", "Database is not writable",
             str(exc)[:300],
             "Check free disk space, antivirus/EDR locks and folder permissions "
             "for the account that runs the bridge service.")

    try:
        counts = {
            "devices": ctx.repos["devices"].counts(),
            "events": ctx.repos["events"].counts(),
            "people": ctx.repos["students"].count(),
            "device_users": ctx.repos["device_users"].count(),
            "mappings": ctx.repos["mappings"].count(),
            "queue_pending": ctx.repos["queue"].pending_count(),
        }
        row = db.query_one(
            "SELECT COUNT(*) AS c FROM sync_queue WHERE status = 'failed'")
        counts["queue_failed"] = int(row["c"]) if row else 0
        info["counts"] = counts
        events = counts["events"]
        detail = (f"{events.get('pending', 0)} pending · {events.get('synced', 0)} synced · "
                  f"{events.get('failed', 0)} failed · {events.get('review', 0)} review · "
                  f"{events.get('rejected', 0)} rejected")
        status = "warn" if events.get("failed") or events.get("review") else "ok"
        _add(checks, "database.events", status, "Attendance queue", detail,
             "Open Attendance → retry failed/review rows after fixing the cause "
             "shown in Last error." if status == "warn" else "")
        if counts["queue_failed"]:
            _add(checks, "database.task_queue", "warn", "Some background tasks are failed",
                 f"{counts['queue_failed']} task(s) reached max attempts",
                 "Inspect Logs → Sync attempts; fix the EMS error, then press "
                 "Sync now to re-enqueue.")
        else:
            _add(checks, "database.task_queue", "ok", "Background task queue is healthy",
                 f"{counts['queue_pending']} pending task(s)")

        # Last results the bridge actually received from the EMS. This is the
        # local half of the evidence needed to correlate a server-side Celery
        # traceback with one upload attempt.
        attempts = ctx.repos["queue"].recent_attempts(limit=10)
        recent_failures = [a for a in attempts
                           if (a.get("error") or a.get("result") == "failed")]
        info["recent_sync_attempts"] = [
            {k: a.get(k) for k in ("attempt_type", "event_count", "result",
                                   "detail", "error", "attempted_at")}
            for a in attempts[:5]
        ]
        if recent_failures:
            latest = recent_failures[0]
            _add(checks, "database.sync_attempts", "warn",
                 "Recent sync attempt(s) failed",
                 f"{latest.get('attempt_type')}: {latest.get('error') or latest.get('detail')}"
                 [:300],
                 "This is what the bridge received. Compare its timestamp with "
                 "the server/Celery log for the same SN; the server-side trace "
                 "is fixed on the EMS, not on this PC.")
    except sqlite3.DatabaseError as exc:
        _add(checks, "database.events", "warn", "Could not read row counts", str(exc)[:300])

    # Backups are the difference between a bad afternoon and a lost register.
    try:
        from .paths import backup_dir
        folder = backup_dir()
        backups = sorted(folder.glob("bridge-backup-*.db"), key=lambda p: p.stat().st_mtime) \
            if folder.exists() else []
        if backups:
            newest = backups[-1]
            info["latest_backup"] = str(newest)
            _add(checks, "database.backup", "ok", "A database backup exists",
                 f"{newest.name} ({newest.stat().st_size:,} bytes)")
        else:
            _add(checks, "database.backup", "warn", "No database backup found",
                 f"looked in {folder}",
                 "Press Settings → Backup database now (or run `backup` in the CLI).")
    except OSError as exc:
        _add(checks, "database.backup", "warn", "Could not inspect backups", str(exc)[:300])

    try:
        with tempfile.NamedTemporaryFile(dir=str(path.parent), prefix=".doctor-",
                                         delete=True):
            pass
        _add(checks, "storage.write", "ok", "Data directory is writable",
             str(path.parent))
    except OSError as exc:
        _add(checks, "storage.write", "fail", "Data directory is not writable",
             f"{path.parent}: {exc}",
             "Run the bridge as the same user that installed it, or grant write "
             "permission on %ProgramData%\\ApexityEduBridge.")


# --------------------------------------------------------------------- secrets --
def _secret_checks(ctx, checks: list[dict], report: dict) -> None:
    store = ctx.secret_store
    info: dict = {"available": store is not None}
    report["secrets"] = info
    if store is None:
        _add(checks, "secrets.store", "warn", "Credential store is not configured",
             "the bridge cannot read or save an EMS device token",
             "Start the bridge through the normal service/tray entry point so "
             "Windows Credential Manager is available.")
        return
    try:
        info["backend"] = getattr(store, "backend_name", type(store).__name__)
    except Exception:  # pragma: no cover - defensive
        info["backend"] = type(store).__name__
    try:
        token = store.get_ems_token()
        info["token_configured"] = bool(token)
        if token:
            _add(checks, "secrets.token", "ok", "EMS device token is stored",
                 f"in {info['backend']} (value never printed)")
        else:
            _add(checks, "secrets.token", "warn", "No EMS device token is stored",
                 "the EMS may reject REST calls with 401/403",
                 "Settings → paste the device token → Save token "
                 "(or `python -m apexity_bridge set-token`).")
    except Exception as exc:              # credential store unavailable/locked
        info["token_configured"] = None
        info["error"] = str(exc)[:300]
        _add(checks, "secrets.token", "warn", "Could not read the EMS token",
             str(exc)[:300],
             "Unlock Windows Credential Manager for this user; if the bridge runs "
             "as a service, store the token under that service account.")


# ---------------------------------------------------------------------- config --
def _config_checks(ctx, checks: list[dict], report: dict) -> None:
    cfg = ctx.config
    errors = cfg.validate()
    report["config"] = {
        "ems_base_url": cfg.ems_base_url,
        "ems_protocol": cfg.ems_protocol,
        "iclock_path": cfg.ems_iclock_path,
        "dashboard": f"{cfg.dashboard_host}:{cfg.dashboard_port}",
    }
    if errors:
        _add(checks, "config.valid", "fail", "Configuration is invalid",
             "; ".join(errors), "Fix .env (or Settings) and restart the bridge.")
    else:
        _add(checks, "config.valid", "ok", "Configuration is valid")


# ------------------------------------------------------------------------- EMS --
def _ems_checks(ctx, checks: list[dict], report: dict) -> None:
    from .ems.iclock import command_is_read_only, parse_command
    from .ems.probe import probe_ems

    base = ctx.settings.ems_base_url
    info: dict = {"base_url": base, "protocol": ctx.settings.ems_protocol,
                  "client": ctx.ems_client is not None,
                  "live_status": ctx.ems_status.snapshot()}
    report["ems"] = info

    try:
        probe = probe_ems(base, iclock_path=ctx.config.ems_iclock_path,
                          timeout=ctx.config.ems_timeout)
        info["probe"] = probe.to_dict()
        if probe.reachable:
            _add(checks, "ems.probe", "ok", "EMS is reachable",
                 f"iClock={probe.iclock_verdict}, REST={probe.rest_verdict}; "
                 f"recommended={probe.recommended_protocol or 'none'}")
        else:
            _add(checks, "ems.probe", "fail", "EMS is unreachable",
                 f"{base}: {'; '.join(probe.errors) or 'no HTTP response'}"[:300],
                 "Check DNS, the system clock, TLS root certificates, the school "
                 "firewall/proxy and that https://apexityedu.com opens in a "
                 "browser on this PC.")
    except Exception as exc:              # noqa: BLE001 - report, never raise
        info["probe"] = {"error": str(exc)[:300]}
        _add(checks, "ems.probe", "fail", "EMS probe failed", str(exc)[:300])

    client = ctx.ems_client
    if client is None:
        _add(checks, "ems.client", "fail", "No EMS client was constructed",
             "check EMS_BASE_URL / EMS_PROTOCOL and the application log",
             "Save Settings again or restart the bridge; the log contains the "
             "construction error.")
        return

    try:
        health = client.health_check()
        info["health"] = {"ok": health.ok, "detail": health.detail}
        _add(checks, "ems.health", "ok" if health.ok else "fail",
             "EMS health/handshake check",
             (health.detail or "ok")[:300],
             "" if health.ok else
             "A rejected handshake means the URL/path or the serial whitelist is "
             "wrong; confirm EMS_ICLOCK_PATH=/iclock.")
    except EmsError as exc:
        info["health"] = {"ok": False, "detail": str(exc)[:300]}
        _add(checks, "ems.health", "fail", "EMS health check failed", str(exc)[:300],
             "401/403 → token/serial problem. Timeout/TLS → network problem. "
             "HTML/JSON → wrong URL or the EMS does not implement iClock.")

    devices = ctx.repos["devices"].list_active()
    info["devices"] = []
    if not devices:
        _add(checks, "ems.serials", "warn", "No registered terminal",
             "discovery/registration has not completed yet",
             "Devices → register the K40, then run the self-check again.")
        return

    presence = {}
    for device in devices:
        from .database.repositories import ems_serial_for
        target = ems_serial_for(device)
        entry = {"hardware_serial": device.get("serial_number"),
                 "ems_serial": target,
                 "override": target != device.get("serial_number"),
                 "handshake": False, "poll": False, "commands": []}
        try:
            text = client.handshake(target)
            entry["handshake"] = True
            entry["handshake_reply"] = (text or "")[:120]
            if getattr(client, "protocol_name", "") == "iclock":
                commands = client.poll_commands(target)
                entry["poll"] = True
                entry["commands"] = commands
                parsed = [parse_command(c) for c in commands]
                entry["commands_read_only"] = [
                    c.body for c in parsed if command_is_read_only(c.body)]
                entry["commands_refused"] = [
                    c.body for c in parsed if not command_is_read_only(c.body)]
                if entry["commands_refused"]:
                    _add(checks, f"ems.commands.{target}", "warn",
                         f"EMS queued command(s) the bridge refuses for {target}",
                         "; ".join(entry["commands_refused"])[:300],
                         "The bridge is read-only and will not CLEAR/REBOOT/SET a "
                         "terminal. New versions acknowledge the refusal once; ask "
                         "the EMS admin to cancel the command, or set "
                         "EMS_EXECUTE_COMMANDS=0.")
        except EmsError as exc:
            entry["error"] = str(exc)[:300]
        info["devices"].append(entry)
        presence[target] = entry

    bad = [e for e in info["devices"] if not e.get("handshake")]
    if bad:
        _add(checks, "ems.serials", "fail", "Some serials are not accepted by the EMS",
             "; ".join(f"{e['ems_serial']}: {e.get('error', 'handshake failed')}"
                       for e in bad)[:400],
             "Register the exact serial shown under ApexityEdu → Biometric Devices, "
             "or set the device's EMS serial in the bridge (Devices → EMS serial).")
    else:
        _add(checks, "ems.serials", "ok", "Every registered serial handshakes",
             ", ".join(e["ems_serial"] for e in info["devices"]))
    info["presence"] = presence


# ---------------------------------------------------------------------- logs --
def _log_checks(ctx, checks: list[dict], report: dict) -> None:
    lines = _tail(ctx.log_file(), max_lines=400)
    report["log"] = {"path": str(ctx.log_file()) if ctx.log_file() else None,
                     "lines_scanned": len(lines)}
    if not lines:
        _add(checks, "logs.file", "warn", "Application log is empty or not configured",
             str(ctx.log_file()),
             "Start the bridge normally; logs rotate at 5 MB × 5 files.")
    else:
        samples = [ln for ln in lines
                   if any(word in ln.lower() for word in ERROR_WORDS)]
        report["log"]["error_samples"] = samples[-MAX_LOG_ERROR_SAMPLES:]
        if samples:
            _add(checks, "logs.errors", "warn", "Recent errors in the application log",
                 samples[-1][:300],
                 "Open Logs → application log, or Export diagnostics for support.")
        else:
            _add(checks, "logs.errors", "ok", "No error lines in the recent log tail")

    try:
        refused, refused_total = ctx.repos["audit"].list(
            action="ems.command_refused", limit=5)
        auth, auth_total = ctx.repos["audit"].list(
            action="ems.auth_failed", limit=5)
        report["audit"] = {
            "command_refused_total": refused_total,
            "recent_command_refused": refused,
            "auth_failed_total": auth_total,
        }
        if auth_total:
            _add(checks, "logs.auth", "warn", "EMS rejected credentials before",
                 f"{auth_total} auth failure(s) recorded",
                 "Re-save the device token, then run verify-ems. Keep the token "
                 "out of chat, email and screenshots.")
    except Exception as exc:  # pragma: no cover - defensive
        _add(checks, "logs.auth", "warn", "Could not read the audit log", str(exc)[:300])


# ---------------------------------------------------------------------- report --
def build_doctor_report(ctx, *, network: bool = True) -> dict:
    """Return a JSON-serialisable self-check report (never mutates anything)."""
    checks: list[dict] = []
    report: dict = {
        "version": __version__,
        "generated_at": iso_utc(),
        "network_checked": bool(network),
        "checks": checks,
    }
    for step in (_database_checks, _secret_checks, _config_checks):
        try:
            step(ctx, checks, report)
        except Exception as exc:          # noqa: BLE001 - a doctor never crashes
            log.exception("doctor step %s crashed", step.__name__)
            _add(checks, f"doctor.{step.__name__}", "warn",
                 "A self-check step could not complete", str(exc)[:300])
    if network:
        try:
            _ems_checks(ctx, checks, report)
        except Exception as exc:          # noqa: BLE001
            log.exception("doctor EMS step crashed")
            _add(checks, "ems.check", "warn", "EMS self-check could not complete",
                 str(exc)[:300])
    else:
        _add(checks, "ems.skipped", "warn", "EMS connectivity was not checked",
             "--no-network / offline mode",
             "Run the self-check again without --no-network when the PC is online.")
    try:
        _log_checks(ctx, checks, report)
    except Exception as exc:              # noqa: BLE001
        _add(checks, "logs.check", "warn", "Log inspection failed", str(exc)[:300])

    failures = [c for c in checks if c["status"] == "fail"]
    warnings = [c for c in checks if c["status"] == "warn"]
    report["failures"] = failures
    report["warnings"] = warnings
    report["ok"] = not failures
    report["status"] = "fail" if failures else ("warn" if warnings else "ok")
    report["server_note"] = (
        "Celery and the queue workers belong to the ApexityEdu server, not to "
        "this bridge. A Celery error in the EMS log must be fixed there; use "
        "verify-ems and the 'Recent sync attempts' table to prove what the "
        "server answered. See docs/SETTINGS_AND_SYNC_TROUBLESHOOTING.md."
    )
    return report


def format_doctor_report(report: dict) -> str:
    """Human-readable text used by the CLI, dashboard and desktop app."""
    label = {"ok": "OK", "warn": "WARN", "fail": "FAIL"}
    overall = {"ok": "OK", "warn": "ATTENTION NEEDED", "fail": "FAILED"}[
        report.get("status", "warn")]
    lines = [
        f"ApexityEdu Bridge self-check — v{report.get('version')} "
        f"({report.get('generated_at')})",
        f"Overall: {overall}",
        "",
    ]
    for check in report.get("checks", []):
        if check["status"] == "ok" and len(report.get("checks", [])) > 18:
            continue  # keep long reports readable; failures/warnings always show
        lines.append(f"[{label.get(check['status'], '?')}] {check['id']}: {check['title']}")
        if check.get("detail"):
            lines.append(f"       {check['detail']}")
        if check["status"] != "ok" and check.get("fix"):
            lines.append(f"       fix: {check['fix']}")
    db = report.get("database") or {}
    if db.get("counts"):
        c = db["counts"]
        devices = c.get("devices") if isinstance(c.get("devices"), dict) else {}
        events = c.get("events") if isinstance(c.get("events"), dict) else {}
        lines += ["", "Database: " + str(db.get("path")),
                  f"  devices={devices.get('total', 0)}  people={c.get('people', 0)}  "
                  f"device_users={c.get('device_users', 0)}  "
                  f"events={events.get('total', 0)} "
                  f"(pending {events.get('pending', 0)}, synced {events.get('synced', 0)}, "
                  f"failed {events.get('failed', 0)})"]
    ems = report.get("ems") or {}
    if ems:
        lines += ["", f"EMS: {ems.get('base_url')} ({ems.get('protocol')})"]
        for device in ems.get("devices") or []:
            state = "handshake OK" if device.get("handshake") else "HANDSHAKE FAILED"
            lines.append(
                f"  SN {device.get('ems_serial')} "
                f"(hardware {device.get('hardware_serial')}"
                f"{', override' if device.get('override') else ''}): {state}")
            if device.get("commands_refused"):
                lines.append("    commands refused: "
                             + "; ".join(device["commands_refused"])[:300])
            if device.get("error"):
                lines.append(f"    error: {device['error']}")
    if report.get("server_note"):
        lines += ["", report["server_note"]]
    return "\n".join(lines)
