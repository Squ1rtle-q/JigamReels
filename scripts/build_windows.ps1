# Сборка ReelsMakerPro.exe (из корня репозитория: .\scripts\build_windows.ps1)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root

if (-not (Test-Path ".\venv\Scripts\python.exe")) {
    Write-Error "Создайте venv и установите зависимости: pip install -r requirements.txt -r requirements-build.txt"
}

& .\venv\Scripts\pip.exe install -r requirements.txt -r requirements-build.txt
& .\venv\Scripts\pyinstaller.exe --noconfirm ReelsMakerPro.spec

Write-Host "Готово: dist\ReelsMakerPro.exe" -ForegroundColor Green
