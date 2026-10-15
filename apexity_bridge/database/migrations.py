"""Schema definition + forward-only migrations.

Migration 1 is the full initial schema. Future schema changes are appended
as (version, sql_statements) pairs and applied transactions-first.

Design notes:
* ``devices`` — the *identity* of a terminal is its serial number (strongest
  available signal), never its IP address. IP is mutable state.
* ``attendance_events.dedup_hash`` — stable idempotency key, UNIQUE.
* ``sync_queue`` — generic durable task queue (roster refresh, registration
  retries). Attendance pushing uses ``attendance_events`` rows directly so an
  event can never be lost between "queued" and "sent" states.
"""
from __future__ import annotations

import logging

log = logging.getLogger("apexity_bridge.database")

SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS devices (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    serial_number    TEXT,                        -- strongest identity signal; NULL = not yet identified
    mac_address    TEXT,                          -- may be unavailable across routed networks; never sole identity
    model            TEXT,
    firmware_version TEXT,
    platform         TEXT,
    ip_address       TEXT,                        -- mutable; last verified address only
    port             INTEGER NOT NULL DEFAULT 4370,
    comm_key         INTEGER NOT NULL DEFAULT 0,
    status           TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','registered','disabled')),
    label            TEXT,
    notes            TEXT,
    first_seen_at    TEXT NOT NULL,
    last_seen_at     TEXT,
    ip_updated_at    TEXT,                        -- last automatic IP correction
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_devices_serial
    ON devices(serial_number) WHERE serial_number IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_devices_status ON devices(status);

CREATE TABLE IF NOT EXISTS discovery_observations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ip_address    TEXT NOT NULL,
    port          INTEGER,
    mac_address   TEXT,
    evidence      TEXT NOT NULL,                  -- tcp_open | protocol_identified
    model         TEXT,
    serial_number TEXT,
    device_name   TEXT,
    matched_device_id INTEGER REFERENCES devices(id),
    error         TEXT,
    observed_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_observations_ip ON discovery_observations(ip_address, observed_at);

CREATE TABLE IF NOT EXISTS students_cache (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ems_student_id TEXT NOT NULL UNIQUE,          -- EMS-side identifier (as string)
    admission_no   TEXT,
    roll_no        TEXT,
    name           TEXT NOT NULL,
    class_name     TEXT,
    section        TEXT,
    active         INTEGER NOT NULL DEFAULT 1,
    raw_json       TEXT,                          -- untouched EMS payload for troubleshooting
    fetched_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_students_class ON students_cache(class_name, section);
CREATE INDEX IF NOT EXISTS idx_students_name ON students_cache(name);

CREATE TABLE IF NOT EXISTS device_user_mappings (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    device_serial  TEXT NOT NULL,
    device_user_id TEXT NOT NULL,
    ems_student_id TEXT NOT NULL,
    mapped_by      TEXT,
    source         TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('manual','csv')),
    active         INTEGER NOT NULL DEFAULT 1,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    UNIQUE(device_serial, device_user_id)
);
CREATE INDEX IF NOT EXISTS idx_mappings_student ON device_user_mappings(ems_student_id);

CREATE TABLE IF NOT EXISTS attendance_events (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    dedup_hash        TEXT NOT NULL UNIQUE,       -- stable idempotency key (sha256 hex)
    device_id         INTEGER NOT NULL REFERENCES devices(id),
    device_serial     TEXT NOT NULL,
    device_user_id    TEXT NOT NULL,
    device_record_uid INTEGER,                    -- native record id when firmware reports one
    event_timestamp   TEXT NOT NULL,              -- device-reported local time (verbatim)
    event_timestamp_tz TEXT NOT NULL,             -- e.g. Asia/Dhaka
    punch             INTEGER,                    -- device punch/check-type code
    status            INTEGER,                    -- device status/state code
    verify_mode       INTEGER,                    -- verification method code (finger/card/password)
    raw_json          TEXT,                       -- bounded raw protocol metadata
    ems_student_id    TEXT,                       -- resolved at collection time (NULL if unmapped)
    mapping_status    TEXT NOT NULL DEFAULT 'unmapped' CHECK (mapping_status IN ('resolved','unmapped','ambiguous','inactive_student')),
    sync_status       TEXT NOT NULL DEFAULT 'pending' CHECK (sync_status IN ('pending','synced','failed','review','rejected')),
    retry_count       INTEGER NOT NULL DEFAULT 0,
    next_retry_at     TEXT,
    last_error        TEXT,
    first_seen_at     TEXT NOT NULL,              -- when the bridge first saw this record
    synced_at         TEXT,
    remote_id         TEXT                        -- EMS record id after acceptance
);
CREATE INDEX IF NOT EXISTS idx_events_sync ON attendance_events(sync_status, next_retry_at);
CREATE INDEX IF NOT EXISTS idx_events_ts ON attendance_events(event_timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_events_first_seen ON attendance_events(first_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_devuser ON attendance_events(device_serial, device_user_id);

CREATE TABLE IF NOT EXISTS sync_queue (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type    TEXT NOT NULL CHECK (task_type IN ('fetch_students','register_device','push_attendance')),
    payload_json TEXT NOT NULL DEFAULT '{}',
    status       TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','done','failed')),
    run_after    TEXT NOT NULL,
    attempts     INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 25,
    last_error   TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_queue_due ON sync_queue(status, run_after);

CREATE TABLE IF NOT EXISTS sync_attempts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_type TEXT NOT NULL,
    event_count  INTEGER NOT NULL DEFAULT 0,
    result       TEXT NOT NULL,                   -- success | partial | failed
    detail       TEXT,
    error        TEXT,
    duration_ms  INTEGER,
    attempted_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attempts_time ON sync_attempts(attempted_at DESC);

CREATE TABLE IF NOT EXISTS application_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    is_secret  INTEGER NOT NULL DEFAULT 0,        -- secrets land in the OS credential store instead
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    actor      TEXT NOT NULL,                     -- dashboard admin name or 'system'
    action     TEXT NOT NULL,
    entity     TEXT,
    entity_id  TEXT,
    detail     TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_logs(created_at DESC);
"""

MIGRATIONS: list[tuple[int, str]] = [
    (1, SCHEMA_V1),
]
CURRENT_VERSION = MIGRATIONS[-1][0]


def migrate(db) -> None:
    """Apply all pending migrations (idempotent, transactional each)."""
    with db.transaction() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
    applied = {
        row["version"]
        for row in db.query("SELECT version FROM schema_migrations")
    }
    for version, sql in MIGRATIONS:
        if version in applied:
            continue
        log.info("Applying schema migration v%s", version)
        from ..timeutils import iso_utc

        with db.transaction() as conn:
            conn.executescript(sql)
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, iso_utc()),
            )
    latest = max(applied | {m[0] for m in MIGRATIONS} | {0})
    log.debug("Schema at version %s", latest)
