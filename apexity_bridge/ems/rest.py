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
    def health_check(self) -> HealthResult:
        payload = self._request("GET", "/health")
        return HealthResult(
            ok=bool(payload.get("ok")),
            server_time=payload.get("server_time"),
            detail=str(payload.get("detail", ""))[:200],
            protocol=self.protocol_name,
        )

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

    def fetch_students(self, cursor: str | None = None) -> RosterPage:
        params: dict = {"limit": 500}
        if cursor:
            params["cursor"] = cursor
        payload = self._request("GET", "/students", params=params)
        students_raw = payload.get("students")
        if not isinstance(students_raw, list):
            raise EmsProtocolError("GET /students response missing 'students' list")
        students = []
        for s in students_raw:
            if not isinstance(s, dict) or "id" not in s:
                raise EmsProtocolError(f"invalid student entry: {str(s)[:200]}")
            students.append(
                StudentInfo(
                    ems_student_id=str(s["id"]),
                    name=str(s.get("name", "")).strip() or "(unnamed)",
                    admission_no=_opt(s.get("admission_no")),
                    roll_no=_opt(s.get("roll_no")),
                    class_name=_opt(s.get("class")),
                    section=_opt(s.get("section")),
                    active=bool(s.get("active", True)),
                    raw=s,
                )
            )
        next_cursor = payload.get("next_cursor")
        return RosterPage(
            students=students,
            next_cursor=str(next_cursor) if next_cursor else None,
            complete=not next_cursor,
        )

    def push_attendance(self, events: list[dict]) -> UploadOutcome:
        if not events:
            return UploadOutcome()
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
