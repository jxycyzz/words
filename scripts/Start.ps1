[CmdletBinding()]
param([int]$Port = 8765, [switch]$Build)
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw 'Run scripts\Setup.ps1 first to install this project''s independent dependencies.'
}
Push-Location $projectRoot
try {
    if ($Build -or -not (Test-Path -LiteralPath 'frontend\dist\index.html')) {
        Push-Location (Join-Path $projectRoot 'frontend')
        try {
            & npm.cmd run build
            if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed.' }
        } finally { Pop-Location }
    }
    Write-Host "WordLearner B/S: http://127.0.0.1:$Port"
    Write-Host 'Local-only development version. Press Ctrl+C to stop.'
    & $python -m uvicorn backend.main:app --host 127.0.0.1 --port $Port --workers 1
    if ($LASTEXITCODE -ne 0) { throw 'Server stopped with an error.' }
} finally { Pop-Location }
