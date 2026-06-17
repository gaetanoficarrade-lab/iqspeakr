# ============================================================
#  IQspeakr - Installer Build Script
#  Baut aus dem PyInstaller-Output einen Setup.exe-Installer
#  via Inno Setup. Ergebnis: dist\IQspeakr-Setup.exe
# ============================================================
#
# Voraussetzungen:
#   - Inno Setup 6 installiert (winget install JRSoftware.InnoSetup)
#   - .\build_exe.ps1 wurde einmal erfolgreich durchgelaufen
#     (dist\IQspeakr\IQspeakr.exe muss existieren)
#
# Ausfuehren:
#   .\build_installer.ps1
#   .\build_installer.ps1 -Rebuild     # baut zuerst die .exe neu

param(
    [switch]$Rebuild
)

$ErrorActionPreference = 'Stop'

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectDir

# --- ISCC.exe finden ---------------------------------------------------------
$IsccCandidates = @(
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
)
$Iscc = $IsccCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $Iscc) {
    Write-Host "FEHLER: Inno Setup nicht gefunden." -ForegroundColor Red
    Write-Host "  Installieren mit: winget install JRSoftware.InnoSetup" -ForegroundColor Yellow
    exit 1
}
Write-Host "[1/3] Inno Setup gefunden: $Iscc" -ForegroundColor Cyan

# --- PyInstaller-Output vorhanden? ------------------------------------------
$DistExe = Join-Path $ProjectDir "dist\IQspeakr\IQspeakr.exe"
if ($Rebuild -or -not (Test-Path $DistExe)) {
    if ($Rebuild) {
        Write-Host "[2/3] -Rebuild angefordert -> build_exe.ps1 ausfuehren..." -ForegroundColor Cyan
    } else {
        Write-Host "[2/3] dist\IQspeakr\IQspeakr.exe fehlt -> build_exe.ps1 ausfuehren..." -ForegroundColor Yellow
    }
    & "$ProjectDir\build_exe.ps1"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "FEHLER: build_exe.ps1 fehlgeschlagen (Exit $LASTEXITCODE)." -ForegroundColor Red
        exit 1
    }
} else {
    Write-Host "[2/3] dist\IQspeakr\IQspeakr.exe vorhanden - nutze bestehenden Build." -ForegroundColor Cyan
    Write-Host "      Fuer Neubau: .\build_installer.ps1 -Rebuild" -ForegroundColor DarkGray
}

# --- Installer kompilieren ---------------------------------------------------
Write-Host "[3/3] Installer kompilieren..." -ForegroundColor Cyan
& $Iscc "$ProjectDir\installer.iss"
if ($LASTEXITCODE -ne 0) {
    Write-Host "FEHLER: ISCC.exe fehlgeschlagen (Exit $LASTEXITCODE)." -ForegroundColor Red
    exit 1
}

$SetupExe = Get-ChildItem (Join-Path $ProjectDir "dist") -Filter "IQspeakr-Setup-*.exe" | Sort-Object LastWriteTime -Descending | Select-Object -First 1 -ExpandProperty FullName
if (Test-Path $SetupExe) {
    $size = [math]::Round((Get-Item $SetupExe).Length / 1MB, 1)
    Write-Host ""
    Write-Host "============================================" -ForegroundColor Green
    Write-Host "  Installer fertig:" -ForegroundColor Green
    Write-Host "  $SetupExe" -ForegroundColor Green
    Write-Host "  Groesse: $size MB" -ForegroundColor Green
    Write-Host "============================================" -ForegroundColor Green
    Write-Host ""
    Write-Host "Weitergabe: diese eine Datei verschicken." -ForegroundColor DarkGray
    Write-Host "Empfaenger: doppelklicken, Wizard durchlaufen, fertig." -ForegroundColor DarkGray
} else {
    Write-Host "FEHLER: Setup.exe wurde nicht erzeugt." -ForegroundColor Red
    exit 1
}
