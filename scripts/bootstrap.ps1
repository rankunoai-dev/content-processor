<#
.SYNOPSIS
    One-command environment setup for the Rankuno automation repository.

.DESCRIPTION
    Creates a virtual environment, installs the package with its dev extras, and
    seeds a local .env. Safe to re-run.

.EXAMPLE
    .\scripts\bootstrap.ps1
    .\scripts\bootstrap.ps1 -WithSeo    # also install the SEO domain libraries
#>
[CmdletBinding()]
param(
    [switch]$WithSeo
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

# --- 1. Locate a Python 3.11+ interpreter --------------------------------
$python = $null
foreach ($candidate in @('py -3.12', 'py -3.11', 'python')) {
    $parts = $candidate.Split(' ')
    $exe = Get-Command $parts[0] -ErrorAction SilentlyContinue
    if ($exe) { $python = $candidate; break }
}

if (-not $python) {
    Write-Error @'
No Python interpreter found.

Install Python 3.11 or newer, then re-run this script:
  winget install Python.Python.3.12

Note: the "python3" stub under WindowsApps is a Microsoft Store placeholder,
not a real interpreter.
'@
}

Write-Host "Using interpreter: $python" -ForegroundColor Cyan

# --- 2. Create the virtual environment ------------------------------------
if (-not (Test-Path '.venv')) {
    Write-Host 'Creating virtual environment (.venv)...' -ForegroundColor Cyan
    Invoke-Expression "$python -m venv .venv"
} else {
    Write-Host 'Virtual environment already exists.' -ForegroundColor DarkGray
}

$venvPython = Join-Path $RepoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPython)) { Write-Error "venv creation failed: $venvPython not found." }

# --- 3. Install dependencies ----------------------------------------------
Write-Host 'Installing dependencies...' -ForegroundColor Cyan
& $venvPython -m pip install --upgrade pip
$extras = if ($WithSeo) { '.[dev,seo]' } else { '.[dev]' }
& $venvPython -m pip install -e $extras
if ($LASTEXITCODE -ne 0) { Write-Error 'Dependency installation failed.' }

# --- 4. Seed the local environment file -----------------------------------
if (-not (Test-Path '.env')) {
    Copy-Item '.env.example' '.env'
    Write-Host 'Created .env from .env.example - fill in your credentials.' -ForegroundColor Yellow
} else {
    Write-Host '.env already exists; leaving it untouched.' -ForegroundColor DarkGray
}

# --- 5. Install git hooks (only if this is a git repo) --------------------
if ((Test-Path '.git') -and (Get-Command git -ErrorAction SilentlyContinue)) {
    & $venvPython -m pre_commit install
    Write-Host 'Installed pre-commit hooks.' -ForegroundColor Green
} else {
    Write-Host 'Not a git repository - skipping pre-commit hook install.' -ForegroundColor DarkGray
}

Write-Host ''
Write-Host 'Bootstrap complete.' -ForegroundColor Green
Write-Host 'Activate with:  .\.venv\Scripts\Activate.ps1'
Write-Host 'Verify with:    .\scripts\verify.ps1'
