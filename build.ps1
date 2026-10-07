# Build VerTab into a single executable: dist\VerTab.exe
#
#   powershell -ExecutionPolicy Bypass -File build.ps1
#
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "Installing build requirements..." -ForegroundColor Cyan
python -m pip install --disable-pip-version-check -r requirements-build.txt

Write-Host "Generating icon..." -ForegroundColor Cyan
python tools/make_icon.py

Write-Host "Running PyInstaller..." -ForegroundColor Cyan
python -m PyInstaller --noconfirm --clean VertabNB.spec

$exe = Join-Path $PSScriptRoot "dist\VerTab.exe"
if (Test-Path $exe) {
    Write-Host "Built $exe" -ForegroundColor Green
} else {
    Write-Error "Build finished but dist\VerTab.exe is missing."
}
