; ProjectZen - Windows installer (Inno Setup 6)
;
; Build:   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\ProjectZen.iss
; Output:  installer\dist\ProjectZen-Setup.exe
;
; Design notes
; ------------
; PrivilegesRequired=lowest and a %LOCALAPPDATA% target are deliberate. Installing
; to Program Files would demand an admin prompt on every corporate laptop AND leave
; the app in a directory it cannot write to. ProjectZen keeps its database and
; generated documents in per-user app data (middleware/paths.py), so a per-user
; install needs no elevation at all.
;
; The payload is assembled by installer\build.ps1 into installer\payload\ first.

#define AppName        "ProjectZen"
#define AppVersion     "1.0.0"
#define AppPublisher   "Accenture"
#define AppExeName     "Start-ProjectZen.bat"

[Setup]
AppId={{8E2F4C1A-9B3D-4E7A-8C5F-2D6A1B0E9C34}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=no
PrivilegesRequired=lowest
OutputDir=dist
OutputBaseFilename=ProjectZen-Setup
SetupIconFile=projectzen_z.ico
UninstallDisplayIcon={app}\projectzen_z.ico
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
; ~200 MB unpacked, dominated by the Claude CLI bundled inside claude-agent-sdk.
DiskSpanning=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
; Everything staged by build.ps1. recursesubdirs keeps middleware\seed (the bundled
; reference documents) and python\ intact.
Source: "payload\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "projectzen_z.ico"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}";           Filename: "{app}\{#AppExeName}"; IconFilename: "{app}\projectzen_z.ico"; WorkingDir: "{app}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}";     Filename: "{app}\{#AppExeName}"; IconFilename: "{app}\projectzen_z.ico"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Start {#AppName} now"; Flags: postinstall nowait skipifsilent

[UninstallDelete]
; Python writes bytecode caches after install; remove them so the folder goes cleanly.
Type: filesandordirs; Name: "{app}\middleware\__pycache__"
Type: filesandordirs; Name: "{app}\middleware\quality\__pycache__"
Type: filesandordirs; Name: "{app}\python\Lib\site-packages"

[Messages]
; The uninstaller intentionally leaves %LOCALAPPDATA%\ProjectZen alone - that is the
; user's generated documents and database, not program files.
ConfirmUninstall=Remove {#AppName}?%n%nYour generated documents and settings in your user folder will be kept.
