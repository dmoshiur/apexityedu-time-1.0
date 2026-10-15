<#
.SYNOPSIS
    Start / stop / restart / status for the ApexityEdu Bridge auto-start task.
.EXAMPLE
    .\Service-Control.ps1 status
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet("start", "stop", "restart", "status")]
    [string]$Action
)
$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$VenvPy = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPy)) { throw "bridge is not installed (run scripts\Install-ApexityBridge.ps1 first)" }
& $VenvPy -m apexity_bridge service $Action
exit $LASTEXITCODE
