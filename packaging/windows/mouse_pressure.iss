#ifndef MyAppVersion
  #define MyAppVersion "0.1.0-dev"
#endif

#define MyAppName "Mouse Pressure"
#define MyAppPublisher "Ben Klein"
#define MyAppExeName "MousePressure.exe"
#define RepositoryRoot AddBackslash(SourcePath) + "..\.."

[Setup]
AppId={{B63841C7-3B13-47CC-A80A-85D44708AF35}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL=https://github.com/benmklein/analog_mouse_pressure
AppSupportURL=https://github.com/benmklein/analog_mouse_pressure/issues
AppUpdatesURL=https://github.com/benmklein/analog_mouse_pressure/releases
DefaultDirName={autopf}\Mouse Pressure
DefaultGroupName=Mouse Pressure
DisableProgramGroupPage=yes
OutputDir={#RepositoryRoot}\dist\installer
OutputBaseFilename=MousePressure-{#MyAppVersion}-Setup
SetupIconFile={#RepositoryRoot}\src\mouse_pressure\assets\lucide_mouse.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=commandline
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "{#RepositoryRoot}\dist\windows\MousePressure\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#RepositoryRoot}\dist\windows\MousePressureSandbox\*"; DestDir: "{app}\sandbox"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#RepositoryRoot}\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#RepositoryRoot}\LICENSING.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#RepositoryRoot}\THIRD_PARTY_NOTICES.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#RepositoryRoot}\PRIVACY.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#RepositoryRoot}\SECURITY.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#RepositoryRoot}\packaging\legal\*"; DestDir: "{app}\legal"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#RepositoryRoot}\dist\release-metadata\*"; DestDir: "{app}\legal"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#RepositoryRoot}\docs\compatibility.md"; DestDir: "{app}\docs"; Flags: ignoreversion
Source: "{#RepositoryRoot}\docs\recovery.md"; DestDir: "{app}\docs"; Flags: ignoreversion

[Icons]
Name: "{group}\Mouse Pressure"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Pressure Sandbox"; Filename: "{app}\sandbox\MousePressureSandbox.exe"
Name: "{autodesktop}\Mouse Pressure"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch Mouse Pressure"; Flags: nowait postinstall skipifsilent
