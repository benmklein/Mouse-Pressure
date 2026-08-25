#ifndef MyAppVersion
  #define MyAppVersion "0.4.0-dev"
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
UsePreviousAppDir=yes
DisableDirPage=auto
; The default directory is app-specific. Allow reinstall/upgrade recovery when
; the uninstall registry entry is missing but the application folder remains.
DirExistsWarning=no

[InstallDelete]
; Replace generated payload directories so removed files do not survive upgrades.
Type: filesandordirs; Name: "{app}\_internal"
Type: filesandordirs; Name: "{app}\sandbox"

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

[Code]
const
  UninstallRegistryKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{B63841C7-3B13-47CC-A80A-85D44708AF35}_is1';

var
  ExistingInstallPage: TInputOptionWizardPage;
  ExistingUninstaller: String;
  ExistingVersion: String;
  UninstallLaunched: Boolean;

function FindExistingInstall: Boolean;
begin
  Result :=
    RegQueryStringValue(
      HKCU64, UninstallRegistryKey, 'UninstallString', ExistingUninstaller
    ) and FileExists(RemoveQuotes(ExistingUninstaller));
  if Result then begin
    ExistingUninstaller := RemoveQuotes(ExistingUninstaller);
    if not RegQueryStringValue(
      HKCU64, UninstallRegistryKey, 'DisplayVersion', ExistingVersion
    ) then
      ExistingVersion := 'an earlier version';
  end;
end;

procedure InitializeWizard;
begin
  if WizardSilent or not FindExistingInstall then
    Exit;

  ExistingInstallPage := CreateInputOptionPage(
    wpWelcome,
    'Existing installation',
    'Choose what you want to do.',
    'Mouse Pressure ' + ExistingVersion + ' is already installed.',
    True,
    False
  );
  ExistingInstallPage.Add('Update or reinstall Mouse Pressure {#MyAppVersion}');
  ExistingInstallPage.Add('Uninstall Mouse Pressure ' + ExistingVersion);
  ExistingInstallPage.SelectedValueIndex := 0;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  ResultCode: Integer;
begin
  Result := True;
  if (ExistingInstallPage = nil) or
     (CurPageID <> ExistingInstallPage.ID) or
     (ExistingInstallPage.SelectedValueIndex <> 1) then
    Exit;

  Result := False;
  if Exec(
    ExistingUninstaller,
    '',
    ExtractFileDir(ExistingUninstaller),
    SW_SHOWNORMAL,
    ewNoWait,
    ResultCode
  ) then begin
    UninstallLaunched := True;
    WizardForm.Close;
  end else
    MsgBox('Mouse Pressure could not start its uninstaller.', mbError, MB_OK);
end;

procedure CancelButtonClick(
  CurPageID: Integer; var Cancel, Confirm: Boolean
);
begin
  if UninstallLaunched then
    Confirm := False;
end;
