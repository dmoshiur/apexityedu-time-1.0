<#
.SYNOPSIS
    Uninstalls the ApexityEdu Smart Attendance Bridge.

.DESCRIPTION
    Default behaviour (safe): stop + unregister the auto-start task and write a
    FINAL database backup. All data and the venv are preserved.

    -DeleteData additionally removes %ProgramData%\ApexityEduBridge
    (attendance history, settings, logs, backups) — this is irreversible.
    -RemoveVenv additionally removes the .venv directory.
    The EMS device token is removed from Windows Credential Manager in all cases.

.EXAMPLE
    .\Uninstall-ApexityBridge.ps1
.EXAMPLE
    .\Uninstall-ApexityBridge.ps1 -DeleteData -RemoveVenv
#>
[CmdletBinding()]
param(
    [switch]$DeleteData,
    [switch]$RemoveVenv
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot
$VenvPy = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$DataDir = Join-Path $env:ProgramData "ApexityEduBridge"

Write-Host "==> Stopping and removing the auto-start task" -ForegroundColor Cyan
if (Test-Path $VenvPy) {
    & $VenvPy -m apexity_bridge service stop      2>$null
    # final backup BEFORE the task is gone
    & $VenvPy -m apexity_bridge backup            2>$null
    & $VenvPy -m apexity_bridge service uninstall
    & $VenvPy -m apexity_bridge delete-token      2>$null
} else {
    schtasks /End /TN ApexityEduBridge 2>$null
    schtasks /Delete /TN ApexityEduBridge /F 2>$null
}

if ($DeleteData) {
    Write-Host "==> Deleting data directory $DataDir" -ForegroundColor Yellow
    Remove-Item -Recurse -Force $DataDir -ErrorAction SilentlyContinue
} else {
    Write-Host "Data preserved at $DataDir (use -DeleteData to remove)" -ForegroundColor Green
}

if ($RemoveVenv -and (Test-Path ".venv")) {
    Write-Host "==> Removing .venv" -ForegroundColor Yellow
    Remove-Item -Recurse -Force ".venv"
}
Write-Host "Uninstall complete." -ForegroundColor Green
