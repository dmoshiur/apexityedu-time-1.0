"""Student roster fetching from the EMS (with honest failure modes)."""
from __future__ import annotations

import logging

from ..ems.base import EmsProtocolError, EmsUnavailableError, EmsAuthError, StudentInfo

log = logging.getLogger("apexity_bridge.students.directory")


def refresh_roster(client, repos, queue_repo=None) -> dict:
    """Pull the full roster, handling pagination. Returns a stats dict.

    Failure modes:
    * EMS unavailable → raise EmsUnavailableError (caller schedules retry).
    * Protocol missing (classic iClock) → EmsProtocolError with a documented
      message; we cache the failure state for the dashboard and never invent
      students.
    """
    stats = {"fetched": 0, "inserted": 0, "updated": 0, "ok": False, "error": None}
    cursor = None
    pages = 0
    inserted = updated = 0
    try:
        while True:
            page = client.fetch_students(cursor)
            ins, upd = repos["students"].upsert_many(
                [
                    {
                        "ems_student_id": s.ems_student_id,
                        "name": s.name,
                        "admission_no": s.admission_no,
                        "roll_no": s.roll_no,
                        "class_name": s.class_name,
                        "section": s.section,
                        "active": s.active,
                        **({"raw": s.raw} if s.raw else {}),
                    }
                    for s in page.students
                ]
            )
            inserted += ins
            updated += upd
            stats["fetched"] += len(page.students)
            pages += 1
            if page.complete or not page.next_cursor or pages > 100:
                break
            cursor = page.next_cursor
    except EmsProtocolError as exc:
        stats["error"] = str(exc)
        repos["settings"].set("roster_last_error", str(exc)[:500])
        raise
    except (EmsUnavailableError, EmsAuthError):
        raise
    stats.update(ok=True, inserted=inserted, updated=updated)
    repos["settings"].delete("roster_last_error")
    from ..timeutils import iso_utc
    repos["settings"].set("roster_last_sync", iso_utc())
    log.info("roster refreshed: %d students (%d new, %d updated)",
             stats["fetched"], inserted, updated)
    return stats
