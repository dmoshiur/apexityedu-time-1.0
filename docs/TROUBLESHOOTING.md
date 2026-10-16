# Troubleshooting

First stop: **Dashboard → Logs** and `…\ApexityEduBridge\logs\bridge.log`.
Generate a support bundle from **Settings → Export diagnostics bundle** (all
secrets are redacted) before contacting support.

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

### EMS permanently OFFLINE
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
   update logic. Polling commands are only logged, not executed; **Sync users**
   in the EMS does not cause the bridge to apply device administration commands.

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

### Students page says the roster API is missing
The EMS doesn't implement `GET /api/v1/bridge/students` yet. Attendance keeps
working; mapping requires the roster. Options: implement the documented
endpoint (`docs/EMS_API_CONTRACT.md`) or select `EMS_PROTOCOL=rest` once the
EMS team ships it. The bridge will never invent student data.

---

## Platform problems

### Dashboard won't open (`http://127.0.0.1:8321`)
- Bridge not running: `scripts\Service-Control.ps1 status` → `start`.
- Port conflict: change `DASHBOARD_PORT` in `.env` and restart.
- Check that you see *password-setup* on first run: passwords are set via
  `python -m apexity_bridge set-admin-password` — there are no default
  credentials.

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

---

## ZKTime 5.0 coexistence
ZKTime 5.0 polls the same terminals via its own service. Running both is
possible but avoids surprises:
- Prefer giving the bridge exclusive 4370 access during its collect window
  (ZK comm is short-lived; both typically coexist fine).
- Never enable ZKTime's "clear logs after download" — the bridge must be the
  only actor that decides about attendance lifecycle (and it never deletes).
