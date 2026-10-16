# ApexityEdu EMS — Attendance Ingestion Contract (required server-side)

> **Status: three requirements left, all small.**
> Everything the bridge needs is listed below. **ApexityEdu already speaks
> ZKTeco iClock/ADMS** — see the verified findings under
> [Option A](#option-a--zkteco-iclock--adms-push-endpoint-already-present-on-apexityeducom)
> — so the remaining work is: accept `POST` ATTLOG for a registered serial,
> accept the `USER` table, and (optionally) expose the people directory.
> The fourth item — *writing* people (`POST /people`, Option E) — is only
> needed for the desktop app's manual "add person" button.
> The bridge keeps collecting and queueing locally until then and says exactly
> what is missing — nothing is faked.

> ### Verified against the live server (2026-10-16, read-only probes)
>
> | Check | Request | Result |
> |---|---|---|
> | iClock handshake | `GET https://apexityedu.com/iclock/cdata?SN=APEXITY-PROBE&options=all` | `HTTP 200` + `GET OPTION FROM: APEXITY-PROBE` block (`Stamp`, `OpStamp`, `TransFlag`, `Realtime=1`, …) |
> | Presence poll | `GET https://apexityedu.com/iclock/getrequest?SN=APEXITY-PROBE` | `HTTP 200` + `OK` |
> | ATTLOG upload | `POST …/iclock/cdata?SN=…&table=ATTLOG` | **not yet verified** — run `python -m apexity_bridge verify-ems` on the school PC |
>
> The handshake answered for a serial that certainly is **not** registered in
> ApexityEdu. That is normal iClock behaviour (terminals fetch their config
> before they are known), and it is exactly why **a green "EMS online" badge
> proves nothing about attendance**. Use `verify-ems` for proof.

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
| Presence poll | `GET {EMS_BASE}/iclock/getrequest?SN=<serial>` → `OK` or `C:...` command; the bridge sends this every health interval (~30 s) for registered serials after a successful handshake |
| Security | HTTPS **required**. The EMS must whitelist accepted serial numbers. Optional extension the bridge already sends when configured: `AuthToken=<shared secret>` query param **and** `X-Auth-Token` header (token stored in Windows Credential Manager). Reject requests with `401/403` when the token/SN is wrong. |
| Server-side dedup | Mandatory: treat `SN + PIN + timestamp + status` as the natural unique key. The bridge may re-push a record after a crash; uploads must be idempotent. |

`status` is the device check-type code (K40 firmware: `0`=check-in, `1`=check-out,
`4`=overtime-start, `5`=overtime-end — treat as informational; apply the
school's rules server-side). The `PIN` is the biometric user ID **on the
device**, not the EMS student id.

### The three things ApexityEdu must do for Option A to work end-to-end

1. **Accept `POST` on the `cdata` route for `table=ATTLOG`** and answer
   `OK: <accepted count>` with HTTP 200. Today a `GET` with `table=ATTLOG`
   returns the handshake block (verified), so the handler is currently
   `GET`-only for data. A redirect to a login page, HTML, JSON or an empty
   body is treated as *not accepted* and the bridge retries.
2. **Use the serial the bridge pushes.** The SN in the query string must be
   the one listed under *Biometric Devices*, otherwise there is nothing to
   attach the punch to. When the two differ, the school sets the device's
   **EMS serial** on the bridge's Devices page (or via
   `POST /api/devices/<id>/ems-serial`) — the bridge then pushes under that
   serial while still identifying the terminal by its hardware serial.
3. **Accept `table=USER`** (see [Option D](#option-d--publishing-the-terminals-user-table-how-a-pin-becomes-a-person))
   so a `PIN` can be resolved to a person. Without it every punch arrives
   anonymous and ApexityEdu has no way to attribute it.

### Acceptance and presence checks
- Only **HTTP 200** with a complete `OK` or `OK: <count>` reply acknowledges
  an attendance upload. Counts must be between zero and the submitted batch
  size. `OK: 0` acknowledges no records; unaccepted records stay retryable.
  Empty replies, HTML/login pages, JSON, handshake blocks, malformed counts,
  and redirects are errors, never evidence of successful attendance storage.
  Configure the final iClock URL: the client does not follow redirects.
- With registered devices, the sync worker checks each **real serial** via
  `cdata` and then `getrequest` every health interval, even when there are no
  punches. Cached registration flags do not replace these checks. Without
  registered devices, the synthetic health check tests endpoint availability
  only. Probe/health success alone does not prove attendance ingestion.
- The EMS should update its device `last_seen` on authenticated real-serial
  communication. The bridge cannot force an EMS UI status change: that logic
  and the tenant/SN mapping belong to the EMS backend. Presence here is
  **bridge-to-EMS presence**, not proof the physical terminal is powered on;
  the bridge's Devices page separately tracks LAN reachability.
- If a server does not implement `getrequest`, the bridge exposes the poll
  failure but still attempts attendance uploads after a valid handshake.
  A failed serial does not stop presence checks for other registered devices.
  Per-serial handshake/poll results are available in the overview API under
  `ems.device_presence`; errors are also in the overview and application log.
- Commands received through polling are **logged only**, never executed or
  acknowledged as executed. User synchronization and device administration
  are not added by presence polling.
- The configured token is still the documented shared-token extension
  (`AuthToken` and `X-Auth-Token`). An EMS with a different or per-device API
  key contract needs explicit integration; a generic handshake does not
  prove that such a key was accepted. Never paste credentials into logs/chat.

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

## Option C — the people directory (students **and** teachers **and** staff)

A terminal does not care who punches: teachers, guards and office staff use
the same K40. A directory that only lists students makes those punches
unattributable forever.

`GET {EMS_BASE}/api/v1/bridge/people?cursor=&limit=500&types=student,teacher,staff`

```json
{
  "people": [
    {"id": "S1001", "name": "Afnan Rahman", "person_type": "student",
     "admission_no": "ADM-001", "roll_no": "12", "class": "Six", "section": "A",
     "active": true},
    {"id": "T2001", "name": "Sir Alam", "person_type": "teacher",
     "employee_no": "EMP-01", "designation": "Headmaster", "active": true},
    {"id": "X3001", "name": "Bacchu Mia", "person_type": "staff",
     "employee_no": "EMP-77", "designation": "Guard", "active": true}
  ],
  "next_cursor": "opaque-or-null"
}
```

* `person_type`: `student` (default) | `teacher` | `staff` | `other`.
  Aliases are accepted: `employee`/`faculty` → `teacher`,
  `worker`/`non-teaching` → `staff`.
* `class`/`section` are meaningful for students; `designation`/`department`
  for teachers and staff. Send whichever apply — the bridge stores all of them.
* **Backwards compatible:** if `/people` returns 404 the bridge silently falls
  back to the legacy `GET /students` and caches everyone as `student`.
* The bridge **never creates people** — this endpoint is read-only from the
  bridge's side.

Optional mirror of the same idea over iClock is not possible; that is why the
roster side-channel exists (see **Split mode** below).

### Split mode (recommended starting point for ApexityEdu)

Attendance and the directory do not have to travel over the same protocol:

* punches → **iClock ATTLOG** (already present, no new endpoint needed)
* people  → **REST `/people`** (one new read-only endpoint)

Set `EMS_PROTOCOL=iclock` and `EMS_ROSTER_MODE=auto` and the bridge does
exactly that. `EMS_ROSTER_MODE=off` disables the directory download entirely.

## Option D — publishing the terminal's user table (how a PIN becomes a person)

An ATTLOG line carries only a `PIN`. Unless ApexityEdu knows "PIN 7 = Anika",
it cannot attribute anything. Two supported ways to teach it that:

**iClock (preferred — no new endpoint shape, reuse `cdata`):**

```
POST {EMS_BASE}/iclock/cdata?SN=<serial>&table=USER&Stamp=<unix>
Content-Type: text/plain

USER PIN=7\tName=Anika Hossain\tPri=0\tPasswd=\tCard=\tGrp=1\tTZ=0000000000000000
USER PIN=12\tName=Sir Alam\tPri=0\tPasswd=\tCard=\tGrp=1\tTZ=0000000000000000
```

Reply `OK` or `OK: <count>` (HTTP 200). The bridge sends this automatically
(``EMS_PUSH_USERS=1``) whenever a terminal's user table changes, and also
immediately when the server asks for it via `DATA QUERY USERINFO`.
**No fingerprints and no passwords are ever transmitted** (``Passwd=`` is
always empty) — the bridge never calls ``get_templates``.

**REST:**

```
POST {EMS_BASE}/api/v1/bridge/devices/users
Authorization: Bearer <device-token>
{"serial": "K40-1", "users": [{"user_id": "7", "name": "Anika Hossain",
                              "privilege": 0, "card": null}]}
→ 200 {"ok": true, "accepted": 1}
```

Once ApexityEdu has the user table it can offer a *"map device user → person"*
screen, exactly like the bridge's own **Device Users** page.

## Option E — writing people to the EMS (students, teachers and staff)

Option C is the **download** direction. This is the **write** direction: the
bridge can publish the people directory (or a single person) to ApexityEdu,
which is what the desktop app's **People → "Push people to ApexityEdu"** and
**"Add person to ApexityEdu…"** buttons use.

It is **optional**. An EMS that implements only `GET /people` answers 404 on
this endpoint, and the bridge then says so in plain words
("this EMS does not implement POST /api/v1/bridge/people") instead of
pretending the people were saved.

### `POST /api/v1/bridge/people`

```http
POST {EMS_BASE}/api/v1/bridge/people
Authorization: Bearer <device-token>
Content-Type: application/json
```
```json
{
  "people": [
    {"id": "S1001", "name": "Afnan Rahman", "person_type": "student",
     "admission_no": "ADM-001", "roll_no": "12", "class": "Six", "section": "A",
     "active": true},
    {"id": "T2001", "name": "Sir Alam", "person_type": "teacher",
     "employee_no": "EMP-01", "designation": "Headmaster", "active": true},
    {"id": "X3001", "name": "Bacchu Mia", "person_type": "staff",
     "employee_no": "EMP-77", "designation": "Guard", "active": true}
  ]
}
```
→ `200 {"ok": true, "accepted": 3, "created": 2, "updated": 1, "detail": "stored"}`

Rules the server must follow:

* **Upsert keyed by `id`** — sending the same person twice must update the
  existing row, never create a duplicate. The bridge may press the button
  as often as it likes.
* **`id` is required**; `name` is required for a usable row. Unknown fields
  are ignored.
* `person_type`: `student` (default) | `teacher` | `staff` | `other`.
* `class`/`section` are meaningful for students, `designation`/`department`
  for teachers and staff — send whichever apply.
* `active: false` marks a person as no longer attending (the bridge keeps
  inactive people in the directory, it never deletes them).
* The reply must be JSON. `404` means "not implemented" and the bridge
  reports that as a missing capability, not as a failure to retry.
* `401/403` → the token/serial was rejected; `429/5xx` → retryable.

A complete, runnable implementation of this endpoint (plus Options B, C and
D) is in [`reference/ems_reference_api.py`](reference/ems_reference_api.py).

### iClock has no equivalent
iClock/ADMS carries device tables and punches only. People are written over
REST — set `EMS_PROTOCOL=rest`, or keep `EMS_ROSTER_MODE=auto` so the REST
side-channel stays available next to the iClock push protocol.

## Commands the bridge will answer (and the ones it never will)

The bridge polls `getrequest` every health interval. It answers **read-only**
ADMS commands by uploading the corresponding table, and refuses everything
else (``EMS_EXECUTE_COMMANDS=0`` disables even these):

| Command | Bridge response |
|---|---|
| `C:n:DATA QUERY USERINFO` | uploads `table=USER` (see Option D) |
| `C:n:DATA QUERY ATTLOG` | re-uploads up to 1000 stored punches (idempotent server-side) |
| `C:n:DATA QUERY OPERLOG` | no payload (the bridge keeps no operation log) |
| `C:n:DATA QUERY BIODATA` / `FINGERTMP` | ignored — biometric templates are never handled |
| `CHECK`, `INFO`, `OPTIONS` | no payload (answered by the next handshake) |
| `CLEAR …`, `DELETE …`, `REBOOT`, `SET …`, `ENROLL …`, `INIT …` | **refused**, logged and written to the audit log |

## Which medium should ApexityEdu use? (the short answer)

| Goal | Use |
|---|---|
| Get punches flowing with the least server work | **Option A** — accept `POST …/iclock/cdata?…&table=ATTLOG` for the registered serials and reply `OK: <n>`. Nothing else is required for data to move. |
| Let a school see/choose who punched | **Option D** — accept `table=USER`, or implement `/api/v1/bridge/devices/users`. |
| Push students **and teachers and staff** into the bridge | **Option C** — `GET /api/v1/bridge/people`. |
| Let the school create students/teachers/staff from the desktop app | **Option E** — `POST /api/v1/bridge/people` (upsert by `id`). |
| Full two-way integration later | **Option B** — the REST contract (health, register, people, attendance/batch, devices/users). |

There is deliberately **no "server pulls from the school LAN" option**: the
school PC sits behind NAT with no inbound route, so the bridge must be the one
initiating every request. A relay (the bridge pushing to ApexityEdu, or
`docs/reference/ems_reference_api.py` running somewhere reachable) is the only
architecture that works without opening the school network to the internet.

## Reference implementation

`docs/reference/ems_reference_api.py` is a working Flask implementation of
Option B (with a minimal schema) that the EMS team can lift into their stack,
and that the school can temporarily run anywhere reachable as a relay. It is
also what the bridge's test-suite fakes mirror, so behavior is pinned.
