# ApexityEdu Smart Attendance Bridge

Connects ZKTeco **K40** fingerprint terminals on the school LAN to the
**ApexityEdu EMS** (`https://apexityedu.com`) — reliably, offline-first, and
without depending on ZKTime 5.0.

- **Automatic LAN discovery** of K40 terminals with verified-identity IP
  repair (serial number first — never by IP alone).
- **Direct device communication** via the maintained `pyzk` ZK-protocol
  library (TCP/UDP 4370), strictly **read-only**: device logs are never
  deleted and fingerprint templates are never touched.
- **Offline-first sync**: every punch is persisted to local SQLite before any
  internet call, pushed to the EMS with idempotency keys and exponential
  backoff, and resumes exactly where it stopped after restarts/outages.
- **Student directory + mapping UI** driven by the real EMS roster
  (`device user → EMS student`), with conflict detection and validated CSV
  bulk import/export.
- **Native Windows desktop app** (no browser): Overview, Devices, Students,
  Attendance, Logs, Settings — sharp 90° cards, system tray, same process as
  the background service.
- **Always-on background service** with Task Scheduler auto-start at boot:
  reconnects K40s and resumes the offline sync queue after a PC restart.

> **Honest status.** The bridge software is complete and covered by 127
> automated tests. A branded Windows installer produces `ApexityEduBridge.exe`
> with the product logo (Start Menu, Desktop, tray). Two things can only be finished on site, and the software
> will tell you plainly instead of guessing: (1) what protocol
> `apexityedu.com` actually accepts for attendance — run
> `python -m apexity_bridge probe-ems` on the school PC; the exact required
> server-side changes (if any) are in
> [`docs/EMS_API_CONTRACT.md`](docs/EMS_API_CONTRACT.md); (2) real-device
> acceptance — execute [`docs/ACCEPTANCE_TEST_PLAN.md`](docs/ACCEPTANCE_TEST_PLAN.md).

## Quick start — Windows 10/11

**Preferred:** install the branded Windows application (no Python required).

1. Download **`ApexityEduBridge-Setup-1.0.0.exe`** from
   [GitHub Actions artifacts](../../actions) (workflow *Build Windows installer*)
   or build it locally with `packaging\build.ps1` — see
   [`docs/WINDOWS_PACKAGING.md`](docs/WINDOWS_PACKAGING.md).
2. Double-click the installer. It copies the app into
   `C:\Program Files\ApexityEdu\Attendance Bridge\`, creates a **Start Menu**
   and **Desktop** shortcut with the ApexityEdu logo, and optionally starts
   at sign-in.
3. Launch **ApexityEdu Bridge**. A native desktop window opens (not a
   browser) and a tray icon stays in the background. First run is a setup
   wizard (password + optional EMS token). Closing the window hides it —
   the bridge keeps collecting. Exit from the tray to stop. After a PC
   restart the app auto-starts hidden, reconnects devices, and resumes sync.

Developer / source install (Python 3.10+ still required):

```powershell
cd "E:\Software Run\ZKTeco"
git clone <this-repo-url> apexityedu-bridge
cd apexityedu-bridge
powershell -ExecutionPolicy Bypass -File scripts\Install-ApexityBridge.ps1
```

Full guide: [`docs/WINDOWS_DEPLOYMENT.md`](docs/WINDOWS_DEPLOYMENT.md).

## Everyday operations (PowerShell)

```powershell
.\scripts\Service-Control.ps1 status     # start | stop | restart | status
.\.venv\Scripts\python.exe -m apexity_bridge probe-ems                 # what does the EMS accept?
.\.venv\Scripts\python.exe -m apexity_bridge scan                      # discovery + identification (read-only)
.\.venv\Scripts\python.exe -m apexity_bridge scan --ip 192.168.0.201 --read-attendance 5
.\.venv\Scripts\python.exe -m apexity_bridge set-token                 # EMS token → Credential Manager
.\.venv\Scripts\python.exe -m apexity_bridge backup                    # consistent SQLite backup
```

## Configuration

Everything lives in `.env` (copy of [`.env.example`](.env.example)) — EMS URL
and protocol, dashboard port, scan/collect/sync intervals, device comm key,
extra subnets. A handful of values (EMS URL/protocol, intervals, device
timezone) can also be changed from **Settings** in the dashboard at runtime.
Secrets are never in `.env` — the EMS device token goes to Windows Credential
Manager, the dashboard password is stored as a PBKDF2 hash.

## Development

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements-dev.txt
./scripts/run_tests.sh                     # 116 automated tests
./scripts/dev_run.sh                       # run the bridge in the foreground
```

## Documentation

| Doc | Contents |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | design, identity rules, offline-first guarantees, security model, documented limitations |
| [`docs/EMS_API_CONTRACT.md`](docs/EMS_API_CONTRACT.md) | **required EMS-side API** (iClock/ADMS + REST) + reference implementation |
| [`docs/WINDOWS_DEPLOYMENT.md`](docs/WINDOWS_DEPLOYMENT.md) | install / update / uninstall / service details |
| [`docs/WINDOWS_PACKAGING.md`](docs/WINDOWS_PACKAGING.md) | **Windows .exe + installer** (PyInstaller + Inno Setup) |
| [`docs/ACCEPTANCE_TEST_PLAN.md`](docs/ACCEPTANCE_TEST_PLAN.md) | 16-scenario on-site verification gate |
| [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md) | diagnostics bundle, common issues |

## Security & privacy

Localhost-only dashboard with session auth, CSRF + origin checks and login
throttling; secrets in the OS credential store; HTTPS-only remote transport;
no fingerprint templates, ever; audit log of every admin action; redacted
diagnostic exports. Attendance is personal data — the bridge keeps collection
and retention to the minimum the school needs.

## In-app Windows updates (1.0.1+)

Install an update-enabled build once on existing PCs. Afterwards the desktop app
checks the public GitHub Release channel ten seconds after launch and every six
hours. **Check for updates** also checks immediately. When a newer stable version
is available, **Download and install update** downloads it in the background,
checks its size and SHA-256, asks for Windows administrator permission, then exits
the bridge and runs the installer. The installer reopens the app after updating.
If permission is declined, the current bridge stays open. Internet/verification
errors leave the current installation untouched and allow retrying. Database,
credentials and settings are kept; updating does not uninstall the application.
The installer may display progress and Windows may show UAC/SmartScreen prompts.
Update downloads are staged in the Windows user's temporary directory.

### Publishing a new version

1. Make changes and set `__version__` in `apexity_bridge/__init__.py` to a higher
   stable `X.Y.Z` (for example `1.0.2`). Never reuse a published version.
2. Run `python -m scripts.release_metadata` to synchronize the package, installer
   and Windows executable version fields. Commit these changes.
3. After review/merge, a maintainer publishes a matching tag, e.g. `v1.0.2`.
   The Windows workflow checks the tag/version match, builds the installer and
   `update.json`, then publishes both as a GitHub Release. Ordinary code pushes
   only produce CI artifacts: they do **not** update installed users.
4. Users receive the update on the next check. The repository and release assets
   must be publicly downloadable; no GitHub credentials are stored in the app.

For a local Windows build, `packaging/build.ps1` also synchronizes versions and
writes `dist/update.json` next to the installer. Both files must be published on
the matching release. The update channel is fixed to this repository; do not edit
URLs in the manifest to point elsewhere. HTTPS and SHA-256 protect transport and
integrity, but are not a replacement for Windows code signing. Protect release
permissions and use a signing certificate for production distribution.

Windows installer/UAC/restart behaviour must be smoke-tested on a Windows PC;
source checkouts can check releases but intentionally cannot install updates.
