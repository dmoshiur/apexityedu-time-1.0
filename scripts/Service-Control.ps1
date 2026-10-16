<#
.SYNOPSIS
    Start / stop / restart / status for the ApexityEdu Bridge auto-start task,
    plus install/uninstall of the auto-start task and of the optional
    scheduled-sync task (runs "sync-now" every 15 minutes).
.EXAMPLE
    .\Service-Control.ps1 status
.EXAMPLE
    .\Service-Control.ps1 install-sync
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet("start", "stop", "restart", "status", "install", "uninstall",
                 "install-sync", "uninstall-sync")]
    [string]$Action
)
$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$VenvPy = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPy)) { throw "bridge is not installed (run scripts\Install-ApexityBridge.ps1 first)" }
& $VenvPy -m apexity_bridge service $Action
exit $LASTEXITCODE
