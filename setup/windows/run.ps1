<#
.SYNOPSIS
  run.ps1 - start the Code Review Agent on Windows (native PowerShell
  equivalent of run.sh). Assumes .\setup\windows\setup.ps1 has already
  created the venv and .env. Loads simple KEY=VALUE lines from .env into the
  process environment (so APP_HOST/APP_PORT reach main.py's os.getenv
  fallback, same as run.sh's `source .env`), then starts uvicorn.
#>
$ErrorActionPreference = "Stop"
# This script lives in setup\windows\ -- everything it operates on (venv,
# .env, main.py) lives at the repo root, two directories up.
Set-Location -Path (Join-Path $PSScriptRoot "..\..")

if (-not (Test-Path "venv\Scripts\python.exe")) {
    Write-Error "No venv found. Run .\setup\windows\setup.ps1 first."
    exit 1
}

if (-not (Test-Path ".env")) {
    Write-Error ".env not found - copy .env.example to .env and set GITLAB_INSTANCES first."
    exit 1
}

# Parse .env for simple single-line KEY=VALUE pairs. Skips multi-line
# single-quoted values (e.g. GITLAB_INSTANCES='{ ... }' spanning several
# lines) - those are read directly by pydantic-settings inside the app, not
# needed in the process environment.
$lines = Get-Content ".env"
$i = 0
while ($i -lt $lines.Count) {
    $line = $lines[$i]
    if ($line -match '^\s*#' -or $line -match '^\s*$') { $i++; continue }
    if ($line -match "^([A-Za-z_][A-Za-z0-9_]*)=(.*)$") {
        $key = $matches[1]
        $val = $matches[2]
        if ($val -match "^'[^']*$") {
            # multi-line quoted value - skip to its closing quote, don't export
            $i++
            while ($i -lt $lines.Count -and $lines[$i] -notmatch "'\s*$") { $i++ }
            $i++
            continue
        }
        $val = $val.Trim().Trim("'").Trim('"')
        [System.Environment]::SetEnvironmentVariable($key, $val, "Process")
    }
    $i++
}

# Explicitly cleared (not just left unset) -- Uvicorn 0.30.6 forces
# WindowsSelectorEventLoopPolicy whenever reload is on (see its
# uvicorn/loops/asyncio.py: `if sys.platform == "win32" and use_subprocess`),
# which cannot create subprocesses -- breaking nvidia-smi hardware detection
# (called on every startup, not optional) and every static-analysis linter.
# So reload and this app are incompatible on Windows; stay in stable mode,
# same as the Linux systemd unit. Cleared rather than just omitted because a
# prior run.ps1 (or manual `$env:UVICORN_RELOAD = "true"`) in this same
# terminal session would otherwise leak into this process's environment too
# -- $env: assignments persist for the rest of the shell session, not just
# for the script that set them.
Remove-Item Env:UVICORN_RELOAD -ErrorAction SilentlyContinue

& "venv\Scripts\python.exe" main.py
