"""Secure REST client for the custom ApexityEdu bridge API.

Implements docs/EMS_API_CONTRACT.md exactly:
* Authorization: Bearer <device token> (from the OS credential store)
* GET    {base}/api/v1/bridge/health
* POST   {base}/api/v1/bridge/devices/register
* GET    {base}/api/v1/bridge/students?cursor=...&limit=...
* POST   {base}/api/v1/bridge/attendance/batch
Every attendance event carries a stable idempotency key; the server is the
final authority on acceptance (accepted/duplicate/rejected per event).
"""
from __future__ import annotations

import logging
from urllib.parse import urlparse

import requests

from .base import (
    EmsAuthError,
    EmsProtocolError,
    EmsUnavailableError,
    HealthResult,
    RegisterResult,
    RosterPage,
    StudentInfo,
    UploadOutcome,
    UserSyncOutcome,
)

log = logging.getLogger("apexity_bridge.ems.rest")


class RestEmsClient:
    protocol_name = "rest"
    API = "/api/v1/bridge"

    def __init__(self, base_url: str, *, token_provider=None, timeout: int = 15,
                 session: requests.Session | None = None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._token_provider = token_provider  # callable → str | None
        self._http = session or requests.Session()
        self._http.headers.update(
            {"User-Agent": "ApexityEduBridge/1.0", "Content-Type": "application/json"}
        )

    # ---------------------------------------------------------------- helpers --
    def _headers(self) -> dict:
        token = self._token_provider() if self._token_provider else None
        if not token:
            raise EmsAuthError(
                "no EMS device token configured — run "
                "'python -m apexity_bridge set-token' first"
            )
        return {"Authorization": f"Bearer {token}"}

    def _url(self, suffix: str) -> str:
        return f"{self.base_url}{self.API}{suffix}"

    def _request(self, method: str, suffix: str, *, auth: bool = True, **kwargs) -> dict:
        headers = kwargs.pop("headers", {})
        if auth:
            headers.update(self._headers())
        try:
            resp = self._http.request(
                method, self._url(suffix), timeout=self.timeout, headers=headers, **kwargs
            )
        except requests.Timeout as exc:
            raise EmsUnavailableError(f"EMS timeout: {exc}") from exc
        except requests.ConnectionError as exc:
            raise EmsUnavailableError(f"EMS unreachable: {exc}") from exc
        except requests.RequestException as exc:
            raise EmsUnavailableError(f"EMS request failed: {exc}") from exc

        if resp.status_code in (401, 403):
            raise EmsAuthError(f"EMS HTTP {resp.status_code}: token rejected or expired")
        if resp.status_code >= 500 or resp.status_code == 429:
            raise EmsUnavailableError(f"EMS HTTP {resp.status_code}")
        try:
            payload = resp.json()
        except ValueError as exc:
            raise EmsProtocolError(
                f"EMS returned non-JSON (HTTP {resp.status_code}): {resp.text[:200]!r}"
            ) from exc
        if not isinstance(payload, dict):
            raise EmsProtocolError("EMS returned an unexpected JSON shape")
        if resp.status_code >= 400:
            raise EmsProtocolError(
                f"EMS HTTP {resp.status_code}: {payload.get('error', payload)}"
            )
        return payload

    # ---------------------------------------------------------------- contract --
    def register_device(self, *, serial: str, model: str | None,
                        firmware: str | None, label: str | None) -> RegisterResult:
        payload = self._request(
            "POST", "/devices/register",
            json={"serial": serial, "model": model, "firmware": firmware, "label": label},
        )
        return RegisterResult(
            ok=bool(payload.get("ok")),
            remote_device_id=payload.get("device_id"),
            detail=str(payload.get("detail", ""))[:200],
        )

    def _try_get(self, suffixes: tuple[str, ...], params: dict) -> tuple[str, dict]:
        """GET the first endpoint that exists (404 → try the next one)."""
        last: Exception | None = None
        for suffix in suffixes:
            try:
                return suffix, self._request("GET", suffix, params=params)
            except EmsProtocolError as exc:
                last = exc
                if "404" not in str(exc):
                    raise
        raise last  # type: ignore[misc]

    def fetch_students(self, cursor: str | None = None) -> RosterPage:
        """Backwards-compatible name — the directory contains more than students."""
        return self.fetch_people(cursor)

    def fetch_people(self, cursor: str | None = None,
                     types: tuple[str, ...] | None = None) -> RosterPage:
        """Download students **and** teachers **and** staff.

        Preferred endpoint: ``GET /people`` (``{"people": [...]}``, each with a
        ``person_type``). Falls back to the legacy ``GET /students`` so an EMS
        that only implemented the original contract keeps working — those rows
        are cached as ``person_type="student"``.
        """
        params: dict = {"limit": 500}
        if cursor:
            params["cursor"] = cursor
        if types:
            params["types"] = ",".join(types)

        suffix, payload = self._try_get(("/people", "/students"), params)
        legacy = suffix == "/students"
        people_raw = payload.get("people") if not legacy else None
        if not isinstance(people_raw, list):
            key = "students" if legacy else "people"
            people_raw = payload.get(key)
        if not isinstance(people_raw, list):
            raise EmsProtocolError(
                f"GET {suffix} response missing a '{'people' if not legacy else 'students'}' list"
            )

        people = []
        for s in people_raw:
            if not isinstance(s, dict) or "id" not in s:
                raise EmsProtocolError(f"invalid person entry: {str(s)[:200]}")
            # A directory entry without an explicit type is a student — that is
            # the historical shape of every EMS roster we have seen.
            person_type = str(s.get("person_type") or "student")
            person_type = person_type.strip().lower()
            if person_type in ("employee", "teacher", "faculty"):
                person_type = "teacher"
            elif person_type in ("worker", "non-teaching", "nonteaching"):
                person_type = "staff"
            elif person_type not in ("student", "teacher", "staff", "other"):
                person_type = "other"
            people.append(
                StudentInfo(
                    ems_student_id=str(s["id"]),
                    name=str(s.get("name", "")).strip() or "(unnamed)",
                    admission_no=_opt(s.get("admission_no")),
                    roll_no=_opt(s.get("roll_no")),
                    class_name=_opt(s.get("class")),
                    section=_opt(s.get("section")),
                    active=bool(s.get("active", True)),
                    person_type=person_type,
                    employee_no=_opt(s.get("employee_no") or s.get("employee_id")
                                     or s.get("staff_id")),
                    designation=_opt(s.get("designation")),
                    department=_opt(s.get("department")),
                    raw=s,
                )
            )
        next_cursor = payload.get("next_cursor")
        return RosterPage(
            students=people,
            next_cursor=str(next_cursor) if next_cursor else None,
            complete=not next_cursor,
        )

    def push_users(self, serial: str, users: list[dict]) -> UserSyncOutcome:
        """Optional: publish a terminal's user table over REST.

        Only needed when the EMS wants to show "device users" in its own UI and
        has no iClock USER handler. 404 → reported as unsupported, not fatal.
        """
        if not users:
            return UserSyncOutcome(accepted=0, detail="no users to push")
        try:
            payload = self._request(
                "POST", "/devices/users",
                json={
                    "serial": serial,
                    "users": [
                        {"user_id": u.get("user_id"), "name": u.get("name"),
                         "privilege": u.get("privilege"), "card": u.get("card")}
                        for u in users
                    ],
                },
            )
        except EmsProtocolError as exc:
            if "404" in str(exc):
                return UserSyncOutcome(
                    accepted=0,
                    detail="EMS does not implement POST /devices/users (optional)",
                    fatal=None,
                )
            return UserSyncOutcome(fatal=str(exc)[:300])
        return UserSyncOutcome(
            accepted=int(payload.get("accepted", len(users)) or 0),
            detail=str(payload.get("detail", ""))[:200],
        )

    def health_check(self) -> HealthResult:
        payload = self._request("GET", "/health")
        return HealthResult(
            ok=bool(payload.get("ok")),
            server_time=payload.get("server_time"),
            detail=str(payload.get("detail", ""))[:200],
            protocol=self.protocol_name,
        )

    def push_attendance(self, events: list[dict], *, serial: str | None = None) -> UploadOutcome:
        if not events:
            return UploadOutcome()
        if serial:
            for ev in events:
                ev.setdefault("ems_serial", serial)
        payload = self._request(
            "POST", "/attendance/batch",
            json={
                "events": [
                    {
                        "idempotency_key": ev["dedup_hash"],
                        "device_serial": ev["device_serial"],
                        "device_user_id": ev["device_user_id"],
                        "timestamp": ev["event_timestamp"],
                        "timestamp_tz": ev["event_timestamp_tz"],
                        "status": ev.get("status"),
                        "verify": ev.get("verify_mode") or ev.get("punch"),
                        "ems_student_id": ev.get("ems_student_id"),
                        "ems_person_id": ev.get("ems_student_id"),
                        "ems_serial": ev.get("ems_serial") or ev["device_serial"],
                    }
                    for ev in events
                ]
            },
        )
        results = payload.get("results")
        if not isinstance(results, list):
            raise EmsProtocolError("attendance/batch response missing 'results' list")

        by_key = {ev["dedup_hash"]: ev for ev in events}
        accepted, remote_ids, rejected, duplicates = [], {}, {}, []
        for item in results:
            key = str(item.get("idempotency_key", ""))
            ev = by_key.get(key)
            if ev is None:
                log.warning("EMS returned unknown idempotency key %s", key[:16])
                continue
            status = str(item.get("status", "")).lower()
            if status == "accepted":
                accepted.append(ev["id"])
                if item.get("remote_id") is not None:
                    remote_ids[ev["id"]] = str(item["remote_id"])
            elif status == "duplicate":
                duplicates.append(ev["id"])
                if item.get("remote_id") is not None:
                    remote_ids[ev["id"]] = str(item["remote_id"])
            elif status == "rejected":
                rejected[key] = str(item.get("reason", "rejected"))[:300]
            else:
                raise EmsProtocolError(f"unknown result status {status!r}")
        return UploadOutcome(
            accepted_ids=tuple(accepted),
            remote_ids=remote_ids,
            rejected=rejected,
            duplicates=tuple(duplicates),
        )


def _opt(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
