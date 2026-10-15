"""Reference implementation of the ApexityEdu EMS bridge API (Option B).

CONTENTS
    GET  /api/v1/bridge/health
    POST /api/v1/bridge/devices/register
    GET  /api/v1/bridge/students?cursor=&limit=
    POST /api/v1/bridge/attendance/batch
    GET  /iclock/cdata           (Option A handshake stub)
    POST /iclock/cdata           (Option A ATTLOG upload)

This file is intentionally self-contained so the EMS team can read the exact
semantics in code, and so the school can run it temporarily as a relay:
    pip install flask
    EMS_TOKEN=... python docs/reference/ems_reference_api.py
It is NOT a production EMS component — the real implementation must live
inside apexityedu.com behind HTTPS with its own student tables.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from functools import wraps

from flask import Flask, g, jsonify, request

DB = os.environ.get("EMS_DB", "ems_reference.db")
TOKEN = os.environ.get("EMS_TOKEN", "dev-token-change-me")

app = Flask(__name__)

# --------------------------------------------------------------------------- --
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS bridge_devices (
    serial TEXT PRIMARY KEY, model TEXT, firmware TEXT, label TEXT,
    registered_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS students (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, admission_no TEXT, roll_no TEXT,
    class TEXT, section TEXT, active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS attendance (
    remote_id INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT UNIQUE NOT NULL,
    device_serial TEXT NOT NULL, device_user_id TEXT NOT NULL,
    ems_student_id TEXT, ts TEXT NOT NULL, ts_tz TEXT NOT NULL,
    status INTEGER, verify INTEGER, created_at TEXT NOT NULL
);
"""


def init_db():
    with closing(sqlite3.connect(DB)) as db:
        db.executescript(SCHEMA)
        db.commit()


def require_token(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        auth = request.headers.get("Authorization", "")
        iclock_token_ok = (
            request.args.get("AuthToken") == TOKEN
            or request.headers.get("X-Auth-Token") == TOKEN
        )
        if not request.path.startswith("/iclock/") and auth != f"Bearer {TOKEN}":
            return jsonify({"error": "unauthorized"}), 401
        if request.path.startswith("/iclock/") and not iclock_token_ok:
            return jsonify({"error": "unauthorized"}), 401
        return fn(*a, **kw)
    return wrapper


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- --
@app.get("/api/v1/bridge/health")
@require_token
def health():
    return jsonify(ok=True, server_time=now())


@app.post("/api/v1/bridge/devices/register")
@require_token
def register_device():
    body = request.get_json(silent=True) or {}
    serial = str(body.get("serial", "")).strip()
    if not serial:
        return jsonify({"error": "serial required"}), 400
    db = get_db()
    db.execute(
        "INSERT INTO bridge_devices (serial, model, firmware, label, registered_at)"
        " VALUES (?,?,?,?,?)"
        " ON CONFLICT(serial) DO UPDATE SET model=excluded.model,"
        " firmware=excluded.firmware, label=excluded.label",
        (serial, body.get("model"), body.get("firmware"), body.get("label"), now()),
    )
    db.commit()
    return jsonify(ok=True, device_id=f"dev-{serial}")


@app.get("/api/v1/bridge/students")
@require_token
def students():
    limit = min(1000, int(request.args.get("limit", 500)))
    cursor = int(request.args.get("cursor") or 0)
    rows = get_db().execute(
        "SELECT * FROM students ORDER BY rowid LIMIT ? OFFSET ?", (limit, cursor)
    ).fetchall()
    nxt = cursor + len(rows)
    has_more = get_db().execute("SELECT COUNT(*) c FROM students").first()["c"] > nxt
    return jsonify(
        students=[dict(r) for r in rows],
        next_cursor=str(nxt) if has_more and rows else None,
    )


@app.post("/api/v1/bridge/attendance/batch")
@require_token
def attendance_batch():
    body = request.get_json(silent=True) or {}
    events = body.get("events")
    if not isinstance(events, list) or not events:
        return jsonify({"error": "events[] required"}), 400
    if len(events) > 500:
        return jsonify({"error": "max 500 events per batch"}), 400

    db = get_db()
    results = []
    for ev in events:
        key = str(ev.get("idempotency_key", ""))
        ts = str(ev.get("timestamp", ""))
        user = str(ev.get("device_user_id", "")).strip()
        serial = str(ev.get("device_serial", "")).strip()
        if not key or len(key) != 64 or not ts or not user or not serial:
            results.append({"idempotency_key": key, "status": "rejected",
                            "reason": "missing/invalid required fields"})
            continue
        # server-side sanity: reject obvious clock-corrupt records
        try:
            tsdt = datetime.fromisoformat(ts)
            if (datetime.now() - tsdt).total_seconds() < -48 * 3600:
                raise ValueError("future timestamp")
        except ValueError:
            results.append({"idempotency_key": key, "status": "rejected",
                            "reason": "untrustworthy device timestamp"})
            continue
        existing = db.execute(
            "SELECT remote_id FROM attendance WHERE idempotency_key = ?", (key,)
        ).first()
        if existing:
            results.append({"idempotency_key": key, "status": "duplicate",
                            "remote_id": existing["remote_id"]})
            continue
        cur = db.execute(
            "INSERT INTO attendance (idempotency_key, device_serial, device_user_id,"
            " ems_student_id, ts, ts_tz, status, verify, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (key, serial, user, ev.get("ems_student_id"), ts,
             ev.get("timestamp_tz", "Asia/Dhaka"), ev.get("status"),
             ev.get("verify"), now()),
        )
        results.append({"idempotency_key": key, "status": "accepted",
                        "remote_id": cur.lastrowid})
    db.commit()
    return jsonify(results=results)


# ------------------------------------------------------------- iClock stub ---
@app.route("/iclock/cdata", methods=["GET", "POST"])
@require_token
def iclock_cdata():
    sn = request.args.get("SN", "").strip()
    if request.method == "GET":
        return "GET OPTION from: EMS-REFERENCE\nStamp=0\nDelay=30\nTransTimes=00:00;14:05\n"
    if request.args.get("table") != "ATTLOG":
        return "OK"
    body = request.get_data(as_text=True)
    accepted = 0
    db = get_db()
    for line in body.splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        pin, ts, status, verify = parts[0], parts[1], parts[2], parts[3]
        key = hashlib.sha256(f"iclock|{sn}|{pin}|{ts}|{status}".encode()).hexdigest()
        try:
            db.execute(
                "INSERT INTO attendance (idempotency_key, device_serial, device_user_id,"
                " ems_student_id, ts, ts_tz, status, verify, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (key, sn, pin, None, ts.replace(" ", "T")[:19], "Asia/Dhaka",
                 int(status or 0), int(verify or 0), now()),
            )
            accepted += 1
        except sqlite3.IntegrityError:
            pass  # duplicate — idempotent re-push, safe to ignore
    db.commit()
    return f"OK: {accepted}"


@app.get("/iclock/getrequest")
@require_token
def iclock_getrequest():
    return "OK"


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=8443)  # put behind HTTPS in real deployments!
