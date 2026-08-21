<#
.SYNOPSIS
  One-shot bootstrap for Windows. Installs system prerequisites (git,
  Python 3.12, Ollama) via winget, creates the venv, installs Python deps,
  and bootstraps .env from .env.example.

.PARAMETER NoSystem
  Skip OS package installs (git/python/ollama) via winget.

.PARAMETER NoOllama
  Skip Ollama install/pull even if LLM_PROVIDER=ollama in .env.

.PARAMETER Model
  Ollama model to pull. Default: qwen3-coder:latest

.PARAMETER StaticLangs
  Comma-separated languages to also install linters for:
  python|js|dotnet|rust|cpp|java

.EXAMPLE
  .\setup\windows\setup.ps1
  .\setup\windows\setup.ps1 -NoSystem
  .\setup\windows\setup.ps1 -Model deepseek-r1:7b
  .\setup\windows\setup.ps1 -StaticLangs python,js
#>
param(
    [switch]$NoSystem,
    [switch]$NoOllama,
    [string]$Model = "qwen3-coder:latest",
    [string]$StaticLangs = ""
)

$ErrorActionPreference = "Stop"
# This script lives in setup\windows\ -- everything it operates on (venv,
# .env, requirements.txt, main.py) lives at the repo root, two directories up.
Set-Location -Path (Join-Path $PSScriptRoot "..\..")

function Log($msg) { Write-Host "==> $msg" }

function Test-Command($name) {
    return [bool](Get-Command $name -ErrorAction SilentlyContinue)
}

function Sync-Path {
    # winget updates PATH in the registry, not this already-running process's
    # $env:Path -- without this, a package winget just installed stays
    # invisible to Get-Command/Test-Command until a brand new terminal opens.
    $machine = [System.Environment]::GetEnvironmentVariable("Path", "Machine")
    $user    = [System.Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = @($machine, $user) -join ";"
}

function Install-Winget($id, $friendlyName) {
    if (-not (Test-Command "winget")) {
        Write-Warning "winget not found. Install $friendlyName manually, then re-run this script."
        return
    }
    Log "Installing $friendlyName via winget..."
    winget install --id $id -e --accept-source-agreements --accept-package-agreements
    Sync-Path
}

# -- System prerequisites ------------------------------------
if (-not $NoSystem) {
    Log "Checking system prerequisites (git, Python 3.12)..."

    if (-not (Test-Command "git")) {
        Install-Winget "Git.Git" "Git"
    }

    $pythonOk = $false
    if (Test-Command "python") {
        try {
            $verOut = (python --version) 2>&1
            if ($verOut -match "3\.1[2-9]") { $pythonOk = $true }
        } catch {}
    }
    if (-not $pythonOk) {
        Install-Winget "Python.Python.3.12" "Python 3.12"
        Write-Warning "If 'python' isn't recognized next, open a new terminal (PATH was just updated) and re-run this script."
    }
} else {
    Log "Skipping system package installs (-NoSystem)."
}

if (-not (Test-Command "python")) {
    Write-Error "No 'python' on PATH. Open a new terminal after installing Python 3.12 and re-run this script."
    exit 1
}
Log "Using Python: $(python --version)"

# -- .env bootstrap (before deciding what to install, so the LLM_PROVIDER
#    choice already on disk - or the default - drives the Ollama step) --
if (-not (Test-Path ".env")) {
    Log "Creating .env from .env.example - edit it (GITLAB_INSTANCES, provider keys) before running the server."
    Copy-Item ".env.example" ".env"
} else {
    Log ".env already exists, leaving it as-is."
}

$llmProvider = "ollama"
$envLine = Select-String -Path ".env" -Pattern '^LLM_PROVIDER=' | Select-Object -Last 1
if ($envLine) {
    $llmProvider = ($envLine.Line -replace '^LLM_PROVIDER=', '').Trim()
}

# -- Ollama (only if this config actually uses it) -----------
if (-not $NoOllama -and $llmProvider -eq "ollama") {
    if (-not (Test-Command "ollama")) {
        Log "LLM_PROVIDER=ollama - installing Ollama..."
        Install-Winget "Ollama.Ollama" "Ollama"
        Write-Warning "If 'ollama' isn't recognized next, open a new terminal and re-run this script."
    }
    if (Test-Command "ollama") {
        Log "Pulling Ollama model: $Model (this can take a while)..."
        try {
            ollama pull $Model
        } catch {
            Log "Model pull failed/skipped - make sure the Ollama app/service is running, then run: ollama pull $Model"
        }
    }
} elseif ($llmProvider -ne "ollama") {
    Log "LLM_PROVIDER=$llmProvider - skipping Ollama install. Make sure the matching API key is set in .env."
} else {
    Log "Skipping Ollama install (-NoOllama)."
}

# -- Optional static-analysis tool installs ------------------
if ($StaticLangs) {
    foreach ($lang in ($StaticLangs -split ",")) {
        switch ($lang.Trim().ToLower()) {
            "python" {
                Log "Installing Ruff..."
                python -m pip install --quiet --user ruff
            }
            "js" {
                Log "Installing ESLint (global npm install)..."
                if (Test-Command "npm") { npm install -g eslint } else { Write-Warning "npm not found - install Node.js first." }
            }
            "dotnet" {
                Install-Winget "Microsoft.DotNet.SDK.8" ".NET SDK"
            }
            "rust" {
                Install-Winget "Rustlang.Rustup" "Rust (rustup)"
                if (Test-Command "rustup") { rustup component add clippy }
            }
            "cpp" {
                Install-Winget "LLVM.LLVM" "LLVM (clang-tidy)"
            }
            "java" {
                Log "SpotBugs: download from https://spotbugs.github.io/ (no winget package)."
            }
            default {
                Write-Warning "Unknown -StaticLangs entry: $lang (expected python|js|dotnet|rust|cpp|java)"
            }
        }
    }
}

# -- Python venv + deps ---------------------------------------
if (-not (Test-Path "venv")) {
    Log "Creating virtual environment..."
    python -m venv venv
}

Log "Installing Python dependencies..."
# Upgrading pip via python -m pip (not pip.exe directly) -- pip.exe can't
# overwrite its own running executable on Windows.
& "venv\Scripts\python.exe" -m pip install -q --disable-pip-version-check --upgrade pip
& "venv\Scripts\python.exe" -m pip install -q --disable-pip-version-check -r requirements.txt

Log "Setup complete."
Write-Host ""
Write-Host "Next steps:"
Write-Host "  1. Edit .env - set GITLAB_INSTANCES and the $llmProvider provider key/config."
Write-Host "  2. Start the server: .\setup\windows\run.ps1"
Write-Host "  3. Point a GitLab webhook at http://<this-host>:8000/webhook/gitlab/<instance-name>"
