# Troubleshooting

First stop: **Dashboard → Logs** and `…\ApexityEduBridge\logs\bridge.log`.
Generate a support bundle from **Settings → Export diagnostics bundle** (all
secrets are redacted) before contacting support.

> **Settings page frozen, `ems.command_refused` repeating, or a Celery error?
> Start with the dedicated walkthrough:
> [`SETTINGS_AND_SYNC_TROUBLESHOOTING.md`](SETTINGS_AND_SYNC_TROUBLESHOOTING.md).**
> It covers the database, the Windows Credential Manager token, serial
> registration, the ADMS command acknowledgement and which errors belong to
> the ApexityEdu backend. The one-command overview is:
>
> ```powershell
> python -m apexity_bridge doctor
> ```

---

## Device problems

### "Scan LAN now" finds nothing
1. Can the PC reach the device at all? `ping <k40-ip>` and
   `Test-NetConnection <k40-ip> -Port 4370` in PowerShell.
2. If 4370 answers but the bridge still can't see it, run the connectivity
   proof with more detail:
   `.\.venv\Scripts\python.exe -m apexity_bridge scan --ip <k40-ip> --verbose`
3. If the K40 is on a different subnet/VLAN, add that CIDR to
   `DISCOVERY_EXTRA_SUBNETS` in `.env`, or use **Devices → Manual registration**
   (the bridge still verifies the serial before registering).
4. Check `DISCOVERY_USE_PING=1` as an extra aid on large subnets — never
   required (TCP probing always runs).

### Device shows "authentication failed"
The K40 has a comm key set. Enter it via DB edit (`comm_key` on the device row —
set it through the dashboard device registration flow, or set `DEVICE_COMM_KEY`
in `.env`). Verify with:
`python -m apexity_bridge scan --ip <ip> --comm-key <n> --read-attendance 5`

### "identity mismatch" in logs
Another terminal is answering at the registered IP (DHCP churn). The bridge
refused to collect on purpose. Click **Scan LAN now** — the real device will be
found by serial and its IP repaired automatically. Pin IPs via DHCP reservations
to reduce churn.

### Device timestamps look wrong
The bridge stores device timestamps *verbatim* (Asia/Dhaka context). Check the
K40's own clock from its menu; fix it there. Events with absurd timestamps
(pre-2000 or >48h in the future) are quarantined to `review` status instead of
being uploaded.

### Records stopped appearing but device is online
The K40 log may have been cleared by ZKTime 5.0 or an admin — the bridge still
has its own copy in SQLite (Attendance page) and never deletes device data.
Check **Logs** for collect errors; run **Devices → Test**.

---

## EMS / sync problems

### "Everything says OK, but nothing reaches ApexityEdu"  ← start here

A green *EMS ONLINE* badge only means **the server answered a handshake**.
Every iClock/ADMS server answers a handshake for a serial it has never heard
of — that is how real terminals fetch their configuration before they are
registered — so a handshake proves nothing about ingestion. The bridge
therefore keeps a separate, honest signal: **Ingestion verified**, which flips
only after the server explicitly acknowledged an ATTLOG upload.

Run the one command that settles it, on the school PC:

```powershell
.\.venv\Scripts\python.exe -m apexity_bridge verify-ems
```

It performs, per registered device: handshake → presence poll → **an ATTLOG
POST with an empty body** (no fake punch is ever written into the school
register) → the USER table post, and prints the server's exact reply for each
step. Then work through the results in this order:

| What `verify-ems` shows | Cause | Fix |
|---|---|---|
| `attlog_route: FAIL` + `reply: HTML / JSON / empty` | the EMS does not accept `POST` on the cdata route (a `GET` with `table=ATTLOG` returning the handshake block is the classic symptom) | server-side: accept `POST …/iclock/cdata?SN=…&table=ATTLOG` and answer `OK: <n>` (`docs/EMS_API_CONTRACT.md` § Option A) |
| `attlog_route: FAIL` + `403`/`401` | the serial the bridge pushes is not the one registered in ApexityEdu → Biometric Devices | on **Devices → EMS serial…** enter the serial ApexityEdu shows, then re-run `verify-ems` |
| `attlog_route: OK` but ApexityEdu shows nobody | the punch has a bare PIN and the EMS cannot resolve it | publish the user table: **Device Users → Read from device → Publish to ApexityEdu**; server-side accept `table=USER` (Option D) |
| everything OK, but the People page is empty | the EMS exposes no directory endpoint | server-side: `GET /api/v1/bridge/people` (students + teachers + staff), or the legacy `/students` |
| `handshake: FAIL` | network/DNS/TLS | check that the PC can reach `https://apexityedu.com` at all |

Other checks that matter:

* **Attendance** page: rows stuck in `failed`/`review` carry the server's own
  error text in *Last error*.
* **Overview → Recent sync attempts** shows `attempt_type=push_users` once the
  user tables are being published.
* **Device Users** page: anything marked *NOT MAPPED* will reach ApexityEdu
  without a person. Bind it there — **Auto-map matches** resolves every PIN
  whose name matches exactly one person in the EMS directory, or whose PIN
  equals a roll/admission/employee number. Ambiguous names are listed for you
  instead of being guessed. The CSV import on the **People** page does the same
  in bulk.

### EMS permanently OFFLINE
> **`DENIED` / "SERIAL DENIED"?** The server is reachable but refuses the
> terminal's serial. Follow [`FIX_DENIED_PROMPT.md`](FIX_DENIED_PROMPT.md)
> (quick checks + a copy-paste prompt for the ApexityEdu server side).

1. **Settings → Test EMS connection** runs the full probe (iClock + REST).
2. If `iClock endpoint: no` and `REST bridge API: no` — the EMS has no
   attendance ingestion yet. The fix is server-side; see
   `docs/EMS_API_CONTRACT.md`. Collection continues regardless.
3. If reachable but 401/403 — the token is wrong/expired:
   `python -m apexity_bridge set-token`
4. TLS errors on Windows — update Windows root certificates; ensure the EMS
   serves a valid certificate (the bridge refuses cleartext HTTP).

### Probe is OK, but the EMS shows Offline / no punch logs
A probe uses a synthetic serial, so it only proves that an endpoint answers.
The running sync worker now handshakes and polls each registered real serial
approximately every 30 seconds, independently of new punches. Check:
1. The bridge's serial exactly matches the EMS serial and tenant/site.
2. **Overview**: read the EMS error text, not just ONLINE. A working handshake
   and a failed presence poll can coexist; uploads are still attempted.
3. **Attendance** and **Recent sync attempts**: an upload must return HTTP 200
   and exactly `OK` or `OK: <accepted count>`. Unexpected replies now remain
   failed/retryable, with an error in the application log and attendance row.
4. `401/403`: verify the EMS's credential contract. A device API key shown in
   the EMS UI is not necessarily the shared-token extension this bridge uses.
   Do not rotate keys blindly or publish them in a screenshot.
5. `getrequest` fails: ask the EMS team to check that route and its `last_seen`
   update logic. Read-only `DATA QUERY` commands are answered and acknowledged
   via `/iclock/devicecmd`; destructive commands are refused (audited once as
   `ems.command_refused`) and acknowledged with a non-zero Return. **Sync users**
   in the EMS does not cause the bridge to apply device administration commands.
   See [`SETTINGS_AND_SYNC_TROUBLESHOOTING.md`](SETTINGS_AND_SYNC_TROUBLESHOOTING.md)
   §6 for the decision table.

After installing an updated build, fully exit/restart the old bridge process
(including its tray/background instance). Create one new test punch and allow
at least one collection/health cycle, then check both the bridge and EMS.
This source fix does not update an already installed Windows EXE automatically;
rebuild with `powershell -ExecutionPolicy Bypass -File packaging\build.ps1`
on Windows or install a build containing these changes.

**Previously synced records:** this fix deliberately does not reset/replay old
`synced` rows. Older builds may have falsely marked a batch synced after an
unexpected HTTP 200 reply. Back up the local database and reconcile the affected
serial/time range against the EMS before arranging a targeted replay; confirm
EMS deduplication first. **Retry selected** only retries non-synced rows, not
these historical rows. Do not delete the database or clear the K40 logs.

### Events stuck in `failed` / `review`
- `failed` rows retry automatically with exponential backoff — give it time.
- `review`/`rejected` rows need a human: **Attendance → Retry selected** after
  fixing the cause (bad timestamp, EMS-side rejection reason shown in the row).
- The **Logs** page shows the per-batch error the EMS returned.

### People page says the roster API is missing
The EMS doesn't implement `GET /api/v1/bridge/people` (or the legacy
`/students`) yet. Attendance keeps working; only *mapping* needs the roster.
The directory holds **students, teachers and staff** — everyone who punches.
Options: implement the documented endpoint (`docs/EMS_API_CONTRACT.md` §
Option C), or set `EMS_ROSTER_MODE=off` to silence the check. The bridge will
never invent people.

If the page shows students but no teachers/staff, the EMS endpoint is the
legacy `/students` shape: it should return `person_type` per row
(`student` | `teacher` | `staff`) so the school can bind a teacher's PIN too.

### The EMS never receives device users
1. **Device Users → Read from device** — the cache must be filled first (the
   bridge mirrors the terminal's user table every `USERS_SYNC_INTERVAL`
   seconds, default 900).
2. **Device Users → Push users to EMS** performs the push immediately and
   shows the server's exact answer (`pushed` / `refused` / `auth error` /
   `no users read yet`). From the shell:
   `python -m apexity_bridge push-users`.
3. Server-side, ApexityEdu must accept `POST …/iclock/cdata?SN=…&table=USER`
   (iClock) or `POST /api/v1/bridge/devices/users` (REST) — see
   `docs/EMS_API_CONTRACT.md` § Option D. Without it, punches arrive as bare
   PINs and cannot be attributed to anybody.

### "I created people in the desktop app but ApexityEdu has no users/teachers/students"
- The **device user table** (PIN + name) and the **people directory**
  (students/teachers/staff) are two different things:
  * device users → **Device Users → Push users to EMS** (Option D),
  * people → **People → Push people to ApexityEdu** or
    **Add person to ApexityEdu…** (Option E, `POST /api/v1/bridge/people`).
- If the people push says *"this EMS does not implement
  POST /api/v1/bridge/people"*, the server side is missing that endpoint —
  the exact contract is in `docs/EMS_API_CONTRACT.md` § Option E and a
  working implementation is in `docs/reference/ems_reference_api.py`.
- With the iClock protocol the bridge cannot write people at all (iClock
  has no people endpoint); use `EMS_PROTOCOL=rest` or keep
  `EMS_ROSTER_MODE=auto` so the REST side-channel is available.
- Full walkthrough: [`PUSH_AND_SCHEDULER.md`](PUSH_AND_SCHEDULER.md).

---

## Platform problems

### Dashboard won't open (`http://127.0.0.1:8321`)
- Bridge not running: `scripts\Service-Control.ps1 status` → `start`.
- Port conflict: change `DASHBOARD_PORT` in `.env` and restart.
- Check that you see *password-setup* on first run: passwords are set via
  `python -m apexity_bridge set-admin-password` — there are no default
  credentials.
- Page opens but fields stay `…` / buttons do nothing → browser JavaScript
  error, usually a stale v1.2.0 asset. Hard-reload with **Ctrl+F5**, confirm
  `--version` is 1.3.0+, then follow
  [`SETTINGS_AND_SYNC_TROUBLESHOOTING.md`](SETTINGS_AND_SYNC_TROUBLESHOOTING.md) §1.

### Duplicate instance warnings
The single-instance mutex refused a second start. Stop the Task Scheduler task
(and kill stray `pythonw.exe -m apexity_bridge` processes) before starting
manually.

### Database corruption after power cut
On boot the bridge runs `PRAGMA integrity_check`. Corrupt databases are moved
to `bridge.db.corrupt-<timestamp>` **(never deleted)** and a fresh DB is
created. Restore:
1. Stop the bridge.
2. Copy the latest good backup from `%ProgramData%\ApexityEduBridge\backups\`
   to `%ProgramData%\ApexityEduBridge\RESTORE_REQUESTED.db`.
3. Start the bridge — it applies the staged restore and keeps the corrupt file.
Events already synced are safe on the EMS; events only in the corrupt file can
be re-read from the device itself (logs are never deleted there).

### Logs / diagnostics
- Live log: `%ProgramData%\ApexityEduBridge\logs\bridge.log` (rotating, 5×5 MB).
- Bundle: **Settings → Export diagnostics bundle** → zip with redacted config,
  stats, logs and a DB snapshot.

### The bridge does not start after a reboot
- Check the scheduled task: `scripts\Service-Control.ps1 status` (or
  **Settings → Auto-start & scheduled sync → Status** in the desktop app).
  It must show `ApexityEduBridge: Running`.
- If it is *not installed*, register it again:
  `python -m apexity_bridge service install` (or the **Install auto-start**
  button).
- The task runs as the signed-in user with least privileges; a "task has not
  run yet" state usually means the machine was never rebooted after
  installation — start it once with `service start`.
- The always-on collector and the optional scheduled sync are two separate
  tasks; see [`PUSH_AND_SCHEDULER.md`](PUSH_AND_SCHEDULER.md) § 5.

---

## ZKTime 5.0 coexistence
ZKTime 5.0 polls the same terminals via its own service. Running both is
possible but avoids surprises:
- Prefer giving the bridge exclusive 4370 access during its collect window
  (ZK comm is short-lived; both typically coexist fine).
- Never enable ZKTime's "clear logs after download" — the bridge must be the
  only actor that decides about attendance lifecycle (and it never deletes).
