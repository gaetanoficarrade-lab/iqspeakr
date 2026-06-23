; ============================================================
;  IQspeakr - Inno Setup Script
;  Baut aus dem PyInstaller-Output (dist\IQspeakr\) einen
;  einzelnen Setup.exe-Installer mit Start-Menue-Eintrag,
;  optionalem Desktop-Shortcut und optionalem Autostart.
; ============================================================

#define AppName        "IQspeakr"
#define AppVersion     "2026.6.8"
#define AppPublisher   "gaetanoficarra"
#define AppURL         "https://github.com/gaetanoficarrade-lab/iqspeakr"
#define AppExeName     "IQspeakr.exe"
#define SourceDir      "dist\IQspeakr"

[Setup]
; Stabile AppId (NICHT aendern - wird fuer Upgrade/Uninstall benutzt)
AppId={{8B3E5F2A-7C91-4D6E-B8F4-1A2C3D4E5F60}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
AppUpdatesURL={#AppURL}

; Per-User-Install by default (kein UAC-Prompt), aber umschaltbar
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\{#AppExeName}

; Nur 64-Bit (PyInstaller-Build ist x64)
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

OutputDir=dist
OutputBaseFilename=IQspeakr-Setup-{#AppVersion}
Compression=lzma2/ultra64
SolidCompression=yes
LZMAUseSeparateProcess=yes
WizardStyle=modern
SetupIconFile=icon.ico

[Languages]
Name: "german";  MessagesFile: "compiler:Languages\German.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "autostart";   Description: "IQspeakr bei Windows-Start automatisch ausfuehren"; GroupDescription: "Optionen:"; Flags: unchecked

[Files]
Source: "{#SourceDir}\{#AppExeName}";  DestDir: "{app}";           Flags: ignoreversion
Source: "{#SourceDir}\_internal\*";    DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}";       Filename: "{app}\{#AppExeName}"
Name: "{autoprograms}\{#AppName} deinstallieren"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}";        Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Registry]
; Autostart-Eintrag (HKCU -> nur fuer diesen Benutzer)
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
    ValueType: string; ValueName: "{#AppName}"; ValueData: """{app}\{#AppExeName}"""; \
    Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(AppName, '&', '&&')}}"; \
    Flags: nowait postinstall skipifsilent

[UninstallRun]
; Laufende Instanz vor Deinstallation beenden (best effort)
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM {#AppExeName}"; Flags: runhidden; RunOnceId: "KillIQspeakr"

[UninstallDelete]
; Singleton-Lock-Datei mitloeschen (stoert sonst beim Neuinstallieren)
Type: files; Name: "{tmp}\iqspeakr.lock"
