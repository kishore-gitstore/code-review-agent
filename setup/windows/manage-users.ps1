<#
.SYNOPSIS
  manage-users.ps1 - add, remove and list the dashboard's local sign-in
  accounts (LOCAL_USERS / ADMIN_USERS in .env).

.DESCRIPTION
  Runs app.auth.local_users with the project's venv Python, so it works even
  when the system "python" doesn't have the app's packages installed
  (ModuleNotFoundError: No module named 'structlog').

  Passwords are typed at a hidden prompt and only a PBKDF2 hash is written to
  .env. Restart the service afterwards (or press Apply on the Configuration
  page) for changes to take effect.

.EXAMPLE
  .\setup\windows\manage-users.ps1                      # interactive menu
  .\setup\windows\manage-users.ps1 add admin -Admin     # add an admin
  .\setup\windows\manage-users.ps1 add kishore          # add a normal user
  .\setup\windows\manage-users.ps1 reset kishore        # reset an existing user's password
  .\setup\windows\manage-users.ps1 remove kishore
  .\setup\windows\manage-users.ps1 list
#>
param(
    [Parameter(Position = 0)]
    [ValidateSet("add", "reset", "remove", "list")]
    [string]$Command = "",

    [Parameter(Position = 1)]
    [string]$Username = "",

    [switch]$Admin
)

$ErrorActionPreference = "Stop"
# This script lives in setup\windows\ -- the venv and .env live at the repo
# root, two directories up.
Set-Location -Path (Join-Path $PSScriptRoot "..\..")

$Python = "venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    Write-Error "No venv found at $((Get-Location).Path)\venv. Run .\setup\windows\setup.ps1 first."
    exit 1
}
if (-not (Test-Path ".env")) {
    Write-Error ".env not found - run .\setup\windows\setup.ps1 (or copy .env.example to .env) first."
    exit 1
}

function Invoke-LocalUsers {
    param([string[]]$CliArgs)
    # Out-Host so the output is shown, not swallowed into the return value.
    & $Python -m app.auth.local_users @CliArgs | Out-Host
    return $LASTEXITCODE
}

function Read-Username {
    while ($true) {
        $name = (Read-Host "Username").Trim()
        if ($name -match '^[A-Za-z0-9._-]{1,64}$') { return $name }
        Write-Host "Usernames may contain letters, digits, '.', '_' and '-' only (max 64)." -ForegroundColor Yellow
    }
}

# --- Non-interactive: arguments given ---
if ($Command) {
    switch ($Command) {
        "list"   { $code = Invoke-LocalUsers @("list") }
        { $_ -in "remove", "reset" } {
            if (-not $Username) { $Username = Read-Username }
            $code = Invoke-LocalUsers @($Command, $Username)
        }
        "add"    {
            if (-not $Username) { $Username = Read-Username }
            $cli = @("add", $Username)
            if ($Admin) { $cli += "--admin" }
            $code = Invoke-LocalUsers $cli
        }
    }
    exit $code
}

# --- Interactive menu ---
$changed = $false
while ($true) {
    Write-Host ""
    Write-Host "Dashboard users" -ForegroundColor Cyan
    Write-Host "  1) List users"
    Write-Host "  2) Add a normal user (dashboard only)"
    Write-Host "  3) Add an admin (dashboard + Home + Configuration)"
    Write-Host "  4) Reset a user's password"
    Write-Host "  5) Remove a user"
    Write-Host "  q) Quit"
    $choice = (Read-Host "Choose").Trim().ToLower()

    switch ($choice) {
        "1" { [void](Invoke-LocalUsers @("list")) }
        "2" { if ((Invoke-LocalUsers @("add", (Read-Username))) -eq 0) { $changed = $true } }
        "3" { if ((Invoke-LocalUsers @("add", (Read-Username), "--admin")) -eq 0) { $changed = $true } }
        "4" { if ((Invoke-LocalUsers @("reset", (Read-Username))) -eq 0) { $changed = $true } }
        "5" {
            $name = Read-Username
            $confirm = (Read-Host "Remove '$name'? (y/N)").Trim().ToLower()
            if ($confirm -eq "y") {
                if ((Invoke-LocalUsers @("remove", $name)) -eq 0) { $changed = $true }
            }
        }
        { $_ -in "q", "quit", "exit", "" } {
            if ($changed) {
                Write-Host ""
                Write-Host "Changes saved to .env. Restart the service for them to take effect:" -ForegroundColor Yellow
                Write-Host "  nssm restart code-review-agent   (if installed as a service)"
                Write-Host "  or stop and re-run .\setup\windows\run.ps1"
            }
            exit 0
        }
        default { Write-Host "Unknown choice '$choice'." -ForegroundColor Yellow }
    }
}
