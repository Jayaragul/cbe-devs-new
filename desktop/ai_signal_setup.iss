; Inno Setup script for the AI Signal desktop build -- the actual
; "Next > Next > Install > Finish" installer wizard.
;
; Build, in order:
;   1. pyinstaller desktop/AISignal.spec --noconfirm     (produces dist/AISignal/)
;   2. Open this file in Inno Setup (or run ISCC.exe ai_signal_setup.iss)
;
; Output: installer_output/AISignalSetup.exe

#define MyAppName "AI Signal"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "AI Signal"
#define MyAppExeName "AISignal.exe"

[Setup]
; Fixed and never changed across versions -- Inno Setup uses this to
; recognise "this is an update to the same app" rather than a separate
; install, and to find the right uninstall registry entry.
AppId={{66B49AF7-5BAE-488A-B398-0ACEF2094285}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; Per-user install, no admin / UAC prompt: this is a small internal tool
; someone should be able to install on a work laptop without needing
; elevated rights, and it keeps the app's own files in the same
; user-writable territory its data already lives in (see
; ai_signal/live/appdirs.py) rather than Program Files, which an
; ordinary account often cannot write to at all.
PrivilegesRequired=lowest
OutputBaseFilename=AISignalSetup
OutputDir=installer_output
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional icons:"

[Files]
; The whole PyInstaller output folder -- AISignal.exe plus its _internal\
; support files (the dashboard, artwork, watchlist and bundled Python
; runtime). Built by desktop/AISignal.spec; see that file's own docstring.
Source: "..\dist\AISignal\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName} now"; Flags: nowait postinstall skipifsilent
