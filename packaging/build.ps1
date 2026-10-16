<#
.SYNOPSIS
    Build ApexityEduBridge.exe and the Windows installer on a Windows 10/11 PC.

.DESCRIPTION
    1. Creates .venv-build (isolated from the runtime .venv)
    2. Installs runtime + packaging dependencies
    3. Regenerates ICO/BMP icons from branding PNGs
    4. Freezes the app with PyInstaller (onedir)
    5. Compiles ApexityEduBridge-Setup-1.0.0.exe with Inno Setup if available

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File packaging\build.ps1
#>
[CmdletBinding()]
param(
    [switch]$SkipInstaller
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Write-Step([string]$msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }

Write-Step "Python"
$py = Get-Command py -ErrorAction SilentlyContinue
if ($py) { $Python = & py -3 -c "import sys; print(sys.executable)" }
else { $Python = (Get-Command python -ErrorAction Stop).Source }
Write-Host "Using $Python"

Write-Step "Build virtualenv"
$Venv = Join-Path $Root ".venv-build"
if (-not (Test-Path $Venv)) {
    & $Python -m venv $Venv
}
$VenvPy = Join-Path $Venv "Scripts\python.exe"
& $VenvPy -m pip install --upgrade pip wheel
& $VenvPy -m pip install -r requirements.txt
& $VenvPy -m pip install -r requirements-packaging.txt

Write-Step "Icons"
& $VenvPy (Join-Path $Root "packaging\make_icons.py")

Write-Step "PyInstaller (ApexityEduBridge.exe)"
$Dist = Join-Path $Root "dist"
$Build = Join-Path $Root "build"
if (Test-Path $Dist) { Remove-Item -Recurse -Force $Dist }
if (Test-Path $Build) { Remove-Item -Recurse -Force $Build }
& $VenvPy -m PyInstaller --noconfirm --clean (Join-Path $Root "packaging\ApexityEduBridge.spec")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

$Exe = Join-Path $Dist "ApexityEduBridge\ApexityEduBridge.exe"
if (-not (Test-Path $Exe)) { throw "expected $Exe was not produced" }
Write-Host "Built $Exe"

if ($SkipInstaller) {
    Write-Host "Skipping installer (-SkipInstaller)." -ForegroundColor Yellow
    exit 0
}

Write-Step "Inno Setup installer"
$Iscc = @(
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles(x86)}\Inno Setup 5\ISCC.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1

if (-not $Iscc) {
    Write-Host "ISCC.exe not found. Install Inno Setup 6 from https://jrsoftware.org/isinfo.php" -ForegroundColor Yellow
    Write-Host "The portable app is ready at dist\ApexityEduBridge\ApexityEduBridge.exe"
    exit 0
}

& $Iscc (Join-Path $Root "packaging\installer.iss")
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }

$Setup = Join-Path $Dist "ApexityEduBridge-Setup-1.0.0.exe"
Write-Host @"

Done.
  Application : $Exe
  Installer   : $Setup

Double-click the installer on the school PC. After setup, the Start Menu
and Desktop shortcuts show the ApexityEdu logo.
"@ -ForegroundColor Green
