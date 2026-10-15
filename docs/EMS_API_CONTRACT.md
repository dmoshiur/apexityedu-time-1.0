# ApexityEdu EMS — Attendance Ingestion Contract (required server-side)

> **Status: open item for the EMS team.** The bridge was developed without
> access to the EMS source code or database. Everything below is the exact,
> minimal contract the bridge needs. Until one of the two protocols is live,
> the bridge keeps collecting and queueing attendance locally and the
> dashboard shows the precise blocker — nothing is faked.

Run this on the school PC to check what the EMS currently speaks:

```powershell
.\.venv\Scripts\python.exe -m apexity_bridge probe-ems
```

It performs only read-only GETs — it never posts attendance data.

---

## Option A — ZKTeco iClock / ADMS push endpoint (preferred if it already exists)

Many school systems ship a ZKTeco ADMS handler so that terminals push data
directly. The bridge can act as such a terminal for every real device (it uses
the device's own serial number as its identity).

| Item | Value |
|---|---|
| Handshake | `GET {EMS_BASE}/iclock/cdata?SN=<serial>&options=all&pushver=2.4.2&language=69` |
| Upload | `POST {EMS_BASE}/iclock/cdata?SN=<serial>&table=ATTLOG&Stamp=<unix>` |
| Body | `text/plain`, one TAB-separated line per punch: `PIN\tYYYY-MM-DD HH:MM:SS\tstatus\tverify\tworkcode\t0\t0` |
| Success reply | `OK` or `OK: <count-of-accepted-lines>` (HTTP 200) |
| Presence poll (optional) | `GET {EMS_BASE}/iclock/getrequest?SN=<serial>` → `OK` or `C:...` command |
| Security | HTTPS **required**. The EMS must whitelist accepted serial numbers. Optional extension the bridge already sends when configured: `AuthToken=<shared secret>` query param **and** `X-Auth-Token` header (token stored in Windows Credential Manager). Reject requests with `401/403` when the token/SN is wrong. |
| Server-side dedup | Mandatory: treat `SN + PIN + timestamp + status` as the natural unique key. The bridge may re-push a record after a crash; uploads must be idempotent. |

`status` is the device check-type code (K40 firmware: `0`=check-in, `1`=check-out,
`4`=overtime-start, `5`=overtime-end — treat as informational; apply the
school's rules server-side). The `PIN` is the biometric user ID **on the
device**, not the EMS student id.

### What the EMS should do with the data
1. Map `SN` → registered device (site/branch).
2. Map `PIN` → student via its biometric-mapping table (managed by the bridge's
   UI and exported to the EMS via the rota below **or** read from the EMS's own
   mapping UI — pick one source of truth).
3. Insert attendance idempotently; respond `OK: <accepted count>`.

### iClock limitations (acknowledged by design)
- **No student-roster download.** The Students page requires Option B
  (`GET /api/v1/bridge/students`). If the EMS only has iClock, either implement
  this one read-only endpoint, or accept that Students shows a precise error.
- No per-record ids in responses → the bridge marks whole batches synced on
  `OK`; partial acceptances via `OK: n` are retried automatically.

---

## Option B — REST bridge API (recommended for new work)

Base: `{EMS_BASE}/api/v1/bridge`. All requests carry
`Authorization: Bearer <device-token>` (issued in the EMS admin UI, revocable).
HTTPS required. JSON everywhere. All timestamps ISO-8601.

### `GET /health`
→ `200 {"ok": true, "server_time": "2026-10-15T06:05:00+00:00"}`
Used for liveness + clock-drift warnings. Auth required.

### `POST /devices/register`
```json
{ "serial": "X9C12345", "model": "K40", "firmware": "Ver 6.60 Dec 3 2019", "label": "Main gate" }
```
→ `200 {"ok": true, "device_id": "dev-123"}` — idempotent upsert by serial;
reject unknown/blacklisted serials with 403.

### `GET /students?cursor=&limit=500`
→
```json
{
  "students": [
    {"id": "S1001", "name": "Afnan Rahman", "admission_no": "ADM-001",
     "roll_no": "12", "class": "Six", "section": "A", "active": true}
  ],
  "next_cursor": "opaque-or-null"
}
```
Paginated roster of active+inactive students. This powers the bridge's student
directory and the `device user → student` mapping UI. Read-only from the
bridge's perspective; the bridge never creates students.

### `POST /attendance/batch`
```json
{
  "events": [
    {
      "idempotency_key": "sha256-hex-64",
      "device_serial": "X9C12345",
      "device_user_id": "7",
      "timestamp": "2026-10-15T08:01:22",
      "timestamp_tz": "Asia/Dhaka",
      "status": 0,
      "verify": 1,
      "ems_student_id": "S1001"
    }
  ]
}
```
Semantics:
- `idempotency_key` is a SHA-256 of `serial|device_user_id|exact timestamp|status|verify`.
  Store it; the second submission of the same key must answer `duplicate`.
- `timestamp` is the device's local clock value *verbatim* (do not shift it);
  `timestamp_tz` tells you how to interpret it. Reject clock-skewed values
  (>48h future) with `rejected`.
- `ems_student_id` may be `null` when unmapped — accept and flag for review.
- `status`/`verify` are raw device codes; `verify` `0`=password-ish, `1`=finger,
  `2`=card (firmware dependent).

→ `200 {"results": [{"idempotency_key": "...", "status": "accepted", "remote_id": 91011}]}`

Per-event statuses: `accepted` | `duplicate` (include existing `remote_id`) |
`rejected` (include human-readable `reason`; the bridge shows these under
**Attendance → Rejected** and does not retry them automatically).

Errors: `401/403` token invalid/expired; `429/5xx` retryable — the bridge
backs off exponentially (30s → 30min cap) and never loses queued data.

---

## Reference implementation

`docs/reference/ems_reference_api.py` is a working Flask implementation of
Option B (with a minimal schema) that the EMS team can lift into their stack,
and that the school can temporarily run anywhere reachable as a relay. It is
also what the bridge's test-suite fakes mirror, so behavior is pinned.
