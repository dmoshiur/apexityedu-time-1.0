# Manual push & scheduler — how the people, teachers and students reach ApexityEdu

> **The short version (বাংলায়):**
> মেশিনে (K40) যে দুইজন user বানিয়েছেন, সেটা ডেস্কটপ সফটে দেখা যাচ্ছে, কিন্তু
> ApexityEdu-তে ঢুকছে না — এটা এখন আর "queued" হয়ে থাকে না। **Device Users →
> "Push users to EMS"** বাটনে এখন সরাসরি push হয় এবং সার্ভারের ঠিক উত্তরটা
> দেখায় (কতজন accept হলো / কীসে মানা করলো)। আর **People → "Push people to
> ApexityEdu"** দিয়ে student/teacher/staff ডিরেক্টরি সার্ভারে পাঠানো যায়,
> আর **"Add person to ApexityEdu…"** দিয়ে একজন মানুষ হাতে গেলেই তৈরি করা যায়।
> সিডিউলার (auto-start + scheduled sync) যোগ করার নিয়ম এই ডকুমেন্টের শেষে।

---

## 1. Why did "created on the machine → not visible in ApexityEdu" happen?

Three different things can be meant by "push", and each has its own button,
its own payload and its own server-side requirement:

| What you want to push | Button | Wire format | Server must support |
|---|---|---|---|
| The **users enrolled on the terminal** (PIN + name) | Device Users → **Push users to EMS** | iClock `table=USER` or `POST /api/v1/bridge/devices/users` | Option D |
| The **people directory** (students, teachers, staff) | People → **Push people to ApexityEdu** | `POST /api/v1/bridge/people` | Option E |
| **One new person** (a student / teacher / staff member) | People → **Add person to ApexityEdu…** | `POST /api/v1/bridge/people` | Option E |
| **Attendance** (the punches themselves) | Attendance → **Retry selected**, or Settings → **Sync now** | iClock `table=ATTLOG` or `POST /api/v1/bridge/attendance/batch` | Option A / B |

The old "Publish to ApexityEdu" button only *queued* the work for the
background worker, and the background worker only pushes while the EMS is
marked **online**. If the handshake, the serial override or the token was
not right, the button looked like it had worked and nothing arrived. The
button now performs the push immediately and shows the server's exact
answer — accepted count, refusal, or the missing endpoint.

---

## 2. Device Users → "Push users to EMS"

What it sends: the user table the bridge mirrored from the terminal
(`PIN`, `name`, `privilege`, `card`). **Never** fingerprints, never
passwords, never templates.

Order of operations (do not skip step 1):

1. **Read from device** — the terminal is the source of truth. Without a
   cached table there is nothing to publish and the button says exactly
   that ("no users read yet").
2. **Push users to EMS** — the bridge uploads the table now and reports:
   * `pushed` — the EMS answered `OK` / `OK: <n>`; the accepted count is
     shown,
   * `refused` — the server answered something else (HTML login page, JSON,
     empty body). The exact reply is shown,
   * `auth error` — the token/serial was rejected,
   * `no users read yet` — go back to step 1.
3. If the device's **EMS serial** differs from the hardware serial, set it
   on the **Devices** page (*EMS serial…*) — the bridge pushes under the
   serial ApexityEdu knows.

Command line equivalent (works while the app is closed):

```powershell
.\.venv\Scripts\python.exe -m apexity_bridge push-users            # every device
.\.venv\Scripts\python.exe -m apexity_bridge push-users --sn X9C12345
```

## 3. People → "Push people to ApexityEdu" / "Add person…"

The directory is downloaded from the EMS (`GET /api/v1/bridge/people`) —
that direction has always worked. The **write** direction is new and it is
*optional on the server*:

* If ApexityEdu implements `POST /api/v1/bridge/people`, the button
  publishes every cached student, teacher and staff member (or only the
  type selected in the filter) and reports how many were accepted.
* If it does not, the bridge says so in plain words — *"this EMS does not
  implement POST /api/v1/bridge/people (HTTP 404)"* — instead of silently
  doing nothing. The exact server-side contract (request shape, upsert
  rule, reply shape) is in
  [`EMS_API_CONTRACT.md` § Option E](EMS_API_CONTRACT.md#option-e--writing-people-to-the-ems-students-teachers-and-staff),
  and a working implementation is in
  [`reference/ems_reference_api.py`](reference/ems_reference_api.py).

"Add person to ApexityEdu…" creates a single person (id, name, type and
the type-specific fields) and then caches it locally, so the People page
shows it immediately.

Command line equivalent:

```powershell
.\.venv\Scripts\python.exe -m apexity_bridge push-people
.\.venv\Scripts\python.exe -m apexity_bridge push-people --type teacher
.\.venv\Scripts\python.exe -m apexity_bridge push-people --id S1001
```

> **Note on iClock.** The iClock/ADMS protocol has no people endpoint — it
> only carries device tables and punches. People are written over the REST
> side-channel (`EMS_PROTOCOL=rest` or `EMS_ROSTER_MODE=auto`). The bridge
> tells you which one it is using instead of guessing.

## 4. Settings → "Sync now"

One manual pass over everything: people directory (download) → device user
tables (upload) → pending attendance (upload). It is the same code path the
scheduled task runs, so if "Sync now" works, the scheduler will work.

```powershell
.\.venv\Scripts\python.exe -m apexity_bridge sync-now
```

---

## 5. Adding the scheduler (Windows Task Scheduler)

The bridge needs two different things from the scheduler:

| Task | What it does | When to use it |
|---|---|---|
| **ApexityEduBridge** (auto-start) | Starts the bridge with Windows and restarts it after a crash. The bridge then collects punches and syncs continuously. | Always. |
| **ApexityEduBridge-Sync** (scheduled sync) | Runs `sync-now` every 15 minutes: people → device users → attendance. | When the desktop app is closed between shifts, or as a safety net. |

### 5.1 From the desktop app

**Settings → Auto-start & scheduled sync**:

* **Install auto-start** — registers `ApexityEduBridge` (at logon + at
  boot, restart-on-failure every minute, least privilege, hidden).
* **Start / Stop / Status** — control the running task.
* **Uninstall auto-start** — removes the task; **your data is kept**.
* **Add scheduled sync** — registers `ApexityEduBridge-Sync` (every 15
  minutes).
* **Remove scheduled sync** — removes that task.
* **Sync now** — run the same pass once, right now.

### 5.2 From PowerShell

```powershell
# auto-start (always-on collector)
.\.venv\Scripts\python.exe -m apexity_bridge service install
.\.venv\Scripts\python.exe -m apexity_bridge service start
.\.venv\Scripts\python.exe -m apexity_bridge service status

# scheduled sync (every 15 minutes)
.\.venv\Scripts\python.exe -m apexity_bridge service install-sync
.\.venv\Scripts\python.exe -m apexity_bridge service uninstall-sync

# remove everything (data preserved)
.\.venv\Scripts\python.exe -m apexity_bridge service uninstall
```

or with the helper script:

```powershell
.\scripts\Service-Control.ps1 status
```

### 5.3 By hand (Task Scheduler UI)

1. Open **Task Scheduler** → **Create Task** (not "Create Basic Task").
2. **General**: name `ApexityEduBridge`, "Run whether user is logged on or
   not", "Run with highest privileges" is *not* required.
3. **Triggers** → **New** → *At log on* (and *At startup* if you want it
   running before anyone signs in).
4. **Actions** → **New** → *Start a program*:
   * Program: `C:\Program Files\ApexityEdu\Attendance Bridge\ApexityEduBridge.exe`
     (source install: `…\.venv\Scripts\pythonw.exe`)
   * Arguments: `tray --hidden` (source install: `-m apexity_bridge run`)
   * Start in: the folder of the executable.
5. **Settings** → *Restart the task if it fails* (1 minute, unlimited) and
   *If the task is already running → Do not start a new instance*.
6. For the **scheduled sync** task add a second task with
   *Trigger → Daily → Repeat every 15 minutes, indefinitely* and the
   action `…\python.exe -m apexity_bridge sync-now`.

### 5.4 Check that it works

```powershell
.\scripts\Service-Control.ps1 status      # both tasks + dashboard liveness
Get-Content "$env:ProgramData\ApexityEduBridge\logs\bridge.log" -Tail 40
```

`ApexityEduBridge-Sync` writes its own output to the Windows Task
Scheduler history (Task Scheduler → the task → **History** tab).

---

## 6. Still nothing in ApexityEdu? The 60-second checklist

1. **Settings → Verify what the EMS accepts** (or
   `python -m apexity_bridge verify-ems`) — it asks the live server what
   it really accepts, per serial. A green "EMS online" badge proves
   nothing.
2. **Is the serial the same on both sides?** The bridge pushes under
   *EMS serial* (Devices page) — it must match the serial in ApexityEdu →
   Biometric Devices.
3. **Did you read the users from the device first?** Empty cache → nothing
   to publish.
4. **Is the token configured?** Settings → Token (stored in Windows
   Credential Manager). `401/403` means the token or the serial was
   rejected.
5. **Does the server implement the endpoint you need?**
   * device users → Option D,
   * people → Option E.
   The reference implementation in `docs/reference/ems_reference_api.py`
   has both, so you can run it locally and compare.
