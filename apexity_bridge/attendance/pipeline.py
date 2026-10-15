"""Turn raw device records into durable, deduplicated bridge events.

Idempotency design (see docs/ARCHITECTURE.md):

* pyzk exposes no stable per-record id for the K40 — its ``uid`` field is the
  *user enrol slot*, not a record id. Therefore the stable idempotency key is::

      sha256( "v2" | serial | device_user_id | exact_timestamp | status | punch )

* Limitation (documented + tested): two physically distinct punches by the
  SAME user in the SAME second with the SAME codes collapse into one event.
  On a K40 a fingerprint scan cycle takes well over a second, so this cannot
  collide in practice; different users at identical timestamps are NOT
  affected (requirement: multiple records sharing a timestamp must survive).
* The device-reported timestamp is stored verbatim (with the configured
  timezone label) and never shifted.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta

from ..devices.base import RawAttendanceRecord
from ..timeutils import format_device_ts, utcnow

log = logging.getLogger("apexity_bridge.attendance.pipeline")

MIN_VALID_TS = datetime(2000, 1, 1)
MAX_FUTURE_SKEW = timedelta(hours=48)


def compute_dedup_hash(serial: str, user_id: str, ts_iso: str,
                       status: int | None, punch: int | None) -> str:
    canonical = "|".join(
        ["v2", serial.strip(), str(user_id).strip(), ts_iso,
         "" if status is None else str(int(status)),
         "" if punch is None else str(int(punch))]
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_record_timestamp(ts: datetime) -> str | None:
    """Return an error string if a device timestamp is untrustworthy."""
    if not isinstance(ts, datetime):
        return "device reported a missing/malformed timestamp"
    if ts < MIN_VALID_TS:
        return f"device timestamp {ts.isoformat()} predates year 2000 (device clock unset?)"
    if ts.replace(tzinfo=None) > (utcnow().replace(tzinfo=None) + MAX_FUTURE_SKEW):
        return f"device timestamp {ts.isoformat()} is >48h in the future (device clock wrong?)"
    return None


def build_event(record: RawAttendanceRecord, *, device: dict, timezone_label: str,
                mapping_lookup, now_utc: datetime | None = None) -> dict | None:
    """Map a raw record into the dict EventRepository.insert_new expects.

    ``mapping_lookup`` is a callable (serial, user_id) → mapping row | None.
    Returns None when the record is fundamentally unusable (no user id).
    """
    user_id = str(record.user_id).strip()
    if not user_id:
        log.warning("skipping record with empty user id from %s", device.get("serial_number"))
        return None

    serial = device["serial_number"]
    ts_iso = format_device_ts(record.timestamp)
    ts_error = validate_record_timestamp(record.timestamp)

    mapping = mapping_lookup(serial, user_id)
    ems_student_id = None
    mapping_status = "unmapped"
    if mapping:
        ems_student_id = str(mapping["ems_student_id"])
        mapping_status = "resolved"
        if mapping.get("student_active") == 0:
            mapping_status = "inactive_student"

    event = {
        "dedup_hash": compute_dedup_hash(
            serial, user_id, ts_iso, record.status, record.punch
        ),
        "device_id": device["id"],
        "device_serial": serial,
        "device_user_id": user_id,
        "device_record_uid": record.record_uid,   # diagnostics only — never identity
        "event_timestamp": ts_iso,
        "event_timestamp_tz": timezone_label,
        "punch": record.punch,
        "status": record.status,
        "verify_mode": record.punch,              # ZK protocol: 'punch' carries the verify method
        "raw_json": json.dumps(record.raw_dict(), default=str)[:2000],
        "ems_student_id": ems_student_id,
        "mapping_status": mapping_status,
    }
    if ts_error:
        event["quarantine_reason"] = ts_error
    return event
