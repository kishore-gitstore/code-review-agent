<#
.SYNOPSIS
  install-service.ps1 - installs Code Review Agent as a Windows service,
  the native-Windows equivalent of install-service.sh's systemd unit. Uses
  NSSM (Non-Sucking Service Manager) to wrap venv\Scripts\python.exe main.py
  as a real service: starts on boot, restarts on failure, survives logout.
  Run from an elevated (Administrator) PowerShell.

.PARAMETER ServiceName
  Windows service name. Default: code-review-agent

.EXAMPLE
  .\setup\windows\install-service.ps1
#>
param(
    [string]$ServiceName = "code-review-agent"
)
$ErrorActionPreference = "Stop"
# This script lives in setup\windows\ -- the app itself (venv, .env, main.py,
# where logs\ gets created) lives at the repo root, two directories up.
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location -Path $RepoRoot

$currentUser = [Security.Principal.WindowsIdentity]::GetCurrent()
$isAdmin = (New-Object Security.Principal.WindowsPrincipal $currentUser).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Error "Run this from an elevated (Administrator) PowerShell."
    exit 1
}

if (-not (Test-Path "venv\Scripts\python.exe")) {
    Write-Error "No venv found at venv\Scripts\python.exe -- run .\setup\windows\setup.ps1 first, then .\setup\windows\run.ps1 once by hand to confirm the app actually starts."
    exit 1
}

if (-not (Test-Path ".env")) {
    Write-Error ".env not found -- copy .env.example to .env and configure it first."
    exit 1
}

function Sync-Path {
    # winget updates PATH in the registry, not this already-running process's
    # $env:Path -- without this, a package winget just installed (or that was
    # already installed but only added to PATH by a previous winget run) stays
    # invisible to Get-Command until a brand new terminal is opened.
    $machine = [System.Environment]::GetEnvironmentVariable("Path", "Machine")
    $user    = [System.Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = @($machine, $user) -join ";"
}

if (-not (Get-Command nssm -ErrorAction SilentlyContinue)) {
    Write-Host "==> Installing NSSM via winget..."
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Write-Error "winget not found. Install NSSM manually from https://nssm.cc/ and re-run this script."
        exit 1
    }
    winget install --id NSSM.NSSM -e --accept-source-agreements --accept-package-agreements
    Sync-Path
}

if (-not (Get-Command nssm -ErrorAction SilentlyContinue)) {
    Write-Error "nssm still not on PATH after refreshing it from the registry -- open a new terminal and re-run."
    exit 1
}

$workDir   = $RepoRoot
$pythonExe = Join-Path $workDir "venv\Scripts\python.exe"
$logDir    = Join-Path $workDir "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

function Get-EnvValue($key, $default) {
    $line = Select-String -Path ".env" -Pattern "^$key=" -ErrorAction SilentlyContinue | Select-Object -Last 1
    if ($line) { return ($line.Line -replace "^$key=", '').Trim() }
    return $default
}
# main.py reads APP_HOST/APP_PORT via os.getenv (its own fallback, separate
# from pydantic-settings' .env loading for the rest of the app's config) --
# same reason run.ps1/run.sh load .env into the process environment before
# starting. A plain Windows service starts with a clean environment (nothing
# inherited from any interactive shell or from .env), so pass them through
# explicitly -- this mirrors the systemd unit's `EnvironmentFile=.env`.
$appHost = Get-EnvValue "APP_HOST" "0.0.0.0"
$appPort = Get-EnvValue "APP_PORT" "8000"

# Idempotent re-run: remove a previous install of the same name first instead
# of failing on "service already exists".
$existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "==> Existing service '$ServiceName' found -- stopping and removing it first."
    nssm stop $ServiceName 2>$null | Out-Null
    nssm remove $ServiceName confirm | Out-Null
}

Write-Host "==> Installing service '$ServiceName'..."
nssm install $ServiceName $pythonExe "main.py"
nssm set $ServiceName AppDirectory $workDir
nssm set $ServiceName AppEnvironmentExtra "APP_HOST=$appHost" "APP_PORT=$appPort"
nssm set $ServiceName AppStdout (Join-Path $logDir "service.out.log")
nssm set $ServiceName AppStderr (Join-Path $logDir "service.err.log")
nssm set $ServiceName AppRotateFiles 1
nssm set $ServiceName AppRotateBytes 10485760
nssm set $ServiceName Start SERVICE_AUTO_START
nssm set $ServiceName AppExit Default Restart
nssm set $ServiceName AppRestartDelay 60000
# UVICORN_RELOAD is deliberately not set -- same stable no-reload mode as the
# Linux systemd unit (and reload can't work here anyway: see run.ps1's note
# on uvicorn forcing WindowsSelectorEventLoopPolicy, which breaks the
# nvidia-smi/static-analysis subprocess calls this app makes on every run).

nssm start $ServiceName

Write-Host ""
Write-Host "Service '$ServiceName' installed and started."
Write-Host "  Status  : Get-Service $ServiceName"
Write-Host "  Logs    : $logDir\service.out.log / service.err.log"
Write-Host "  Restart : Restart-Service $ServiceName   (after a code/config change)"
Write-Host "  Stop    : Stop-Service $ServiceName"
Write-Host "  Remove  : .\setup\windows\uninstall-service.ps1"
