# ENGRAM installer for Windows (PowerShell).  Usage:  powershell -ExecutionPolicy Bypass -File scripts\install.ps1 [-NoModel]
param([switch]$NoModel)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

$py = $null
foreach ($cand in @("py -3.12", "py -3.11", "python")) {
    try {
        $v = & ([scriptblock]::Create("$cand -c `"import sys;print(sys.version_info>=(3,11))`""))
        if ($v -eq "True") { $py = $cand; break }
    } catch {}
}
if (-not $py) { throw "Python 3.11+ not found. Install it from https://www.python.org/downloads/ and re-run." }
Write-Host "Using $py"

if (-not (Test-Path .venv)) { & ([scriptblock]::Create("$py -m venv .venv")) }
& .venv\Scripts\python.exe -m pip install --upgrade pip
& .venv\Scripts\python.exe -m pip install -e ".[all]"
if (-not $NoModel) {
    & .venv\Scripts\engram.exe models download
}
& .venv\Scripts\engram.exe doctor
Write-Host ""
Write-Host "Done. Start ENGRAM with:  .venv\Scripts\engram serve --open"
