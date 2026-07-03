$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    python -m venv .venv
}

& $python -m pip install --upgrade pip
& $python -m pip install -r requirements-dev.txt
& $python -m pytest tests -q

Remove-Item -LiteralPath (Join-Path $root "build") -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath (Join-Path $root "dist") -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath (Join-Path $root "CodexRunwayLite.spec") -Force -ErrorAction SilentlyContinue

& $python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name CodexRunwayLite `
    run.py

Write-Host "Built: $root\dist\CodexRunwayLite.exe"
