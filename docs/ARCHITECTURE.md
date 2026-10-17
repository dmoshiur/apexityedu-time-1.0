# Architecture — ApexityEdu Smart Attendance Bridge

```
            ┌────────────── school LAN (no internet exposure) ──────────────┐
            │                                                               │
 ZKTeco K40 ──┐   ZK push-SDK protocol (UDP/TCP 4370, READ-ONLY)             │
 (gate)       ├──┐                                                            │
 ZKTeco K40 ──┤  │   ┌──────────────────────── Bridge (this Windows PC) ────┐│
 (hall)       ├──┼──▶│  discovery ──▶ collector ──▶ pipeline ──▶ SQLite    ││
            ──┘  │   │                               │                    ││
                 │   │      sync worker (offline-first queue) ▼            ││
                 │   │      dashboard (127.0.0.1 only, session+CSRF)       ││
                 │   └──────────────────────┬─────────────────────────────┘│
            └──────────────────────────────┼───────────────────────────────┘
                                       HTTPS│ (iClock/ADMS push  or  REST + Bearer token)
                                            ▼
                                 apexityedu.com (EMS)
```

## Modules

| Module | Responsibility |
|---|---|
| `discovery/` | Enumerate PC subnets (`psutil`), rate-limited TCP probing (4370 default + extras), optional ICMP aid, ARP table hints, protocol-level identification, periodic re-scan with verified IP repair, "Scan LAN Now". |
| `devices/` | `DeviceAdapter` interface + `ZkDeviceAdapter` (pyzk) + `FakeDeviceAdapter` (tests/dev). Read-only contract; also mirrors the terminal's **user table** (PIN + name). |
| `attendance/` | Per-device collectors (round-robin, per-device exponential backoff, identity re-verified on **every** connect) + the user-table mirror, and the event pipeline (dedup + validation + mapping resolution). |
| `sync/` | Offline-first worker: EMS health checks, durable task queue, batched pushes with jittered backoff, **device-user publication**, people-directory refresh, read-only ADMS command answering with `devicecmd` acknowledgement and once-only refusal audits. |
| `ems/` | `IclockEmsClient`, `RestEmsClient`, live `probe`, ingestion `verify`, factory (supports split mode: iClock pushes + REST roster), and `push.py` — the **manual push service** (device user tables, people directory, single person) that reports exactly what the server answered. |
| `students/` | People-directory refresh (students + teachers + staff) + mapping service (manual/CSV, conflicts, unknown-person review). |
| `dashboard/` | Local Flask+waitress UI; session auth (PBKDF2), CSRF + origin checks, login throttling; JSON API. |
| `database/` | WAL SQLite, per-thread connections, migrations, integrity check + quarantine, online backups. |
| `security/` | OS credential store for secrets (keyring → Windows Credential Manager), password hashing. |
| `windows_service/` | Task Scheduler auto-start (default), optional pywin32 service, single-instance mutex. |

## Who punched? (device users → people)

A K40 reports a punch as `PIN` + timestamp. Nothing else. Two caches therefore
exist side by side and are joined in the UI:

* **`device_users_cache`** — the user table enrolled *on the terminal*, read
  over the ZK protocol (read-only; templates and passwords are never touched,
  and `Passwd=` is always empty when the table is published).
* **`students_cache`** — the EMS *people* directory, downloaded from
  ApexityEdu. `person_type` distinguishes `student` / `teacher` / `staff` /
  `other`, because a school's terminals are punched by all of them.

`device_user_mappings` binds (serial, PIN) → person. Until a PIN is bound, a
punch is legitimate data with no owner: it is still stored and still pushed
(an EMS with the user table can resolve it), but the dashboard marks it
*NOT MAPPED* instead of pretending to know.

The bridge also **publishes** the user table to the EMS (`table=USER` over
iClock, `POST /devices/users` over REST) whenever it changes or the server
asks via `DATA QUERY USERINFO`. That is what lets the EMS offer its own
"map this device user" screen.

## Device identity vs EMS identity

The *hardware* serial identifies the terminal on the LAN (never the IP). The
*EMS* serial (`devices.ems_serial`) is the name ApexityEdu knows the device
by — schools routinely register a device in the EMS under a different string.
They are separate on purpose:

* collection, discovery and IP repair always use the hardware serial;
* every push (ATTLOG, USER, handshake, presence) uses
  `ems_serial or serial_number`.

A mismatch between the two is the single most common reason a deployment says
"connected" while nothing arrives. `verify-ems` detects it.

## Device identity rules (never violated)

1. **IP is never identity.** It is mutable state on a device row.
2. Identification signals, strongest first: **serial number** (protocol),
   protocol-verified model+firmware, MAC (hint only — spoofable, absent across
   routers).
3. Discovery marks unknown serials `pending`. Registration is a deliberate,
   audited admin action.
4. IP changes are repaired automatically **only** after the serial at the new
   address matches, and every repair lands in the audit log.
5. Collectors refuse to read a device whose reported serial differs from the
   registered one (protects against a stranger inheriting the old IP via DHCP).
6. If no serial can be read → observation only, never a registered device.

## Honest status: "online" is not "accepted"

`EmsStatus.online` means *the EMS answered a handshake*. iClock servers answer
a handshake for unknown serials by design, so this is not evidence of
ingestion. Two separate flags exist and the dashboard shows both:

* `online` — endpoint reachable.
* `ingestion_verified` — the server explicitly accepted an ATTLOG upload
  (`OK` / `OK: n`). Only real acceptance sets it.

`python -m apexity_bridge verify-ems` and **Settings → Verify what the EMS
accepts** produce the same evidence on demand, per serial, including an
**empty** ATTLOG POST that proves the ingestion route without writing a single
fake punch.

## Offline-first sync design

- Every record is written to `attendance_events` (SQLite, WAL) **before** any
  network call. The device log on the K40 is an additional backup — the bridge
  never deletes device data.
- `sync_status`: `pending → (synced | failed ⇒ retries | review | rejected)`.
  `failed` rows carry `next_retry_at` (jittered exponential backoff,
  `SYNC_RETRY_BASE`→`SYNC_RETRY_MAX`); after 10 attempts they need review.
- `synced` is written **only** after explicit EMS acceptance (protocol-level
  `OK`/`OK: n` or per-event `accepted`/`duplicate` results).
- Crash between "EMS accepted" and "marked synced" → the event is pushed again
  and the EMS resolves it via idempotency (see below). At-least-once delivery
  with idempotent acceptance == exactly-once effect.

### Idempotency (tested, documented limitations included)

`dedup_hash = sha256("v2" | serial | device_user_id | exact_timestamp | status | punch)`

- pyzk's record `uid` is the **user enrol slot**, not a record id — verified in
  the pyzk source — so it cannot key dedup, and the bridge stores it for
  diagnostics only.
- Multiple different users punching at the same timestamp survive (user_id is
  in the hash). Requirement "identical timestamps" is covered by
  `tests/test_pipeline.py::test_same_timestamp_different_users_are_distinct`.
- **Limitation:** two punches by the *same* user in the *same second* with the
  same codes collapse to one event. K40 scan cycles take >1s, so this cannot
  collide in practice. Timestamp-only checkpoints are never used anywhere.
- REST protocol additionally sends `dedup_hash` as the explicit
  `idempotency_key`; iClock relies on server-side `SN+PIN+ts+status` dedup
  (required by contract).

## Recovery scenarios (all automated)

| Failure | Behaviour |
|---|---|
| PC/Windows restart | Task Scheduler restarts the bridge; single-instance mutex prevents doubles; discovery runs immediately to repair IPs; sync resumes from SQLite. |
| Internet down | Health check flips EMS status offline; everything queues; retry loop resumes when the EMS answers. No data loss (device + SQLite). |
| EMS 5xx/429 | Jittered backoff; attempts logged to `sync_attempts`. |
| Token expired (401/403) | 5-minute penalty delay, dashboard banner, audit entry; set a new token via `set-token`, no data loss. |
| Corrupt SQLite | `PRAGMA integrity_check` on boot; corrupt files are **moved aside** (kept) and a fresh DB is created; restore via dashboard backup restore flow. |
| K40 powered off at boot | Per-device backoff; device rejoins automatically when powered. |

## Dashboard security

Localhost-only by default; explicit `DASHBOARD_ALLOW_LAN=1` needed for
`0.0.0.0`. Every mutating request requires session + CSRF token + same-host
Origin; logins are throttled (5 failures → 60s lockout); password is a salted
PBKDF2-SHA256 hash; secrets live in Windows Credential Manager; a logging
redaction filter scrubs tokens from logs.

## Privacy

The bridge never reads or uploads fingerprint templates (`get_templates` is not
used anywhere). Raw attendance payloads are bounded (2 KB), logs redact
credentials, and the diagnostics bundle contains no secrets.
