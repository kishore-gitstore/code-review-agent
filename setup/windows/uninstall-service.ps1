<#
.SYNOPSIS
  uninstall-service.ps1 - stops and removes the code-review-agent Windows
  service installed by install-service.ps1. Only touches the service --
  the venv, .env, and repo files are left alone. Run from an elevated
  (Administrator) PowerShell.

.PARAMETER ServiceName
  Windows service name. Default: code-review-agent
#>
param(
    [string]$ServiceName = "code-review-agent"
)
$ErrorActionPreference = "Stop"

$currentUser = [Security.Principal.WindowsIdentity]::GetCurrent()
$isAdmin = (New-Object Security.Principal.WindowsPrincipal $currentUser).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Error "Run this from an elevated (Administrator) PowerShell."
    exit 1
}

if (-not (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue)) {
    Write-Host "Service '$ServiceName' not found -- nothing to uninstall."
    exit 0
}

if (-not (Get-Command nssm -ErrorAction SilentlyContinue)) {
    Write-Error "nssm not found on PATH -- can't cleanly remove the service. Install NSSM (winget install --id NSSM.NSSM -e) and re-run."
    exit 1
}

nssm stop $ServiceName 2>$null | Out-Null
nssm remove $ServiceName confirm

Write-Host "Removed service '$ServiceName'. venv, .env, and repo files were left untouched."
