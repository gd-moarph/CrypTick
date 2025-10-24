; installer.iss
#define MyAppName "CrypTick"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "GD aka MOARPH"
#define MyAppEXEName "CrypTick.exe"

[Setup]
AppId={{A9D7A5B0-1B54-4E21-9E2E-CRYPTICK-2025}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppComments=Lightweight always-on-top crypto price ticker with profiles and multi-network support.
AppPublisherURL=https://github.com/gd-moarph/crypTick
AppSupportURL=https://github.com/gd-moarph/crypTick/issues
AppUpdatesURL=https://github.com/gd-moarph/crypTick/releases
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
OutputDir=dist\installer
OutputBaseFilename=CrypTick-Setup
WizardStyle=modern
SetupIconFile=assets\cryptick.ico
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64
Compression=lzma
SolidCompression=yes
CloseApplications=yes
DirExistsWarning=no
DisableDirPage=no
DisableProgramGroupPage=no
DisableWelcomePage=yes
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={app}\CrypTick.exe
AppMutex=crypTickInstallMutex

; Pages
LicenseFile=assets\installer\LICENSE.txt
InfoBeforeFile=assets\installer\about.txt
InfoAfterFile=assets\installer\after.txt

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop icon"; GroupDescription: "Additional icons:"; Flags: unchecked
Name: "autostart"; Description: "Start CrypTick with Windows"; GroupDescription: "Startup:"; Flags: unchecked

[Files]
Source: "dist\CrypTick\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs restartreplace

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppEXEName}"
Name: "{commondesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppEXEName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppEXEName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent

[Registry]
; optional autostart (per-user)
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
    ValueType: string; ValueName: "{#MyAppName}"; ValueData: """{app}\{#MyAppEXEName}"""; \
    Tasks: autostart; Flags: uninsdeletevalue
