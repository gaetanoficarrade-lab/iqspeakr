# ============================================================
#  IQspeakr — Windows Build Script (PowerShell)
#  Baut eine .exe mit PyInstaller und legt sie in dist\ ab.
# ============================================================
#
# Voraussetzungen:
#   - Python 3.10+ installiert und im PATH
#   - ffmpeg.exe im PATH ODER im Verzeichnis `bin\` neben dieser ps1
#   - Internet fuer pip + Whisper-Modell-Download beim ersten Start
#
# Ausfuehren (PowerShell im Projekt-Ordner):
#   .\build_exe.ps1
#
# Falls PowerShell-Execution-Policy blockt (typisch bei frischem
# Windows), einmalig setzen:
#   Set-ExecutionPolicy -Scope CurrentUser RemoteSigned

$ErrorActionPreference = 'Stop'

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectDir

Write-Host "[1/4] Virtuelle Umgebung anlegen..." -ForegroundColor Cyan
if (-Not (Test-Path ".venv")) {
    python -m venv .venv
}
$py = ".\.venv\Scripts\python.exe"
$pip = ".\.venv\Scripts\pip.exe"

Write-Host "[2/4] Dependencies installieren..." -ForegroundColor Cyan
& $pip install --upgrade pip | Out-Null
& $pip install -r requirements.txt
& $pip install pyinstaller

Write-Host "[3/4] Icon pruefen..." -ForegroundColor Cyan
$IconArg = ""
if (Test-Path "icon.ico") {
    $IconArg = "--icon=icon.ico"
    Write-Host "  gefunden: icon.ico" -ForegroundColor DarkGray
} else {
    Write-Host "  kein icon.ico — .exe bekommt Default-Icon" -ForegroundColor Yellow
}

Write-Host "[4/4] PyInstaller..." -ForegroundColor Cyan
if (Test-Path "dist") { Remove-Item -Recurse -Force "dist" }
if (Test-Path "build") { Remove-Item -Recurse -Force "build" }

# --windowed unterdrueckt das schwarze Konsolenfenster (Systemtray-App).
# --onefile packt alles in eine einzige IQspeakr.exe.
# --add-data bindet die Default-Config mit, falls noch keine existiert
#   (beim ersten Start wird sie ins User-Verzeichnis kopiert).
$args = @(
    "--name", "IQspeakr",
    "--windowed",
    "--onefile",
    "--noconfirm",
    "--clean",
    "--collect-all", "whisper",
    "--collect-submodules", "pystray",
    "--collect-submodules", "pynput",
    "--add-data", "config.json;."
)
if ($IconArg) { $args += "--icon=icon.ico" }
$args += "app.py"

& ".\.venv\Scripts\pyinstaller.exe" @args

Write-Host ""
Write-Host "============================================" -ForegroundColor Green
Write-Host "  IQspeakr.exe ist bereit:" -ForegroundColor Green
Write-Host "  $ProjectDir\dist\IQspeakr.exe" -ForegroundColor Green
Write-Host "============================================" -ForegroundColor Green
Write-Host ""
Write-Host "Naechster Schritt: dist\IQspeakr.exe direkt starten oder" -ForegroundColor DarkGray
Write-Host "in den Windows-Autostart-Ordner legen:" -ForegroundColor DarkGray
Write-Host "  shell:startup" -ForegroundColor DarkGray
