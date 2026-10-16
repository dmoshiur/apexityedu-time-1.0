"""Manual, synchronous pushes to the EMS — "push it now and tell me what happened".

Why this module exists
----------------------
The periodic loop in ``sync/worker.py`` is deliberately quiet: it only runs
when the EMS is reachable, it skips unchanged tables and it never reports
anything to the operator. That is right for a background worker and useless
for the question that actually gets asked in a school:

    *"I created two users on the machine, the desktop app shows them, why does
    ApexityEdu still show nobody?"*

Every push an administrator can trigger by hand therefore lives here, and
every one of them returns **what the server actually answered** — accepted
count, refusal, unsupported endpoint — instead of a cheerful "queued".

Three things can be pushed:

``push_device_users``
    The user table (PIN + name, never templates/passwords) of one or all
    terminals, so ApexityEdu can resolve a bare PIN to a human. This is the
    iClock ``table=USER`` upload (or ``POST /devices/users`` over REST).

``push_people``
    The people directory (students **and** teachers **and** staff) from the
    local cache to the EMS. Optional on the server: an EMS that only
    implements ``GET /people`` answers 404 and the bridge says exactly that.

``create_person``
    One person, created/updated in the EMS straight from the desktop app —
    the manual button for "add a student / teacher / staff member".

Nothing here ever writes to a device: the bridge stays read-only towards the
ZKTeco terminals.
"""
from __future__ import annotations

import hashlib
import logging

from ..database.repositories import ems_serial_for
from ..timeutils import iso_utc
from .base import (
    EmsAuthError,
    EmsError,
    EmsUnavailableError,
)

log = logging.getLogger("apexity_bridge.ems.push")

# How many people are read from the local cache in one go.
PEOPLE_PAGE_SIZE = 1000


class PushError(Exception):
    """A push could not be attempted at all (no client, bad input)."""


# --------------------------------------------------------------------- utils --
def _client(ctx):
    client = getattr(ctx, "ems_client", None)
    if client is None:
        raise PushError(
            "no EMS client is configured — check EMS_BASE_URL and EMS_PROTOCOL "
            "(Settings → EMS base URL / Protocol)"
        )
    return client


def _devices(ctx, device_id: int | None) -> list[dict]:
    repos = ctx.repos
    if device_id:
        device = repos["devices"].get_by_id(int(device_id))
        if not device:
            raise PushError("device not found")
        return [device]
    return list(repos["devices"].list_active())


def _user_digest(users: list[dict]) -> str:
    return hashlib.sha256(
        "\n".join(f"{u.get('user_id')}|{u.get('name') or ''}|{u.get('privilege')}"
                  for u in users).encode("utf-8")
    ).hexdigest()


def person_row(row: dict) -> dict:
    """Normalise a cached person row into the documented write payload."""
    return {
        "id": str(row.get("ems_student_id") or row.get("id") or "").strip(),
        "name": str(row.get("name") or "").strip(),
        "person_type": str(row.get("person_type") or "student").strip().lower(),
        "admission_no": row.get("admission_no"),
        "roll_no": row.get("roll_no"),
        "class": row.get("class_name") or row.get("class"),
        "section": row.get("section"),
        "employee_no": row.get("employee_no"),
        "designation": row.get("designation"),
        "department": row.get("department"),
        "active": bool(row.get("active", True)),
    }


# ------------------------------------------------------------- device users --
def push_device_users(ctx, device_id: int | None = None, *, force: bool = True,
                      actor: str = "manual") -> dict:
    """Publish terminal user tables to the EMS **now** and report the result.

    ``force=False`` skips terminals whose table did not change since the
    last successful push (the periodic loop's behaviour); a manual push
    defaults to ``force=True`` because the operator asked for it.
    """
    client = _client(ctx)
    repos = ctx.repos
    devices = _devices(ctx, device_id)
    if not devices:
        return {
            "ok": False, "pushed": 0, "failed": 0, "total_users": 0,
            "devices": [],
            "error": "no registered device — register the K40 on the Devices page first",
        }

    results: list[dict] = []
    pushed = failed = total_users = 0
    for device in devices:
        serial = device.get("serial_number")
        target = ems_serial_for(device)
        users = repos["device_users"].users_for_ems(serial)
        entry = {
            "serial": serial,
            "ems_serial": target,
            "label": device.get("label") or "",
            "users": len(users),
            "accepted": 0,
            "state": "",
            "error": None,
            "detail": "",
        }
        if not users:
            entry["state"] = "no users read yet"
            entry["error"] = (
                "the bridge has no user table for this terminal yet — press "
                "“Read from device” first (the device is the source of truth)"
            )
            results.append(entry)
            continue

        digest = _user_digest(users)
        if not force and digest == (device.get("users_pushed_hash") or ""):
            entry["state"] = "unchanged"
            entry["detail"] = "identical to the table already published"
            results.append(entry)
            continue

        try:
            outcome = client.push_users(target, users)
        except EmsAuthError as exc:
            entry["state"] = "auth error"
            entry["error"] = str(exc)[:300]
            failed += 1
            repos["queue"].record_attempt(
                attempt_type="push_users", event_count=len(users),
                result="failed", error=f"auth: {exc}"[:300])
            results.append(entry)
            continue
        except (EmsUnavailableError, EmsError) as exc:
            entry["state"] = "failed"
            entry["error"] = str(exc)[:300]
            failed += 1
            repos["queue"].record_attempt(
                attempt_type="push_users", event_count=len(users),
                result="failed", error=str(exc)[:300])
            results.append(entry)
            continue

        if outcome.fatal:
            entry["state"] = "refused"
            entry["error"] = outcome.fatal[:300]
            failed += 1
            repos["queue"].record_attempt(
                attempt_type="push_users", event_count=len(users),
                result="failed", error=outcome.fatal[:300])
            results.append(entry)
            continue

        repos["devices"].set_users_pushed_hash(device["id"], digest)
        entry["state"] = "pushed"
        entry["accepted"] = outcome.accepted
        entry["detail"] = outcome.detail
        pushed += 1
        total_users += len(users)
        repos["queue"].record_attempt(
            attempt_type="push_users", event_count=len(users), result="success",
            detail=f"accepted={outcome.accepted}")
        repos["audit"].log(
            actor=actor, action="device_users.pushed", entity="device",
            entity_id=serial,
            detail=f"{len(users)} users published to the EMS as SN {target} "
                   f"({outcome.detail})"[:400],
        )
        results.append(entry)

    ok = failed == 0 and pushed > 0
    return {
        "ok": ok,
        "pushed": pushed,
        "failed": failed,
        "total_users": total_users,
        "devices": results,
        "error": None if ok else _summarise_devices(results),
        "pushed_at": iso_utc(),
    }


def _summarise_devices(results: list[dict]) -> str | None:
    problems = [r for r in results if r.get("error")]
    if not problems:
        return None
    return "; ".join(f"{r.get('serial')}: {r.get('error')}" for r in problems)[:400]


# ------------------------------------------------------------------- people --
def push_people(ctx, *, person_types: list[str] | None = None,
                ems_student_id: str | None = None) -> dict:
    """Push the cached people directory (students/teachers/staff) to the EMS."""
    client = _client(ctx)
    repos = ctx.repos

    if ems_student_id:
        row = repos["students"].get(str(ems_student_id).strip())
        if not row:
            raise PushError(f"no cached person with EMS id {ems_student_id!r}")
        rows = [row]
    else:
        rows, _total = repos["students"].list(page=1, page_size=PEOPLE_PAGE_SIZE)

    if person_types:
        wanted = {t.strip().lower() for t in person_types if t and t.strip()}
        rows = [r for r in rows if (r.get("person_type") or "student") in wanted]

    people = [person_row(r) for r in rows]
    people = [p for p in people if p["id"]]
    if not people:
        return {
            "ok": False, "people": 0, "accepted": 0, "unsupported": False,
            "detail": "", "error": "there is nobody in the local directory yet — "
                                   "press “Refresh from EMS” first, or add a person "
                                   "with “Add person to ApexityEdu”.",
            "by_type": {},
        }

    by_type: dict[str, int] = {}
    for person in people:
        by_type[person["person_type"]] = by_type.get(person["person_type"], 0) + 1

    try:
        outcome = client.push_people(people)
    except EmsAuthError as exc:
        repos["queue"].record_attempt(
            attempt_type="push_people", event_count=len(people),
            result="failed", error=f"auth: {exc}"[:300])
        return {"ok": False, "people": len(people), "accepted": 0,
                "unsupported": False, "detail": "", "error": str(exc)[:300],
                "by_type": by_type}
    except (EmsUnavailableError, EmsError) as exc:
        repos["queue"].record_attempt(
            attempt_type="push_people", event_count=len(people),
            result="failed", error=str(exc)[:300])
        return {"ok": False, "people": len(people), "accepted": 0,
                "unsupported": False, "detail": "", "error": str(exc)[:300],
                "by_type": by_type}

    if outcome.fatal:
        repos["queue"].record_attempt(
            attempt_type="push_people", event_count=len(people),
            result="failed", error=outcome.fatal[:300])
        return {"ok": False, "people": len(people), "accepted": 0,
                "unsupported": False, "detail": outcome.detail,
                "error": outcome.fatal[:300], "by_type": by_type}

    if outcome.unsupported:
        # The EMS has no people-write endpoint: say so plainly instead of
        # pretending the directory was saved.
        repos["queue"].record_attempt(
            attempt_type="push_people", event_count=len(people),
            result="skipped", error="the EMS does not implement POST /people"[:300])
        return {
            "ok": False,
            "people": len(people),
            "accepted": 0,
            "unsupported": True,
            "detail": outcome.detail,
            "error": outcome.detail or "this EMS cannot store people",
            "by_type": by_type,
        }

    repos["queue"].record_attempt(
        attempt_type="push_people", event_count=len(people), result="success",
        detail=f"accepted={outcome.accepted}")
    repos["audit"].log(
        actor="manual", action="people.pushed", entity="people",
        detail=f"{len(people)} people published to the EMS "
               f"(accepted={outcome.accepted})"[:400],
    )
    return {
        "ok": True,
        "people": len(people),
        "accepted": outcome.accepted,
        "created": outcome.created,
        "updated": outcome.updated,
        "unsupported": False,
        "detail": outcome.detail,
        "error": None,
        "by_type": by_type,
    }


def create_person(ctx, person: dict) -> dict:
    """Create or update **one** person in the EMS (the manual add button).

    The person is written to the EMS first and only then cached locally, so
    the People page never shows something the server refused.
    """
    client = _client(ctx)
    payload = person_row(person)
    if not payload["id"]:
        raise PushError("the person needs an EMS id (for example S1001 or T2001)")
    if not payload["name"]:
        raise PushError("the person needs a name")

    try:
        outcome = client.push_people([payload])
    except EmsAuthError as exc:
        return {"ok": False, "person": payload, "error": str(exc)[:300]}
    except (EmsUnavailableError, EmsError) as exc:
        return {"ok": False, "person": payload, "error": str(exc)[:300]}

    if outcome.fatal:
        return {"ok": False, "person": payload, "error": outcome.fatal[:300]}
    if outcome.unsupported:
        return {
            "ok": False, "person": payload, "unsupported": True,
            "error": outcome.detail or "this EMS cannot store people",
        }

    # Cache it locally so the bridge's People page shows the new person too.
    ctx.repos["students"].upsert_many([{
        "ems_student_id": payload["id"],
        "name": payload["name"],
        "person_type": payload["person_type"],
        "admission_no": payload.get("admission_no"),
        "roll_no": payload.get("roll_no"),
        "class_name": payload.get("class"),
        "section": payload.get("section"),
        "employee_no": payload.get("employee_no"),
        "designation": payload.get("designation"),
        "department": payload.get("department"),
        "active": payload.get("active", True),
    }])
    ctx.repos["audit"].log(
        actor="manual", action="people.created", entity="person",
        entity_id=payload["id"],
        detail=f"{payload['name']} ({payload['person_type']}) written to the EMS"[:400],
    )
    return {
        "ok": True,
        "person": payload,
        "accepted": outcome.accepted,
        "created": outcome.created,
        "updated": outcome.updated,
        "detail": outcome.detail,
        "error": None,
    }


# --------------------------------------------------------------- attendance --
def push_attendance_now(ctx, limit: int = 500) -> dict:
    """Push everything that is due right now (used by “Sync now” / the scheduler)."""
    worker = getattr(ctx, "sync_worker", None)
    if worker is None:
        raise PushError("the sync worker is not running")
    pushed = worker.push_due_events()
    pending = ctx.repos["events"].counts()
    return {
        "ok": True,
        "pushed": pushed,
        "pending": pending.get("pending", 0) + pending.get("failed", 0),
        "synced": pending.get("synced", 0),
    }


def format_push_result(result: dict) -> str:
    """Human-readable rendering used by the desktop app and the CLI."""
    if result.get("error") and not result.get("devices"):
        return str(result["error"])
    lines: list[str] = []
    for device in result.get("devices") or []:
        mark = "✓" if device.get("state") == "pushed" else "✗"
        lines.append(
            f"{mark} {device.get('label') or device.get('serial')} "
            f"(SN {device.get('ems_serial')}): {device.get('users')} user(s) — "
            f"{device.get('state')}"
            + (f" — {device.get('error')}" if device.get("error") else "")
        )
    if result.get("pushed"):
        lines.append(f"Published {result['pushed']} terminal(s), "
                     f"{result.get('total_users', 0)} user(s) in total.")
    if result.get("error") and result.get("devices"):
        lines.append(str(result["error"]))
    return "\n".join(lines)
