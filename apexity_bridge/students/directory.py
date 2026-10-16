"""People directory (students + teachers + staff) fetched from the EMS.

Previously this only ever downloaded "students", which is why a school could
see punches arriving but no idea who they belonged to: teachers and
non-teaching staff punch the same terminals and were simply absent from the
bridge. The EMS directory is now downloaded as *people*, each carrying a
``person_type``.

Failure modes stay honest:
* EMS unavailable → raise EmsUnavailableError (caller schedules retry).
* Protocol missing (iClock without the REST roster side-channel) →
  EmsProtocolError with a documented message; the failure is cached for the
  dashboard and no people are ever invented.
"""
from __future__ import annotations

import logging

from ..ems.base import EmsProtocolError, EmsUnavailableError, EmsAuthError

log = logging.getLogger("apexity_bridge.students.directory")

MAX_PAGES = 100


def refresh_roster(client, repos, queue_repo=None) -> dict:
    """Alias kept for existing call sites — see :func:`refresh_people`."""
    return refresh_people(client, repos, queue_repo=queue_repo)


def refresh_people(client, repos, queue_repo=None) -> dict:
    """Pull the full directory, handling pagination. Returns a stats dict."""
    stats = {
        "fetched": 0, "inserted": 0, "updated": 0, "ok": False, "error": None,
        "by_type": {},
    }
    cursor = None
    pages = 0
    inserted = updated = 0
    by_type: dict[str, int] = {}
    try:
        while True:
            fetch = getattr(client, "fetch_people", None) or client.fetch_students
            page = fetch(cursor)
            rows = []
            for person in page.students:
                by_type[person.person_type] = by_type.get(person.person_type, 0) + 1
                rows.append(person.as_cache_row())
            ins, upd = repos["students"].upsert_many(rows)
            inserted += ins
            updated += upd
            stats["fetched"] += len(page.students)
            pages += 1
            if page.complete or not page.next_cursor or pages > MAX_PAGES:
                break
            cursor = page.next_cursor
    except EmsProtocolError as exc:
        stats["error"] = str(exc)
        repos["settings"].set("roster_last_error", str(exc)[:500])
        raise
    except (EmsUnavailableError, EmsAuthError):
        raise
    stats.update(ok=True, inserted=inserted, updated=updated, by_type=by_type)
    repos["settings"].delete("roster_last_error")
    from ..timeutils import iso_utc
    repos["settings"].set("roster_last_sync", iso_utc())
    log.info(
        "people directory refreshed: %d people (%d new, %d updated) %s",
        stats["fetched"], inserted, updated,
        " ".join(f"{k}={v}" for k, v in sorted(by_type.items())),
    )
    return stats
