; AlphaNode installer for Windows (Inno Setup). Compiled in CI:
;   iscc packaging\AlphaNode.iss   ->  packaging\Output\AlphaNode-Setup.exe
; Expects the built PyInstaller folder packaging\dist\AlphaNode\ and alphanode.ico alongside it.
#define MyAppName "AlphaNode"
#define MyAppVersion "3.0.0"
#define MyAppPublisher "AlphaNode"
#define MyAppExeName "AlphaNode.exe"

[Setup]
AppId={{A1F0DE00-0A11-4E0D-9C33-A1B2C3D4E5F6}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; show the EULA and require acceptance before install (the repo-root LICENSE.txt, also bundled)
LicenseFile=..\LICENSE.txt
OutputDir=Output
OutputBaseFilename=AlphaNode-Setup-3.0v
SetupIconFile=alphanode.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible
PrivilegesRequiredOverridesAllowed=dialog

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; the entire built PyInstaller onedir folder
Source: "dist\AlphaNode\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent
