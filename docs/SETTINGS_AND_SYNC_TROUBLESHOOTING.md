# Settings page + sync failure — step-by-step recovery guide

**Applies to:** ApexityEdu Attendance Bridge v1.3.0 and later (Windows 10/11).
**Symptoms covered:** the browser **Settings** page opens but stays frozen /
the buttons do nothing, **`ems.command.refused`** (audit action
`ems.command_refused`) repeats in the log, and a **Celery/sync error** appears
on the ApexityEdu backend while attendance does not arrive.

> **বাংলা সারাংশ (quick summary)**
> ১. প্রথমে **Backup database now** চাপুন (বা `backup` কমান্ড) — কোনো ফাইল
> ডিলিট করবেন না, K40-এর লগও ক্লিয়ার করবেন না। ২. `doctor` চালান — ডাটাবেস,
> টোকেন, EMS ও কমান্ডের অবস্থা একসাথে দেখাবে। ৩. Settings না খুললে ব্রাউজারে
> **Ctrl+F5** দিন; পুরোনো v1.2.0-এ `app.js`-এ syntax error ছিল, v1.3.0-এ ঠিক
> করা হয়েছে। ৪. `ems.command_refused` মানে EMS এমন কমান্ড পাঠিয়েছে যা bridge
> কখনো চালায় না (CLEAR/REBOOT/SET) — এটি crash নয়, নিরাপত্তার নিয়ম। নতুন
> ভার্সন একবার audit করে `devicecmd`-এ ব্যর্থতা জানায়; EMS-পক্ষকে কমান্ডটি
> বাতিল করতে বলুন। ৫. Celery হলো **ApexityEdu সার্ভারের** অংশ, bridge-এর নয়;
> নিচের §6 দেখে সার্ভার লগ মিলিয়ে দেখুন। ৬. টোকেন কখনো chat, log বা
> screenshot-এ দেবেন না।

---

## 0. The two-minute triage

Run these on the school PC, **in this order**. Stop at the first FAIL.

```powershell
cd "C:\Program Files\ApexityEdu\Attendance Bridge"   # or your source checkout
.\ApexityEduBridge.exe --version                     # installed build
python -m apexity_bridge doctor                      # source install; add --json for support
python -m apexity_bridge verify-ems                  # live proof, posts only an empty ATTLOG
python -m apexity_bridge sync-now                    # roster → users → attendance
```

For an installed EXE, the CLI commands run the same binary:

```powershell
& "C:\Program Files\ApexityEdu\Attendance Bridge\ApexityEduBridge.exe" doctor
& "C:\Program Files\ApexityEdu\Attendance Bridge\ApexityEduBridge.exe" verify-ems
```

The dashboard has the same check: **Settings → Backend self-check → Run
self-check**. It prints OK/WARN/FAIL per layer and an exact fix line.

| Layer | What `doctor` proves | First fix if it fails |
|---|---|---|
| SQLite | file exists, integrity, schema v3, WAL, writable, counts | §2 |
| Token | Credential Manager readable, token present | §3 |
| Config | URL/protocol/intervals valid | §4 |
| EMS | DNS/TLS reachability, handshake for **each real serial** | §5 |
| Commands | read-only vs refused, ack status | §6 |
| Logs | recent ERROR/Traceback/Celery lines | §6–7 |

`doctor` is strictly read-only: it does **not** POST attendance, does **not**
execute a device command and does **not** print the token.

---

## 1. Settings page does not load / controls do nothing

### 1.1 Confirm the page and the service

```powershell
Get-Service ApexityEduBridge -ErrorAction SilentlyContinue
Get-NetTCPConnection -LocalPort 8321 -State Listen -ErrorAction SilentlyContinue
Invoke-WebRequest http://127.0.0.1:8321/settings -UseBasicParsing | Select-Object StatusCode
```

* **Connection refused** → the bridge is not running. `scripts\Service-Control.ps1 start`.
* **HTTP 302 to /login** → you are not signed in; sign in first.
* **HTTP 500** → open `%ProgramData%\ApexityEduBridge\logs\bridge.log` and go to §1.3.
* **HTTP 200 but fields show `…` and buttons are dead** → JavaScript did not
  run. This is exactly the v1.2.0 bug: `app.js` contained a duplicated
  `if (page === "settings") {` block and one missing brace, so the browser
  rejected the whole file (`Uncaught SyntaxError: Unexpected token ')'`).

### 1.2 Clear the stale asset (30 seconds)

1. Press **Ctrl+F5** (hard reload) on `http://127.0.0.1:8321/settings`.
2. Open DevTools → **Console** and **Network**. A 200 for `static/app.js` and
   no red syntax error means the asset is healthy.
3. If the console still reports a syntax error, the old build is still
   installed. Exit the tray app completely (tray → Exit), stop the service,
   install v1.3.0 or later, then start again. `--version` must show `1.3.0`.

### 1.3 Settings API returns 400/500

Save failures in v1.2.0 had two concrete causes:

| API response | Cause | Fix in v1.3.0 |
|---|---|---|
| `400 ems_protocol must be auto, iclock or rest` | the form's **“(from environment)”** option is the empty string, which the old validator rejected | empty now means “delete the DB override and use `.env`” |
| `500` on `GET /api/settings` | `has_ems_token()` raised when the credential store was locked/unavailable | read errors are caught, reported as `token_error`, page still renders |
| Save says “no changes” | values equal to the DB/env value | expected |

Reproduce the API by hand (after signing in, copy the `csrf-token` meta value):

```powershell
$session = New-Object Microsoft.PowerShell.Commands.WebRequestSession
Invoke-WebRequest http://127.0.0.1:8321/login -WebSession $session -Method Post `
  -Body @{username="admin"; password="<your-password>"}
$page = Invoke-WebRequest http://127.0.0.1:8321/settings -WebSession $session
$csrf = [regex]::Match($page.Content, 'name="csrf-token" content="([a-f0-9]+)"').Groups[1].Value
Invoke-RestMethod http://127.0.0.1:8321/api/settings -WebSession $session `
  -Headers @{"X-CSRF-Token"=$csrf} | ConvertTo-Json -Depth 6
```

A healthy response contains `settings`, `effective`, `token_configured`,
`ems_commands` (`commands_seen`, `commands_refused`), and no `token_error`.

### 1.4 Still blank? Look at the server, not the browser

If `GET /settings` itself returns a template error, the failure is before
JavaScript. Check:

* Log lines around the request (`apexity_bridge.dashboard`).
* Disk full / read-only `%ProgramData%` (see §2.4).
* A corrupted `application_settings` row (rare): restore the latest backup.

---

## 2. Database layer

### 2.1 Where it lives

| Install | Database | Backups | Logs |
|---|---|---|---|
| Windows installer | `%ProgramData%\ApexityEduBridge\bridge.db` | `…\backups\` | `…\logs\bridge.log` |
| Source checkout | `~/.apexity-bridge/bridge.db` (or `BRIDGE_DB_PATH`) | `~/.apexity-bridge/backups/` | `~/.apexity-bridge/logs/` |

`BRIDGE_DATA_DIR` overrides the whole folder. Empty/missing files are created
automatically with migrations v1→v3.

### 2.2 Take a backup **before** touching anything

```powershell
python -m apexity_bridge backup
# or: Settings → Backup database now
```

Backups use SQLite's online backup API, so they are consistent even while the
service runs. Copy the newest `bridge-backup-*.db` somewhere safe before any
repair.

### 2.3 Integrity, schema and health queries

```powershell
python -m apexity_bridge init-db      # migrate + integrity_check(1)
python -m apexity_bridge doctor --no-network
```

Read-only inspection with the Python that ships with a source install:

```powershell
$db = "$env:ProgramData\ApexityEduBridge\bridge.db"
python -c "import sqlite3; c=sqlite3.connect(r'$db'); print(c.execute('pragma integrity_check').fetchone()); print(c.execute('select version from schema_migrations order by version').fetchall()); print(c.execute('select sync_status,count(*) from attendance_events group by 1').fetchall()); print(c.execute('select status,count(*) from sync_queue group by 1').fetchall())"
```

Expected: `('ok',)`, `[(1,), (2,), (3,)]`, pending rows only during outages,
`sync_queue` mostly `done`.

### 2.4 Common database failures

| Symptom | Meaning | Fix |
|---|---|---|
| `database file is locked` / `database is locked` | another process (second bridge instance, antivirus, ZKTime backup) holds a write lock | stop the second instance; add an antivirus exclusion for the data folder; never put `bridge.db` on a network share or OneDrive |
| `database disk image is malformed` | corruption (power cut, disk fault) | the bridge quarantines it as `bridge.db.corrupt-<timestamp>` and creates a fresh DB. Restore the newest good backup (§2.5) |
| `attempt to write a readonly database` | ACL/read-only folder or full disk | run as the installing user; grant Modify on `%ProgramData%\ApexityEduBridge`; free disk space |
| schema version `< 3` | migration never completed | stop the service, backup the file, start again; migration is forward-only and transactional |
| `database or disk is full` | no free space | free space; the bridge never deletes attendance to make room |
| `no such table: …` | wrong `BRIDGE_DB_PATH` or a foreign DB file | point `BRIDGE_DB_PATH` at the real file; restore a backup |

### 2.5 Restore a backup (data-preserving)

1. Exit the tray app and stop the service:
   `.\scripts\Service-Control.ps1 stop`
2. Copy the chosen backup to `%ProgramData%\ApexityEduBridge\RESTORE_REQUESTED.db`.
3. Start the bridge. It moves the current DB to
   `backups\bridge-pre-restore-<timestamp>.db` (never deletes it) and installs
   the staged file.
4. Run `doctor` and compare `database.events` counts with the backup.
5. Events that exist only on the K40 can be re-read: the bridge never deletes
   device logs. Do **not** use ZKTime's “clear after download”.

### 2.6 What “failed” and “review” mean

* `pending`/`failed` — the event has not been accepted by the EMS. `failed`
  retries with exponential backoff; `review` has exceeded retry limits.
* `rejected` — the EMS explicitly refused the record; it will not retry. Read
  **Last error**, fix the cause (timestamp/serial/mapping), then **Retry
  selected**.
* Never mark attendance manually in SQLite unless you have a backup and know
  the EMS already has the record. The dedup hash is the safety net.

---

## 3. Device token layer

The token is **never** stored in `.env`, SQLite, logs or the diagnostics
bundle. On Windows it lives in **Credential Manager** under the service
`apexityedu-bridge/ems-device-token`, user `apexity-bridge`.

### 3.1 Set, replace or remove it

```powershell
python -m apexity_bridge set-token        # hidden prompt
python -m apexity_bridge delete-token
```

Or **Settings → EMS device token → Save token**. An empty value clears it.
The UI only ever shows “configured / not configured”, never the value.

### 3.2 Diagnose a token problem

| Check | Command | Expected |
|---|---|---|
| Credential store readable | `doctor` → `secrets.token` | `OK` (value hidden) |
| Token accepted by REST | `verify-ems` (REST protocol) | no `401/403` |
| Token accepted by iClock | `verify-ems` | no `401/403`; serial handshake OK |
| Token redacted from errors | inspect the log | `[REDACTED]`, never the raw value |

`401`/`403` can mean **three different things**. Do not rotate the token until
you know which one:

1. **Token wrong/revoked** → the EMS admin reissues it; save it again.
2. **Serial not registered** → iClock rejects the serial, not the token. Set
   **Devices → EMS serial…** to the exact serial in *ApexityEdu → Biometric
   Devices*.
3. **Credential-store account mismatch** → the desktop app runs as the signed-in
   user, but a service/scheduled task runs as another account (or SYSTEM).
   Credential Manager is **per user**; save the token while running as the same
   account that hosts the always-on task.

### 3.3 Credential Manager looks empty

* Open **Control Panel → Credential Manager → Windows Credentials** and search
  for `apexity`.
* `cmdkey /list` may not display generic credentials — this is normal.
* If the keyring backend is unavailable, the bridge logs a loud warning and
  falls back to a `0600` file under the data dir **for development only**.
  Production must show `OS credential store (…)` in `doctor`.
* Locked vault / EDR blocking Credential Manager → unlock the session, allow
  the app, or run `doctor` interactively; read failures no longer take down the
  Settings page (v1.3.0); v1.3.1 reports the exact read error via `token_error` / `doctor`.

---

## 4. Configuration layer

`doctor` validates the effective configuration. Typical mistakes:

| Setting | Valid | Invalid symptom |
|---|---|---|
| `EMS_BASE_URL` | `https://apexityedu.com` | `http://…` outside localhost is refused by design |
| `EMS_PROTOCOL` | `auto`, `iclock`, `rest`, or empty (use env) | Settings 400 in old builds |
| `EMS_ICLOCK_PATH` | `/iclock` | handshake 404 |
| `collect_interval` | ≥ 5 s (default 30) | too-low values are rejected |
| `scan_interval` / `roster_interval` / `users_sync_interval` | ≥ 60 s | rejected |
| `DASHBOARD_HOST/PORT` | `127.0.0.1:8321` | port conflict → dashboard never starts |

Changing settings from the dashboard calls `apply_settings_overrides()`: the
EMS client is rebuilt, presence cache cleared, discovery re-triggered. If the
new values look wrong, remove the DB override (choose “(from environment)” on
the form) and restart.

---

## 5. Backend communication layer (bridge → ApexityEdu)

### 5.1 Network first

```powershell
Resolve-DnsName apexityedu.com
Test-NetConnection apexityedu.com -Port 443
Invoke-WebRequest "https://apexityedu.com/iclock/cdata?SN=APEXITY-PROBE&options=all" -UseBasicParsing
w32tm /resync
```

A healthy iClock handshake starts with `GET OPTION FROM:`. A `DENIED` body
means the **serial is not authorized** (register it, or fix the EMS serial).
`doctor` reports this as a handshake failure with the exact serial.

TLS/proxy checklist: Windows root certificates updated, no TLS-inspecting
proxy breaking `requests`, `HTTPS_PROXY`/`HTTP_PROXY` intentional, system
clock within a few minutes.

### 5.2 Serial register (the most common cause)

The bridge identifies a terminal by its **hardware serial** but uploads under
the **EMS serial**. They can differ.

1. In ApexityEdu → **Biometric Devices**, copy the exact serial.
2. In the bridge → **Devices → EMS serial…**, paste it. This clears the
   cached user-table hash and rebuilds the client.
3. `verify-ems` must show that serial accepted for an **empty ATTLOG POST**
   (`OK` or `OK: 0`). An empty POST never writes a fake punch.

### 5.3 What must be true server-side

From [`docs/EMS_API_CONTRACT.md`](EMS_API_CONTRACT.md):

* `POST /iclock/cdata?SN=<serial>&table=ATTLOG` → `200 OK` / `OK: <n>`.
* dedup on `SN + PIN + timestamp + status`.
* `POST /iclock/cdata?SN=<serial>&table=USER` for PIN → person.
* optional `GET /api/v1/bridge/people` for the roster and
  `POST /api/v1/bridge/people` for people writes.

HTML, JSON, login redirects and empty bodies are **not** acceptance. The
bridge keeps those records queued and retries.

---

## 6. `ems.command_refused` — what it means and how to stop the noise

### 6.1 Meaning

`getrequest` returned a `C:<id>:<COMMAND>` instruction. The bridge classifies
it:

| Command class | Examples | Bridge behaviour |
|---|---|---|
| Read-only data query | `DATA QUERY USERINFO`, `DATA QUERY ATTLOG` | answers it (uploads the user table / replays punches) and acks `Return=0` |
| Harmless control | `CHECK`, `INFO`, `OPTIONS` | acks `Return=0` |
| Destructive / admin | `REBOOT`, `CLEAR DATA`, `DELETE USER`, `SET TIME`, `ENROLL`, `UPDATE`, `FORMAT` | **never executed**; audited once as `ems.command_refused`; acked `Return=-1` |
| Biometric template | `DATA QUERY BIODATA`, `DATA QUERY FINGERTMP` | never read; audited as `ems.command_unsupported`; acked `Return=-1004` |

This is a **safety guardrail**, not a crash. The Bridge is a collector: it must
never wipe a school's attendance register or reboot a terminal because a remote
queue asked it to.

> **Dot vs underscore.** The bridge writes the local audit action
> `ems.command_refused` (underscore). A dotted **`ems.command.refused`** line
> in the ApexityEdu web/Celery log is the *server's* event for the same queued
> command — it means the server tried to dispatch a command the terminal
> (the bridge) will not execute. Match it to the bridge audit by timestamp and
> serial; the fix is on the server side (cancel the command / stop re-queueing).

### 6.2 Why the old build repeated the same line every 30 seconds

1. The server sent the command.
2. The bridge refused it but did not report a result to `/iclock/devicecmd`.
3. The server kept the command queued and re-sent it on every heartbeat.
4. The old code wrote an audit row and a warning for every poll.

v1.3.0 fixes the loop in three places:

* one audit row **per command type** (status counts every occurrence),
* an ADMS `POST /iclock/devicecmd` acknowledgement (`Return=-1` for refused,
  `-1004` for templates, `0` only for genuinely answered commands),
* the command state is visible in **Settings** and in `doctor`
  (`commands_refused`, `commands_unsupported`, `commands_acknowledged`,
  `commands_ack_error`).

### 6.3 Decide what to do

| Situation | Action |
|---|---|
| EMS keeps queueing a destructive command | Ask the EMS admin to delete/cancel it. The bridge will never execute it. |
| You do not want the bridge to read any command | **Settings → Answer read-only EMS commands = no** (or `EMS_EXECUTE_COMMANDS=0`), then restart. Presence polling continues; commands are ignored. |
| You need device administration | Use ZKTime/ADMS directly on the terminal, or extend the server contract explicitly. Do not enable destructive execution in the bridge. |
| `commands_ack_error` mentions 404/HTML | The EMS does not implement `/iclock/devicecmd`. The refusal audit still happens once; ask the server team to add the route (ZKTeco ADMS standard). |

Do **not** work around this by deleting `audit_logs` rows. They are the proof of
what the EMS asked for; use the action filter on **Logs**.

---

## 7. Celery / backend sync errors (server side)

### 7.1 Establish the boundary

The bridge has **no Celery dependency**. Celery, Redis/RabbitMQ, periodic
tasks and queue workers belong to the **ApexityEdu web backend**. A Celery
traceback can therefore never be fixed by reinstalling the bridge, and the
bridge cannot inspect that worker from the school PC.

What the bridge *can* prove is the HTTP conversation:

* `doctor` → `ems.probe`, `ems.health`, `ems.serials`
* `verify-ems` → the exact server reply to handshake, presence, empty ATTLOG
  and USER table
* **Overview → Recent sync attempts** and **Attendance → Last error** → the
  server's own error text
* `%ProgramData%\ApexityEduBridge\logs\bridge.log` → request-level errors,
  with tokens redacted

### 7.2 Collect a correlation packet before contacting the backend team

Send **all** of this, never a token or password:

1. UTC timestamp of one failed sync.
2. Bridge version (`--version`), `doctor` output (or `doctor --json`).
3. `verify-ems` output.
4. Serial (both hardware and EMS serial), PIN count, batch line count.
5. HTTP status and response body from the bridge log / sync attempts.
6. The Celery task name, task id, traceback and retry count from the backend
   log at the same timestamp.
7. Whether the EMS's `Biometric Devices` row shows **last seen** updating.

### 7.3 Frequent Celery-side causes

| Backend symptom | Likely cause | Server-side check |
|---|---|---|
| task queued forever, worker idle | broker down / wrong queue / worker not started | `celery -A <project> inspect ping`; `celery -A <project> inspect active`; broker `LLEN` |
| `MaxRetriesExceededError` on the same punch | task retried after a handler bug; queue rows accumulate | inspect `task_retries`, purge only after fixing the handler |
| `OperationalError: database is locked` | long transaction / SQLite/DB contention in the web app | DB locks, connection pool, move ingestion to a short transaction |
| `KeyError`/`ValueError` parsing ATTLOG | body line format, timezone, duplicate key handling | compare a raw line from the bridge body with the parser; see contract § Option A |
| nothing runs after deploy | old worker process still holds stale code | restart Celery workers/beat after deployment |
| `401/403` in the task | token/serial/tenant mismatch or revoked device key | reissue token, fix serial whitelist |
| task succeeds but UI shows nobody | PIN has no person; USER table/mapping missing | publish device users, accept `table=USER`, map PIN → person |
| periodic sync never fires | `beat` not running / schedule disabled | `celery -A <project> inspect scheduled`; beat log |

### 7.4 Bridge-side actions while the backend is being fixed

* Keep collecting — attendance stays safe in SQLite; `pending`/`failed` retry
  with backoff.
* Do **not** “fix” a stuck queue by deleting `bridge.db` or clearing the K40.
* After the server fix: `verify-ems` → `sync-now` → watch **Recent sync
  attempts** until `result=success` and **Ingestion verified** is true.
* If old records may have been falsely marked synced by an older build,
  back up first and reconcile the serial/time range with the EMS before a
  targeted replay (see `docs/TROUBLESHOOTING.md`).

---

## 8. One clean recovery sequence

Use this after any change to the server, token, serial or database.

1. **Stop** the tray app and the service:
   `.\scripts\Service-Control.ps1 stop`
2. **Back up**: `python -m apexity_bridge backup`
3. **Self-check**: `python -m apexity_bridge doctor`
4. **Database**: if integrity/schema fails, follow §2; otherwise continue.
5. **Token**: if `secrets.token` warns/401 occurs, `set-token` under the same
   Windows account that runs the service (§3.2).
6. **Serial**: set **Devices → EMS serial…** to the exact EMS serial (§5.2).
7. **Proof**: `python -m apexity_bridge verify-ems`
   — handshake `OK`, presence `OK`, `attlog_route: OK`, `user_table: OK`.
8. **Push**: `python -m apexity_bridge sync-now` — expect roster/users/attendance
   counts and no error lines.
9. **Start**: `.\scripts\Service-Control.ps1 start`
10. **Real punch test**: punch once on the K40, wait one collection cycle,
    confirm the row in **Attendance** moves to `synced` and appears in
    ApexityEdu. Check **Ingestion verified** on Overview.
11. **Only then** close the incident. Export a diagnostics bundle
    (Settings → Export diagnostics) and attach it to the ticket.

### Acceptance checklist

- [ ] `--version` shows v1.3.0 or later.
- [ ] Settings page renders, values load, Save reports a concrete result.
- [ ] `doctor` has no FAIL; warnings are understood.
- [ ] `verify-ems` accepts every registered serial (empty ATTLOG included).
- [ ] `sync-now` reports zero errors.
- [ ] A new punch reaches ApexityEdu within one cycle.
- [ ] `ems.command_refused` no longer repeats every 30 seconds; if present, a
      single row with a count explains the server's command.
- [ ] Backend Celery task for the same serial/timestamp completes; no retry
      storm and no 5xx/timeout in the bridge log.

---

## 9. Do not do these

* Do not delete `bridge.db`, `audit_logs` or `sync_queue` rows to “reset” sync.
  Back up and fix the cause; synced events are not replayed automatically.
* Do not clear the K40 attendance log. The device is the last-resort backup.
* Do not put the device token in `.env`, chat, email, screenshots or a ticket.
* Do not switch the EMS URL to `http://` to “test faster”; the bridge refuses
  cleartext outside localhost for a reason.
* Do not let the bridge execute `CLEAR`/`REBOOT`/`SET` commands to silence a
  queue. Refusing them is correct behaviour.
* Do not rotate every token/serial at once; change one variable, re-run
  `doctor`, and record the result.
