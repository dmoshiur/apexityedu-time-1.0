# Building the Windows application (.exe + installer)

School PCs should install **`ApexityEduBridge-Setup-1.0.0.exe`**. That wizard
drops a real Windows application into Program Files, with the ApexityEdu logo
on the `.exe`, the Start Menu, and the Desktop shortcut.

## What you get

| File | What it is |
|---|---|
| `ApexityEduBridge-Setup-1.0.0.exe` | Inno Setup installer (Add/Remove Programs, shortcuts, auto-start, uninstaller) |
| `ApexityEduBridge.exe` | The frozen application (system tray + local dashboard at http://127.0.0.1:8321) |

After install:

```
C:\Program Files\ApexityEdu\Attendance Bridge\
    ApexityEduBridge.exe          ← logo icon, double-click to open
    _internal\                    ← Python runtime + libraries (do not edit)
C:\ProgramData\ApexityEduBridge\
    .env                          ← configuration
    bridge.db                     ← SQLite
    logs\bridge.log
```

Start Menu → **ApexityEdu Bridge** (logo). Desktop shortcut (optional, logo).
Tray icon while running. First launch opens a setup wizard for the dashboard
password (no Python, no PowerShell).

## Build on a Windows 10/11 PC

Prerequisites: Python 3.10+ (64-bit) from python.org, and
[Inno Setup 6](https://jrsoftware.org/isinfo.php) for the installer.

```powershell
git clone <this-repo> apexityedu-bridge
cd apexityedu-bridge
powershell -ExecutionPolicy Bypass -File packaging\build.ps1
```

Outputs land in `dist\`:

* `dist\ApexityEduBridge\ApexityEduBridge.exe` — portable app
* `dist\ApexityEduBridge-Setup-1.0.0.exe` — installer

## Build on GitHub Actions

Push to any branch (or run **Actions → Build Windows installer → Run workflow**).
The `windows-latest` job uploads two artifacts:

* `ApexityEduBridge-Setup-1.0.0`
* `ApexityEduBridge-portable-1.0.0`

Download them from the workflow run page.

## Branding

Source art lives in `branding/`. `packaging/make_icons.py` produces the
multi-size `.ico`, Inno Setup BMPs, favicon and dashboard logo. Re-run it
after changing the PNGs.
