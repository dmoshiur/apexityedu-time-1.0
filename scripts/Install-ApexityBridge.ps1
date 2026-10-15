<#
.SYNOPSIS
    Installs the ApexityEdu Smart Attendance Bridge on this Windows PC.

.DESCRIPTION
    * Verifies Python >= 3.10
    * Creates a virtual environment and installs dependencies
    * Creates the %ProgramData%\ApexityEduBridge data directory with ACLs
    * Copies .env.example -> .env (only if missing)
    * Initialises the SQLite database
    * Stores the EMS device token in Windows Credential Manager (optional)
    * Sets the dashboard admin password (PBKDF2 hash, optional interactive)
    * Registers + starts the auto-start Task Scheduler task (restart-on-failure)

    Run from an elevated or normal PowerShell window; normal is fine because the
    task runs as the current user (least privilege).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\Install-ApexityBridge.ps1
.EXAMPLE
    .\Install-ApexityBridge.ps1 -SkipInteractive
#>
[CmdletBinding()]
param(
    [switch]$SkipInteractive,
    [string]$EmsBaseUrl = "https://apexityedu.com"
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

function Write-Step([string]$msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }

Write-Step "Checking Python (>= 3.10 required)"
$py = Get-Command py -ErrorAction SilentlyContinue
if ($py) {
    $PythonExe = (& py -3 -c "import sys; print(sys.executable)")
} else {
    $PythonExe = (Get-Command python -ErrorAction Stop).Source
}
$ver = & $PythonExe -c "import sys; print('%d.%d' % sys.version_info[:2])"
if ([version]$ver -lt [version]"3.10") { throw "Python $ver found, need >= 3.10" }
Write-Host "Python $ver at $PythonExe"

Write-Step "Creating virtual environment (.venv)"
if (-not (Test-Path ".venv")) {
    & $PythonExe -m venv .venv
}
$VenvPy  = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$VenvPyw = Join-Path $RepoRoot ".venv\Scripts\pythonw.exe"

Write-Step "Installing dependencies"
& $VenvPy -m pip install --upgrade pip | Out-Null
& $VenvPy -m pip install -r requirements.txt

Write-Step "Preparing data directory (%ProgramData%\ApexityEduBridge)"
$DataDir = Join-Path $env:ProgramData "ApexityEduBridge"
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
# grant the current user full control (least privilege: only this user)
icacls $DataDir /grant "${env:USERNAME}:(OI)(CI)F" /T | Out-Null

Write-Step "Configuring .env"
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    (Get-Content ".env") -replace "^EMS_BASE_URL=.*", "EMS_BASE_URL=$EmsBaseUrl" |
        Set-Content ".env" -Encoding UTF8
    Write-Host "Created .env — review it with: notepad .env"
} else {
    Write-Host ".env already exists — left untouched"
}

Write-Step "Initialising SQLite database"
& $VenvPy -m apexity_bridge init-db

if (-not $SkipInteractive) {
    Write-Step "EMS device token (stored in Windows Credential Manager, press Enter to skip)"
    $token = Read-Host -AsSecureString "EMS device token"
    if ($token.Length -gt 0) {
        $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
            [Runtime.InteropServices.Marshal]::SecureStringToBSTR($token))
        $env:APEXITY_TMP_TOKEN = $plain
        & $VenvPy -c "import os; from apexity_bridge.config import load_config; from apexity_bridge.security.secrets import SecretStore; SecretStore(load_config().data_dir).set_ems_token(os.environ['APEXITY_TMP_TOKEN']); print('token stored in Windows Credential Manager')"
        Remove-Item Env:APEXITY_TMP_TOKEN
        $plain = $null
    }

    Write-Step "Dashboard admin password"
    & $VenvPy -m apexity_bridge set-admin-password

    Write-Step "EMS connectivity check (does the EMS have an attendance endpoint?)"
    & $VenvPy -m apexity_bridge probe-ems || Write-Host "(probe reported problems — see docs/EMS_API_CONTRACT.md)" -ForegroundColor Yellow
}

Write-Step "Registering auto-start (Task Scheduler: at logon, restart-on-failure)"
# pythonw.exe → no console window for the background task
& $VenvPyw -m apexity_bridge service install

Write-Step "Starting the bridge"
& $VenvPy -m apexity_bridge service start

$port = ([regex]::Match((Get-Content ".env" -Raw), "DASHBOARD_PORT=(\d+)")).Groups[1].Value
if (-not $port) { $port = "8321" }
Write-Step "Done"
Write-Host @"
Dashboard  : http://127.0.0.1:$port
Data dir   : $DataDir
Logs       : $DataDir\logs\bridge.log
Service    : scripts\Service-Control.ps1  (start|stop|restart|status)
Uninstall  : scripts\Uninstall-ApexityBridge.ps1
Next steps : open the dashboard → Devices → 'Scan LAN now', register your K40,
             then map biometric user IDs to students on the Students page.
"@ -ForegroundColor Green
