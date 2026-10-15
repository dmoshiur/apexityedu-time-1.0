# Windows deployment guide (Windows 10/11)

Target layout after install (defaults):

```
E:\Software Run\ZKTeco\apexityedu-bridge\        ← the app (this repository)
    apexity_bridge\                              ← Python package
    scripts\                                     ← installer/service scripts
    .env                                         ← your configuration
    .venv\                                       ← isolated Python (created)
C:\ProgramData\ApexityEduBridge\                 ← data (created)
    bridge.db                                    ← SQLite (WAL)
    logs\bridge.log                              ← rotating logs
    backups\                                     ← db backups + diagnostics zips
```

## Prerequisites
- Windows 10/11 64-bit, Python 3.10+ from python.org (tick *Add python.exe to
  PATH*), or the Microsoft Store build.
- The PC and the K40(s) on the same LAN (or routed VLANs added to
  `DISCOVERY_EXTRA_SUBNETS`).
- Outbound HTTPS (443) to `apexityedu.com`.

## Install — 5 minutes

```powershell
cd "E:\Software Run\ZKTeco"            # put the repository here
git clone <repo-url> apexityedu-bridge # or copy the folder
cd apexityedu-bridge
powershell -ExecutionPolicy Bypass -File scripts\Install-ApexityBridge.ps1
```

The installer: creates `.venv`, installs `requirements.txt`, creates the data
dir with ACLs, copies `.env.example → .env`, initialises SQLite, asks you for
the EMS device token (stored in **Windows Credential Manager**), sets the
dashboard admin password (PBKDF2 hash), runs the EMS connectivity probe,
registers the auto-start task and starts the bridge.

Non-interactive installation:
```powershell
.\scripts\Install-ApexityBridge.ps1 -SkipInteractive -EmsBaseUrl "https://apexityedu.com"
```

Then open **http://127.0.0.1:8321** and:
1. **Devices → Scan LAN now** → register your K40 (or *Manual registration*).
2. **Students → Refresh from EMS** → map biometric user IDs to students.
3. **Settings → Test EMS connection** → expect health OK.

## Run control

```powershell
.\scripts\Service-Control.ps1 start|stop|restart|status
```

Auto-start is a Task Scheduler task `ApexityEduBridge`: starts at logon/boot,
restarts on failure every minute (999 attempts), refuses duplicates (both
Task Scheduler policy and a process mutex).

Crash recovery beyond that is inherent: all state lives in SQLite, so a killed
process loses nothing; the task restarts it and sync resumes from the queue.

## Firewall / network notes
- Dashboard binds **127.0.0.1 only**. Exposing it on the LAN requires setting
  `DASHBOARD_ALLOW_LAN=1` + `DASHBOARD_HOST=0.0.0.0` + a **Windows firewall rule
  for inbound TCP 8321 restricted to admin IPs only** — and is discouraged.
- Outbound to the K40: TCP/UDP 4370 on the LAN.
- Outbound to EMS: TCP 443 only. Nothing inbound from the internet.

## Update to a new version

```powershell
.\scripts\Service-Control.ps1 stop
git pull                                   # or replace the folder contents
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m apexity_bridge init-db   # applies migrations
.\scripts\Service-Control.ps1 start
```

Migrations are forward-only and idempotent; data is always preserved.

## Uninstall

```powershell
powershell -ExecutionPolicy Bypass -File scripts\Uninstall-ApexityBridge.ps1
```
Keeps all data and writes a final backup. Add `-DeleteData` to also remove
`C:\ProgramData\ApexityEduBridge`, `-RemoveVenv` to remove `.venv`. The EMS
token is removed from Credential Manager either way.

## Alternative: native Windows service (advanced)

Task Scheduler is the default because it's dependency-free. If you prefer a
real service (pywin32):

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-windows.txt
.\.venv\Scripts\python.exe -m apexity_bridge.windows_service.service install --startup=auto
.\.venv\Scripts\python.exe -m apexity_bridge.windows_service.service start
```
