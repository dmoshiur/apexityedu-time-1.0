"""Typed repository layer over the raw Database. All SQL lives here.

Conventions:
* All timestamps written here use ``iso_utc()`` unless they are
  device-reported values stored verbatim.
* Writes that must be atomic are wrapped in ``db.transaction()``.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from typing import Iterable

from ..timeutils import iso_utc
from .connection import Database

log = logging.getLogger("apexity_bridge.database")


def _row_to_dict(row) -> dict:
    return {k: row[k] for k in row.keys()}


# Person types the EMS directory can contain. "student" is only one of them —
# schools also punch in teachers and non-teaching staff, and the bridge must be
# able to bind a device user to any of them.
PERSON_TYPES = ("student", "teacher", "staff", "other")


def ems_serial_for(device: dict) -> str:
    """The serial number the EMS knows this terminal by.

    ``devices.ems_serial`` (operator override) wins over the hardware serial.
    iClock servers accept ATTLOG uploads only for a serial they have on file.
    """
    return (device.get("ems_serial") or device.get("serial_number") or "").strip()


def person_label(person: dict | None) -> str:
    """Human label used in lists/logs: 'Name (Class Section)' style."""
    if not person:
        return ""
    name = person.get("name") or person.get("ems_student_id") or ""
    bits = [str(b) for b in (person.get("class_name"), person.get("section")) if b]
    if person.get("designation"):
        bits = [str(person["designation"])]
    return f"{name} ({' '.join(bits)})" if bits else str(name)


# --------------------------------------------------------------------------
class DeviceRepository:
    def __init__(self, db: Database):
        self.db = db

    def get_by_serial(self, serial: str) -> dict | None:
        row = self.db.query_one("SELECT * FROM devices WHERE serial_number = ?", (serial,))
        return _row_to_dict(row) if row else None

    def get_by_id(self, device_id: int) -> dict | None:
        row = self.db.query_one("SELECT * FROM devices WHERE id = ?", (device_id,))
        return _row_to_dict(row) if row else None

    def find_by_mac(self, mac: str) -> list[dict]:
        rows = self.db.query(
            "SELECT * FROM devices WHERE UPPER(mac_address) = UPPER(?)", (mac,)
        )
        return [_row_to_dict(r) for r in rows]

    def list(self, status: str | None = None) -> list[dict]:
        if status:
            rows = self.db.query(
                "SELECT * FROM devices WHERE status = ? ORDER BY label, id", (status,)
            )
        else:
            rows = self.db.query("SELECT * FROM devices ORDER BY label, id")
        return [_row_to_dict(r) for r in rows]

    def list_active(self) -> list[dict]:
        """Devices the collectors should talk to: registered and not disabled."""
        return self.list("registered")

    def upsert_identified(
        self,
        *,
        serial_number: str,
        model: str | None = None,
        mac_address: str | None = None,
        firmware_version: str | None = None,
        platform: str | None = None,
        ip_address: str | None = None,
        port: int = 4370,
        mark_seen: bool = True,
    ) -> tuple[int, bool]:
        """Insert-or-refresh a device identified by serial. Returns (id, created)."""
        now = iso_utc()
        with self.db.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM devices WHERE serial_number = ?", (serial_number,)
            ).fetchone()
            if existing:
                updates = {
                    "updated_at": now,
                    **({"last_seen_at": now} if mark_seen else {}),
                }
                if model:
                    updates["model"] = model
                if mac_address:
                    updates["mac_address"] = mac_address
                if firmware_version:
                    updates["firmware_version"] = firmware_version
                if platform:
                    updates["platform"] = platform
                assignments = ", ".join(f"{k} = ?" for k in updates)
                conn.execute(
                    f"UPDATE devices SET {assignments} WHERE id = ?",
                    (*updates.values(), existing["id"]),
                )
                return existing["id"], False
            cur = conn.execute(
                """INSERT INTO devices
                   (serial_number, model, mac_address, firmware_version, platform,
                    ip_address, port, status, first_seen_at, last_seen_at, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?, 'pending', ?, ?, ?, ?)""",
                (
                    serial_number, model, mac_address, firmware_version, platform,
                    ip_address, port, now, now if mark_seen else None, now, now,
                ),
            )
            return cur.lastrowid, True

    def create_pending(
        self, *, ip_address: str, port: int = 4370, mac_address: str | None = None,
        model: str | None = None,
    ) -> int:
        """Create an observation-only device row (identification not possible yet)."""
        now = iso_utc()
        with self.db.transaction() as conn:
            cur = conn.execute(
                """INSERT INTO devices
                   (model, mac_address, ip_address, port, status, first_seen_at, created_at, updated_at)
                   VALUES (?,?,?,?, 'pending', ?, ?, ?)""",
                (model, mac_address, ip_address, port, now, now, now),
            )
            return cur.lastrowid

    def update_verified_ip(
        self, device_id: int, ip_address: str, *, mac_address: str | None = None
    ) -> None:
        """Move a device to a new verified IP + mark it seen."""
        now = iso_utc()
        with self.db.transaction() as conn:
            dev = conn.execute("SELECT ip_address FROM devices WHERE id = ?", (device_id,)).fetchone()
            if dev is None:
                raise KeyError(f"device {device_id} not found")
            ip_changed = dev["ip_address"] != ip_address
            conn.execute(
                """UPDATE devices SET ip_address = ?, mac_address = COALESCE(?, mac_address),
                       last_seen_at = ?, updated_at = ?{ip} WHERE id = ?""".format(
                    ip=", ip_updated_at = ?" if ip_changed else ""
                ),
                (ip_address, mac_address, now, now, device_id)
                if not ip_changed
                else (ip_address, mac_address, now, now, now, device_id),
            )

    def register(self, device_id: int, *, label: str | None = None, comm_key: int | None = None) -> None:
        with self.db.transaction() as conn:
            cur = conn.execute(
                """UPDATE devices SET status = 'registered',
                       label = COALESCE(?, label), comm_key = COALESCE(?, comm_key),
                       updated_at = ?
                   WHERE id = ? AND serial_number IS NOT NULL""",
                (label, comm_key, iso_utc(), device_id),
            )
            if cur.rowcount == 0:
                raise ValueError("cannot register a device that has no verified serial number")

    def set_status(self, device_id: int, status: str) -> None:
        self.db.execute(
            "UPDATE devices SET status = ?, updated_at = ? WHERE id = ?",
            (status, iso_utc(), device_id),
        )

    def update_seen(self, device_id: int) -> None:
        self.db.execute(
            "UPDATE devices SET last_seen_at = ?, updated_at = ? WHERE id = ?",
            (iso_utc(), iso_utc(), device_id),
        )

    def set_label(self, device_id: int, label: str) -> None:
        self.db.execute(
            "UPDATE devices SET label = ?, updated_at = ? WHERE id = ?",
            (label, iso_utc(), device_id),
        )

    # -- EMS identity -------------------------------------------------------
    def set_ems_serial(self, device_id: int, ems_serial: str | None) -> None:
        """Serial the EMS knows this device by ('' / None → use hardware serial).

        iClock servers only accept ATTLOG uploads for a serial they have on
        file, and that serial is not always the one the terminal reports. This
        override is the supported way to make the two match.
        """
        value = (ems_serial or "").strip() or None
        self.db.execute(
            "UPDATE devices SET ems_serial = ?, updated_at = ? WHERE id = ?",
            (value, iso_utc(), device_id),
        )

    def set_users_pushed_hash(self, device_id: int, value: str | None) -> None:
        self.db.execute(
            "UPDATE devices SET users_pushed_hash = ?, updated_at = ? WHERE id = ?",
            (value, iso_utc(), device_id),
        )

    def set_users_last_read(self, device_id: int, when: str | None = None) -> None:
        self.db.execute(
            "UPDATE devices SET users_last_read_at = ?, updated_at = ? WHERE id = ?",
            (when or iso_utc(), iso_utc(), device_id),
        )

    def count_events_for(self, device_id: int) -> int:
        row = self.db.query_one(
            "SELECT COUNT(*) AS c FROM attendance_events WHERE device_id = ?", (device_id,)
        )
        return int(row["c"])

    def remove(self, device_id: int) -> None:
        """Hard delete — refused when attendance history exists (safety)."""
        if self.count_events_for(device_id):
            raise ValueError(
                "device has collected attendance history and cannot be removed; "
                "disable it instead (history is never deleted automatically)"
            )
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM devices WHERE id = ?", (device_id,))

    def counts(self) -> dict:
        rows = self.db.query("SELECT status, COUNT(*) AS c FROM devices GROUP BY status")
        out = {"registered": 0, "pending": 0, "disabled": 0}
        for r in rows:
            out[r["status"]] = r["c"]
        out["total"] = sum(out.values())
        return out


# --------------------------------------------------------------------------
class ObservationRepository:
    def __init__(self, db: Database):
        self.db = db

    def add(self, *, ip_address: str, port: int | None, evidence: str,
            mac_address: str | None = None, model: str | None = None,
            serial_number: str | None = None, device_name: str | None = None,
            matched_device_id: int | None = None, error: str | None = None) -> int:
        return self.db.execute(
            """INSERT INTO discovery_observations
               (ip_address, port, mac_address, evidence, model, serial_number,
                device_name, matched_device_id, error, observed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (ip_address, port, mac_address, evidence, model, serial_number,
             device_name, matched_device_id, error, iso_utc()),
        )

    def recent(self, limit: int = 50) -> list[dict]:
        rows = self.db.query(
            "SELECT * FROM discovery_observations ORDER BY observed_at DESC, id DESC LIMIT ?",
            (limit,),
        )
        return [_row_to_dict(r) for r in rows]


# --------------------------------------------------------------------------
class EventRepository:
    REVIEW_AFTER_ATTEMPTS = 10

    def __init__(self, db: Database):
        self.db = db

    def insert_new(self, events: list[dict]) -> list[int]:
        """Insert events, silently skipping exact duplicates (dedup_hash UNIQUE).

        Returns the list of *newly inserted* row ids — only those must be
        scheduled for upload."""
        if not events:
            return []
        new_ids: list[int] = []
        now = iso_utc()
        with self.db.transaction() as conn:
            for ev in events:
                cur = conn.execute(
                    """INSERT OR IGNORE INTO attendance_events
                       (dedup_hash, device_id, device_serial, device_user_id,
                        device_record_uid, event_timestamp, event_timestamp_tz,
                        punch, status, verify_mode, raw_json, ems_student_id,
                        mapping_status, sync_status, retry_count, next_retry_at,
                        first_seen_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, 'pending', 0, NULL, ?)""",
                    (
                        ev["dedup_hash"], ev["device_id"], ev["device_serial"],
                        ev["device_user_id"], ev.get("device_record_uid"),
                        ev["event_timestamp"], ev["event_timestamp_tz"],
                        ev.get("punch"), ev.get("status"), ev.get("verify_mode"),
                        ev.get("raw_json"), ev.get("ems_student_id"),
                        ev.get("mapping_status", "unmapped"), now,
                    ),
                )
                if cur.rowcount:  # 1 = inserted, 0 = duplicate ignored
                    new_ids.append(cur.lastrowid)
        return new_ids

    def due_for_sync(self, now_iso: str, limit: int) -> list[dict]:
        rows = self.db.query(
            """SELECT * FROM attendance_events
               WHERE sync_status IN ('pending','failed')
                 AND (next_retry_at IS NULL OR next_retry_at <= ?)
               ORDER BY event_timestamp ASC, id ASC
               LIMIT ?""",
            (now_iso, limit),
        )
        return [_row_to_dict(r) for r in rows]

    def by_ids(self, ids: Iterable[int]) -> list[dict]:
        ids = list(ids)
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        rows = self.db.query(f"SELECT * FROM attendance_events WHERE id IN ({marks})", ids)
        return [_row_to_dict(r) for r in rows]

    def mark_synced(self, ids: Iterable[int], remote_ids: dict[int, str] | None = None) -> None:
        ids = list(ids)
        if not ids:
            return
        now = iso_utc()
        remote_ids = remote_ids or {}
        with self.db.transaction() as conn:
            for eid in ids:
                conn.execute(
                    """UPDATE attendance_events
                       SET sync_status = 'synced', synced_at = ?, last_error = NULL,
                           next_retry_at = NULL, remote_id = COALESCE(?, remote_id), retry_count = 0
                       WHERE id = ?""",
                    (now, remote_ids.get(eid), eid),
                )

    def mark_attempt_failed(self, ids: Iterable[int], error: str, next_retry_at: str) -> None:
        ids = list(ids)
        if not ids:
            return
        with self.db.transaction() as conn:
            for eid in ids:
                conn.execute(
                    """UPDATE attendance_events
                       SET sync_status = CASE WHEN retry_count + 1 >= ? THEN 'review' ELSE 'failed' END,
                           retry_count = retry_count + 1, last_error = ?, next_retry_at = ?
                       WHERE id = ? AND sync_status != 'synced'""",
                    (self.REVIEW_AFTER_ATTEMPTS, error[:500], next_retry_at, eid),
                )

    def mark_rejected(self, dedup_hash: str, reason: str) -> None:
        """Server rejected the record (validation) — never retried automatically."""
        self.db.execute(
            """UPDATE attendance_events
               SET sync_status = 'rejected', last_error = ?, next_retry_at = NULL
               WHERE dedup_hash = ?""",
            (reason[:500], dedup_hash),
        )

    def mark_review(self, ids: Iterable[int], reason: str) -> None:
        ids = list(ids)
        with self.db.transaction() as conn:
            for eid in ids:
                conn.execute(
                    "UPDATE attendance_events SET sync_status='review', last_error=? WHERE id=?",
                    (reason[:500], eid),
                )

    def retry_now(self, ids: Iterable[int]) -> int:
        """Admin action: make failed/review/rejected events due immediately."""
        ids = list(ids)
        if not ids:
            return 0
        marks = ",".join("?" * len(ids))
        with self.db.transaction() as conn:
            cur = conn.execute(
                f"""UPDATE attendance_events
                    SET sync_status = 'pending', next_retry_at = NULL, last_error = NULL
                    WHERE id IN ({marks}) AND sync_status != 'synced'""",
                ids,
            )
            return cur.rowcount

    def re_resolve_mappings(self, device_serial: str, device_user_id: str,
                            ems_student_id: str | None, mapping_status: str) -> int:
        """After a mapping change, re-tag still-unsent events (history preserved)."""
        with self.db.transaction() as conn:
            cur = conn.execute(
                """UPDATE attendance_events
                   SET ems_student_id = ?, mapping_status = ?
                   WHERE device_serial = ? AND device_user_id = ? AND sync_status != 'synced'""",
                (ems_student_id, mapping_status, device_serial, device_user_id),
            )
            return cur.rowcount

    def counts(self) -> dict:
        rows = self.db.query(
            "SELECT sync_status, COUNT(*) AS c FROM attendance_events GROUP BY sync_status"
        )
        out = {k: 0 for k in ("pending", "synced", "failed", "review", "rejected")}
        for r in rows:
            out[r["sync_status"]] = r["c"]
        out["total"] = sum(out.values())
        return out

    def last_synced_at(self) -> str | None:
        row = self.db.query_one("SELECT MAX(synced_at) AS t FROM attendance_events")
        return row["t"] if row else None

    def list_recent(self, limit: int = 100, offset: int = 0, status: str | None = None,
                    search: str | None = None) -> tuple[list[dict], int]:
        where, params = [], []
        if status:
            where.append("ev.sync_status = ?")
            params.append(status)
        if search:
            where.append("(ev.device_user_id LIKE ? OR ev.ems_student_id LIKE ? OR ev.device_serial LIKE ?)")
            params += [f"%{search}%"] * 3
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        total = self.db.query_one(
            f"SELECT COUNT(*) AS c FROM attendance_events ev {clause}", params
        )["c"]
        rows = self.db.query(
            f"""SELECT ev.*, d.label AS device_label, d.model AS device_model,
                       sc.name AS student_name, sc.class_name AS student_class,
                       sc.section AS student_section
                FROM attendance_events ev
                LEFT JOIN devices d ON d.id = ev.device_id
                LEFT JOIN students_cache sc ON sc.ems_student_id = ev.ems_student_id
                {clause}
                ORDER BY ev.event_timestamp DESC, ev.id DESC
                LIMIT ? OFFSET ?""",
            (*params, limit, offset),
        )
        return [_row_to_dict(r) for r in rows], int(total)

    def unmapped_device_users(self) -> list[dict]:
        rows = self.db.query(
            """SELECT ev.device_serial, ev.device_user_id, COUNT(*) AS events,
                      MIN(ev.event_timestamp) AS first_ts, MAX(ev.event_timestamp) AS last_ts
               FROM attendance_events ev
               LEFT JOIN device_user_mappings m
                 ON m.device_serial = ev.device_serial
                AND m.device_user_id = ev.device_user_id AND m.active = 1
               WHERE m.id IS NULL
               GROUP BY ev.device_serial, ev.device_user_id
               ORDER BY last_ts DESC"""
        )
        return [_row_to_dict(r) for r in rows]


# --------------------------------------------------------------------------
class StudentRepository:
    """The EMS *people* directory — students **and** teachers **and** staff.

    The table is still called ``students_cache`` (renaming a shipped table is
    riskier than it is worth); ``person_type`` carries the distinction and
    every row from every EMS endpoint lands in the same cache, because a
    fingerprint punch can belong to any of them.
    """

    def __init__(self, db: Database):
        self.db = db

    def upsert_many(self, students: list[dict]) -> tuple[int, int]:
        """Insert/update people. Returns (inserted, updated).

        ``person_type`` defaults to 'student' so an EMS that only exposes the
        classic ``/students`` endpoint keeps working unchanged.
        """
        if not students:
            return 0, 0
        now = iso_utc()
        ins = upd = 0
        with self.db.transaction() as conn:
            for s in students:
                raw = json.dumps(s, ensure_ascii=False, default=str)[:8000]
                person_type = (s.get("person_type") or "student").strip().lower()
                if person_type not in PERSON_TYPES:
                    person_type = "other"
                existing = conn.execute(
                    "SELECT id FROM students_cache WHERE ems_student_id = ?",
                    (str(s["ems_student_id"]),),
                ).fetchone()
                if existing:
                    conn.execute(
                        """UPDATE students_cache SET admission_no=?, roll_no=?, name=?, class_name=?,
                               section=?, active=?, person_type=?, employee_no=?, designation=?,
                               department=?, raw_json=?, fetched_at=?, updated_at=?
                           WHERE ems_student_id=?""",
                        (
                            s.get("admission_no"), s.get("roll_no"), s.get("name", ""),
                            s.get("class_name"), s.get("section"),
                            1 if s.get("active", True) else 0, person_type,
                            s.get("employee_no"), s.get("designation"), s.get("department"),
                            raw, now, now,
                            str(s["ems_student_id"]),
                        ),
                    )
                    upd += 1
                else:
                    conn.execute(
                        """INSERT INTO students_cache
                           (ems_student_id, admission_no, roll_no, name, class_name,
                            section, active, person_type, employee_no, designation,
                            department, raw_json, fetched_at, updated_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            str(s["ems_student_id"]), s.get("admission_no"), s.get("roll_no"),
                            s.get("name", ""), s.get("class_name"), s.get("section"),
                            1 if s.get("active", True) else 0, person_type,
                            s.get("employee_no"), s.get("designation"), s.get("department"),
                            raw, now, now,
                        ),
                    )
                    ins += 1
        return ins, upd

    def count(self, active_only: bool = False, person_type: str | None = None) -> int:
        where, params = [], []
        if active_only:
            where.append("active = 1")
        if person_type:
            where.append("person_type = ?")
            params.append(person_type)
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        row = self.db.query_one(f"SELECT COUNT(*) AS c FROM students_cache {clause}", params)
        return int(row["c"])

    def counts_by_type(self) -> dict:
        rows = self.db.query(
            "SELECT person_type, COUNT(*) AS c FROM students_cache GROUP BY person_type"
        )
        out = {t: 0 for t in PERSON_TYPES}
        out["all"] = 0
        for r in rows:
            key = r["person_type"] or "other"
            out[key] = out.get(key, 0) + int(r["c"])
            out["all"] += int(r["c"])
        return out

    def get(self, ems_student_id: str) -> dict | None:
        row = self.db.query_one(
            "SELECT * FROM students_cache WHERE ems_student_id = ?", (str(ems_student_id),)
        )
        return _row_to_dict(row) if row else None

    def list(self, *, search: str | None = None, class_name: str | None = None,
             section: str | None = None, active: bool | None = None,
             person_type: str | None = None,
             page: int = 1, page_size: int = 50,
             sort: str = "name", desc: bool = False) -> tuple[list[dict], int]:
        where, params = [], []
        if search:
            where.append(
                "(name LIKE ? OR admission_no LIKE ? OR roll_no LIKE ? OR ems_student_id LIKE ? "
                "OR employee_no LIKE ? OR designation LIKE ?)"
            )
            params += [f"%{search}%"] * 6
        if class_name:
            where.append("class_name = ?")
            params.append(class_name)
        if section:
            where.append("section = ?")
            params.append(section)
        if person_type:
            where.append("person_type = ?")
            params.append(person_type)
        if active is not None:
            where.append("active = ?")
            params.append(1 if active else 0)
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        sortable = {"name", "class_name", "section", "admission_no", "roll_no",
                    "updated_at", "person_type"}
        order = sort if sort in sortable else "name"
        direction = "DESC" if desc else "ASC"
        total = self.db.query_one(f"SELECT COUNT(*) AS c FROM students_cache {clause}", params)["c"]
        rows = self.db.query(
            f"""SELECT sc.*,
                       (SELECT GROUP_CONCAT(m.device_serial || ':' || m.device_user_id, ', ')
                          FROM device_user_mappings m
                         WHERE m.ems_student_id = sc.ems_student_id AND m.active = 1) AS device_users
                FROM students_cache sc {clause}
                ORDER BY {order} COLLATE NOCASE {direction}
                LIMIT ? OFFSET ?""",
            (*params, page_size, max(0, (page - 1) * page_size)),
        )
        return [_row_to_dict(r) for r in rows], int(total)

    def classes(self) -> list[dict]:
        rows = self.db.query(
            """SELECT class_name, GROUP_CONCAT(DISTINCT section) AS sections
               FROM students_cache WHERE class_name IS NOT NULL AND class_name != ''
               GROUP BY class_name ORDER BY class_name"""
        )
        return [
            {"class": r["class_name"],
             "sections": sorted(s for s in (r["sections"] or "").split(",") if s)}
            for r in rows
        ]


# --------------------------------------------------------------------------
class DeviceUserRepository:
    """Read-only local mirror of the user table enrolled on each terminal.

    The bridge pulls this over the ZK protocol (``get_users``) and never
    writes it back to the device. It exists so the dashboard can show
    "who does this K40 actually know about", which is the only way an
    administrator can bind a PIN to an EMS student/teacher/staff record —
    without it, punches arrive with a bare PIN and the EMS cannot attribute
    them to anybody.
    """

    def __init__(self, db: Database):
        self.db = db

    def replace_for(self, device_serial: str, users: list[dict]) -> dict:
        """Replace the cached user list for one terminal. Returns a stats dict.

        Users present on the device but gone from the cache are *removed* —
        the device is the source of truth. Punches already collected are never
        touched.
        """
        now = iso_utc()
        wanted: dict[str, dict] = {}
        for u in users:
            user_id = str(u.get("user_id") or "").strip()
            if user_id:
                wanted[user_id] = u

        inserted = updated = removed = 0
        with self.db.transaction() as conn:
            existing = {
                r["user_id"]
                for r in conn.execute(
                    "SELECT user_id FROM device_users_cache WHERE device_serial = ?",
                    (device_serial,),
                ).fetchall()
            }
            stale = existing - set(wanted)
            if stale:
                marks = ",".join("?" * len(stale))
                cur = conn.execute(
                    f"""DELETE FROM device_users_cache
                        WHERE device_serial = ? AND user_id IN ({marks})""",
                    (device_serial, *stale),
                )
                removed = int(cur.rowcount or 0)
            for user_id, u in wanted.items():
                cur = conn.execute(
                    """UPDATE device_users_cache
                       SET name = COALESCE(?, name), privilege = COALESCE(?, privilege),
                           card = COALESCE(?, card), last_seen_at = ?, updated_at = ?
                       WHERE device_serial = ? AND user_id = ?""",
                    (u.get("name"), u.get("privilege"), u.get("card"), now, now,
                     device_serial, user_id),
                )
                if cur.rowcount:
                    updated += 1
                    continue
                conn.execute(
                    """INSERT INTO device_users_cache
                       (device_serial, user_id, name, privilege, card,
                        first_seen_at, last_seen_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (device_serial, user_id, u.get("name"), u.get("privilege"),
                     u.get("card"), now, now, now),
                )
                inserted += 1
        return {
            "serial": device_serial,
            "on_device": len(wanted),
            "inserted": inserted,
            "updated": updated,
            "removed": removed,
            "cached": self.count(device_serial),
        }

    def get(self, device_serial: str, user_id: str) -> dict | None:
        row = self.db.query_one(
            "SELECT * FROM device_users_cache WHERE device_serial = ? AND user_id = ?",
            (device_serial, str(user_id)),
        )
        return _row_to_dict(row) if row else None

    def count(self, device_serial: str | None = None) -> int:
        if device_serial:
            row = self.db.query_one(
                "SELECT COUNT(*) AS c FROM device_users_cache WHERE device_serial = ?",
                (device_serial,),
            )
        else:
            row = self.db.query_one("SELECT COUNT(*) AS c FROM device_users_cache")
        return int(row["c"])

    def unmapped_count(self) -> int:
        row = self.db.query_one(
            """SELECT COUNT(*) AS c FROM device_users_cache du
               LEFT JOIN device_user_mappings m
                 ON m.device_serial = du.device_serial
                AND m.device_user_id = du.user_id AND m.active = 1
               WHERE m.id IS NULL"""
        )
        return int(row["c"])

    def list(self, *, device_serial: str | None = None, search: str | None = None,
             unmapped_only: bool = False, page: int = 1,
             page_size: int = 200) -> tuple[list[dict], int]:
        where, params = [], []
        if device_serial:
            where.append("du.device_serial = ?")
            params.append(device_serial)
        if search:
            where.append(
                "(du.user_id LIKE ? OR du.name LIKE ? OR du.device_serial LIKE ? "
                "OR sc.name LIKE ?)"
            )
            params += [f"%{search}%"] * 4
        if unmapped_only:
            where.append("m.id IS NULL")
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        total = self.db.query_one(
            f"""SELECT COUNT(*) AS c FROM device_users_cache du
                LEFT JOIN device_user_mappings m
                  ON m.device_serial = du.device_serial
                 AND m.device_user_id = du.user_id AND m.active = 1
                LEFT JOIN students_cache sc ON sc.ems_student_id = m.ems_student_id
                {clause}""",
            params,
        )["c"]
        rows = self.db.query(
            f"""SELECT du.*, d.label AS device_label,
                       m.id AS mapping_id, m.ems_student_id AS ems_person_id,
                       sc.name AS person_name, sc.person_type, sc.class_name, sc.section,
                       sc.designation, sc.department, sc.active AS person_active,
                       (SELECT COUNT(*) FROM attendance_events ev
                         WHERE ev.device_serial = du.device_serial
                           AND ev.device_user_id = du.user_id) AS punch_count,
                       (SELECT MAX(ev.event_timestamp) FROM attendance_events ev
                         WHERE ev.device_serial = du.device_serial
                           AND ev.device_user_id = du.user_id) AS last_punch_at
                FROM device_users_cache du
                LEFT JOIN devices d ON d.serial_number = du.device_serial
                LEFT JOIN device_user_mappings m
                  ON m.device_serial = du.device_serial
                 AND m.device_user_id = du.user_id AND m.active = 1
                LEFT JOIN students_cache sc ON sc.ems_student_id = m.ems_student_id
                {clause}
                ORDER BY CAST(du.user_id AS INTEGER), du.user_id
                LIMIT ? OFFSET ?""",
            (*params, page_size, max(0, (page - 1) * page_size)),
        )
        return [_row_to_dict(r) for r in rows], int(total)

    def users_for_ems(self, device_serial: str) -> list[dict]:
        """Payload for the EMS USER table (passwords are never included)."""
        rows = self.db.query(
            """SELECT user_id, name, privilege, card FROM device_users_cache
               WHERE device_serial = ? ORDER BY CAST(user_id AS INTEGER), user_id""",
            (device_serial,),
        )
        return [_row_to_dict(r) for r in rows]


# --------------------------------------------------------------------------
class MappingRepository:
    def __init__(self, db: Database):
        self.db = db

    def get(self, device_serial: str, device_user_id: str) -> dict | None:
        row = self.db.query_one(
            """SELECT * FROM device_user_mappings
               WHERE device_serial = ? AND device_user_id = ? AND active = 1""",
            (device_serial, str(device_user_id)),
        )
        return _row_to_dict(row) if row else None

    def list(self, device_serial: str | None = None) -> list[dict]:
        if device_serial:
            rows = self.db.query(
                """SELECT m.*, sc.name AS student_name, sc.class_name, sc.section, sc.active AS student_active
                   FROM device_user_mappings m
                   LEFT JOIN students_cache sc ON sc.ems_student_id = m.ems_student_id
                   WHERE m.device_serial = ? AND m.active = 1
                   ORDER BY CAST(m.device_user_id AS INTEGER), m.device_user_id""",
                (device_serial,),
            )
        else:
            rows = self.db.query(
                """SELECT m.*, sc.name AS student_name, sc.class_name, sc.section, sc.active AS student_active
                   FROM device_user_mappings m
                   LEFT JOIN students_cache sc ON sc.ems_student_id = m.ems_student_id
                   WHERE m.active = 1
                   ORDER BY sc.class_name, sc.section, sc.name"""
            )
        return [_row_to_dict(r) for r in rows]

    def upsert(self, *, device_serial: str, device_user_id: str, ems_student_id: str,
               mapped_by: str, source: str = "manual") -> int:
        now = iso_utc()
        with self.db.transaction() as conn:
            existing = conn.execute(
                "SELECT id FROM device_user_mappings WHERE device_serial = ? AND device_user_id = ?",
                (device_serial, str(device_user_id)),
            ).fetchone()
            if existing:
                conn.execute(
                    """UPDATE device_user_mappings SET ems_student_id=?, mapped_by=?, source=?,
                           active=1, updated_at=? WHERE id=?""",
                    (str(ems_student_id), mapped_by, source, now, existing["id"]),
                )
                return existing["id"]
            cur = conn.execute(
                """INSERT INTO device_user_mappings
                   (device_serial, device_user_id, ems_student_id, mapped_by, source, active, created_at, updated_at)
                   VALUES (?,?,?,?,?,1,?,?)""",
                (device_serial, str(device_user_id), str(ems_student_id), mapped_by, source, now, now),
            )
            return cur.lastrowid

    def delete(self, mapping_id: int) -> None:
        self.db.execute(
            "UPDATE device_user_mappings SET active = 0, updated_at = ? WHERE id = ?",
            (iso_utc(), mapping_id),
        )

    def conflicts(self) -> list[dict]:
        """One student mapped to multiple device user accounts — flagged for review."""
        rows = self.db.query(
            """SELECT ems_student_id, COUNT(*) AS n,
                      GROUP_CONCAT(device_serial || ':' || device_user_id, ', ') AS bindings
               FROM device_user_mappings WHERE active = 1
               GROUP BY ems_student_id HAVING COUNT(*) > 1"""
        )
        return [_row_to_dict(r) for r in rows]

    def mappings_for_unknown_students(self) -> list[dict]:
        """Mappings pointing at students not present (or inactive) in the roster cache."""
        rows = self.db.query(
            """SELECT m.*, sc.active AS student_active, sc.name AS student_name
               FROM device_user_mappings m
               LEFT JOIN students_cache sc ON sc.ems_student_id = m.ems_student_id
               WHERE m.active = 1 AND (sc.id IS NULL OR sc.active = 0)
               ORDER BY m.updated_at DESC"""
        )
        return [_row_to_dict(r) for r in rows]

    def count(self) -> int:
        return int(self.db.query_one("SELECT COUNT(*) AS c FROM device_user_mappings WHERE active = 1")["c"])


# --------------------------------------------------------------------------
class SettingsRepository:
    def __init__(self, db: Database):
        self.db = db

    def get(self, key: str, default: str | None = None) -> str | None:
        row = self.db.query_one("SELECT value FROM application_settings WHERE key = ?", (key,))
        return row["value"] if row else default

    def set(self, key: str, value: str, is_secret: bool = False) -> None:
        now = iso_utc()
        self.db.execute(
            """INSERT INTO application_settings (key, value, is_secret, updated_at)
               VALUES (?,?,?,?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                   is_secret=excluded.is_secret, updated_at=excluded.updated_at""",
            (key, value, 1 if is_secret else 0, now),
        )

    def delete(self, key: str) -> None:
        self.db.execute("DELETE FROM application_settings WHERE key = ?", (key,))

    def all_public(self) -> dict:
        rows = self.db.query("SELECT key, value, is_secret FROM application_settings")
        return {r["key"]: ("***" if r["is_secret"] else r["value"]) for r in rows}


# --------------------------------------------------------------------------
class AuditRepository:
    def __init__(self, db: Database):
        self.db = db

    def log(self, *, actor: str, action: str, entity: str | None = None,
            entity_id: str | None = None, detail: str | None = None) -> int:
        if detail and len(detail) > 2000:
            detail = detail[:2000] + "…"
        return self.db.execute(
            "INSERT INTO audit_logs (actor, action, entity, entity_id, detail, created_at) VALUES (?,?,?,?,?,?)",
            (actor, action, entity, entity_id, detail, iso_utc()),
        )

    def list(self, limit: int = 100, offset: int = 0, search: str | None = None,
             action: str | None = None) -> tuple[list[dict], int]:
        where, params = [], []
        if search:
            where.append("(detail LIKE ? OR entity_id LIKE ? OR actor LIKE ? OR action LIKE ?)")
            params += [f"%{search}%"] * 4
        if action:
            where.append("action = ?")
            params.append(action)
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        total = self.db.query_one(f"SELECT COUNT(*) AS c FROM audit_logs {clause}", params)["c"]
        rows = self.db.query(
            f"SELECT * FROM audit_logs {clause} ORDER BY id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )
        return [_row_to_dict(r) for r in rows], int(total)

    def actions(self) -> list[str]:
        rows = self.db.query("SELECT DISTINCT action FROM audit_logs ORDER BY action")
        return [r["action"] for r in rows]


# --------------------------------------------------------------------------
class QueueRepository:
    def __init__(self, db: Database):
        self.db = db

    def enqueue(self, task_type: str, payload: dict | None = None,
                run_after: str | None = None, max_attempts: int = 25) -> int:
        now = iso_utc()
        return self.db.execute(
            """INSERT INTO sync_queue (task_type, payload_json, status, run_after, attempts, max_attempts, created_at, updated_at)
               VALUES (?,?, 'pending', ?, 0, ?, ?, ?)""",
            (task_type, json.dumps(payload or {}), run_after or now, max_attempts, now, now),
        )

    def enqueue_once(self, task_type: str, payload: dict | None = None) -> int | None:
        """Enqueue unless an identical pending/running task already exists."""
        row = self.db.query_one(
            "SELECT id FROM sync_queue WHERE task_type = ? AND status IN ('pending','running') LIMIT 1",
            (task_type,),
        )
        return None if row else self.enqueue(task_type, payload)

    def due(self, now_iso: str, limit: int = 5) -> list[dict]:
        rows = self.db.query(
            """SELECT * FROM sync_queue WHERE status = 'pending' AND run_after <= ?
               ORDER BY run_after ASC LIMIT ?""",
            (now_iso, limit),
        )
        return [_row_to_dict(r) for r in rows]

    def complete(self, task_id: int) -> None:
        self.db.execute(
            "UPDATE sync_queue SET status='done', updated_at=? WHERE id=?", (iso_utc(), task_id)
        )

    def fail(self, task_id: int, error: str, run_after: str) -> None:
        with self.db.transaction() as conn:
            row = conn.execute("SELECT attempts, max_attempts FROM sync_queue WHERE id=?", (task_id,)).fetchone()
            attempts = (row["attempts"] + 1) if row else 1
            terminal = row and attempts >= row["max_attempts"]
            conn.execute(
                """UPDATE sync_queue SET status=?, attempts=?, last_error=?, run_after=?, updated_at=?
                   WHERE id=?""",
                ("failed" if terminal else "pending", attempts, error[:500], run_after, iso_utc(), task_id),
            )

    def pending_count(self) -> int:
        return int(self.db.query_one("SELECT COUNT(*) AS c FROM sync_queue WHERE status='pending'")["c"])

    def record_attempt(self, *, attempt_type: str, event_count: int, result: str,
                       detail: str | None = None, error: str | None = None,
                       duration_ms: int | None = None) -> int:
        return self.db.execute(
            """INSERT INTO sync_attempts
               (attempt_type, event_count, result, detail, error, duration_ms, attempted_at)
               VALUES (?,?,?,?,?,?,?)""",
            (attempt_type, event_count, result, detail, error, duration_ms, iso_utc()),
        )

    def recent_attempts(self, limit: int = 20) -> list[dict]:
        rows = self.db.query(
            "SELECT * FROM sync_attempts ORDER BY id DESC LIMIT ?", (limit,)
        )
        return [_row_to_dict(r) for r in rows]


# --------------------------------------------------------------------------
def open_migrated_database(path) -> tuple[Database, dict]:
    """Open + migrate a database file; quarantine and recreate if corrupt.

    Returns (db, repos) where repos bundles every repository.
    """
    from .migrations import migrate

    db = Database(path)
    try:
        migrate(db)
    except sqlite3.DatabaseError as exc:
        log.critical("Database at %s is corrupted: %s", path, exc)
        db.close_all()
        Database.quarantine_corrupt(path)
        db = Database(path)
        migrate(db)
    repos = {
        "devices": DeviceRepository(db),
        "events": EventRepository(db),
        "students": StudentRepository(db),
        "people": StudentRepository(db),          # alias: students + teachers + staff
        "device_users": DeviceUserRepository(db),
        "mappings": MappingRepository(db),
        "settings": SettingsRepository(db),
        "audit": AuditRepository(db),
        "queue": QueueRepository(db),
        "observations": ObservationRepository(db),
        "db": db,
    }
    return db, repos
