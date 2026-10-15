<#
.SYNOPSIS
    SDLC Step 7 - Automated Verification.

.DESCRIPTION
    Runs the full quality gate: format check, lint, type check, tests with
    coverage. This is the exact set of checks CI runs, so a green run here means
    a green run there.

    No task may be reported as complete until this script exits zero.

.EXAMPLE
    .\scripts\verify.ps1
    .\scripts\verify.ps1 -Fix    # auto-fix formatting and lint issues first
#>
[CmdletBinding()]
param(
    [switch]$Fix
)

$ErrorActionPreference = 'Continue'
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

$python = Join-Path $RepoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    Write-Error 'No .venv found. Run .\scripts\bootstrap.ps1 first.'
}

$failures = @()

function Invoke-Gate {
    param([string]$Name, [string[]]$Arguments)

    Write-Host ''
    Write-Host "=== $Name ===" -ForegroundColor Cyan
    & $python @Arguments
    if ($LASTEXITCODE -ne 0) {
        $script:failures += $Name
        Write-Host "FAILED: $Name" -ForegroundColor Red
    } else {
        Write-Host "PASSED: $Name" -ForegroundColor Green
    }
}

if ($Fix) {
    Write-Host 'Applying automatic fixes...' -ForegroundColor Yellow
    & $python -m ruff format .
    & $python -m ruff check . --fix
}

Invoke-Gate 'Format'      @('-m', 'ruff', 'format', '--check', '.')
Invoke-Gate 'Lint'        @('-m', 'ruff', 'check', '.')
Invoke-Gate 'Type check'  @('-m', 'mypy', 'src')
Invoke-Gate 'Tests'       @('-m', 'pytest', '--cov=src', '--cov-report=term-missing')

Write-Host ''
if ($failures.Count -gt 0) {
    Write-Host ('VERIFICATION FAILED: ' + ($failures -join ', ')) -ForegroundColor Red
    Write-Host 'Do not report this task as complete.' -ForegroundColor Red
    exit 1
}

Write-Host 'ALL GATES PASSED.' -ForegroundColor Green
Write-Host 'Next: SDLC Step 8 - README & architecture drift audit.' -ForegroundColor Cyan
exit 0
