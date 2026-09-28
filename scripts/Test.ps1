[CmdletBinding()]
param([switch]$Browser)
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
Push-Location $projectRoot
try {
    & '.\.venv\Scripts\python.exe' -m pytest -q
    if ($LASTEXITCODE -ne 0) { throw 'Backend tests failed.' }
    Push-Location frontend
    try {
        & npm.cmd run build
        if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed.' }
        if ($Browser) {
            & npm.cmd run test:e2e
            if ($LASTEXITCODE -ne 0) { throw 'Browser tests failed.' }
            & npx.cmd playwright test -c playwright.layout.config.ts
            if ($LASTEXITCODE -ne 0) { throw 'Game layout tests failed.' }
        }
    } finally { Pop-Location }
} finally { Pop-Location }
