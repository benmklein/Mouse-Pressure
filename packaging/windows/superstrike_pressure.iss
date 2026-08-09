#ifndef MyAppVersion
  #define MyAppVersion "0.1.0-dev"
#endif

#define MyAppName "Superstrike Pressure"
#define MyAppPublisher "Ben Klein"
#define MyAppExeName "SuperstrikePressure.exe"
#define RepositoryRoot AddBackslash(SourcePath) + "..\.."

[Setup]
AppId={{B63841C7-3B13-47CC-A80A-85D44708AF35}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL=https://github.com/benmklein/analog_mouse_pressure
AppSupportURL=https://github.com/benmklein/analog_mouse_pressure/issues
AppUpdatesURL=https://github.com/benmklein/analog_mouse_pressure/releases
DefaultDirName={autopf}\Superstrike Pressure
DefaultGroupName=Superstrike Pressure
DisableProgramGroupPage=yes
LicenseFile={#RepositoryRoot}\LICENSE
OutputDir={#RepositoryRoot}\dist\installer
OutputBaseFilename=SuperstrikePressure-{#MyAppVersion}-Setup
SetupIconFile={#RepositoryRoot}\src\superstrike_pressure\assets\lucide_mouse.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=commandline
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Types]
Name: "full"; Description: "Application and Krita 5.3.3 integration"
Name: "compact"; Description: "Application only"
Name: "custom"; Description: "Custom installation"; Flags: iscustom

[Components]
Name: "application"; Description: "Superstrike Pressure application"; Types: full compact custom; Flags: fixed
Name: "krita"; Description: "Superstrike Raster Ink tool for Krita 5.3.3"; Types: full

[Files]
Source: "{#RepositoryRoot}\dist\windows\SuperstrikePressure\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; Components: application
Source: "{#RepositoryRoot}\dist\krita\5.3.3\*"; DestDir: "{app}\integrations\krita\5.3.3"; Flags: ignoreversion recursesubdirs createallsubdirs; Components: krita
Source: "{#RepositoryRoot}\scripts\install_krita_raster_ink.ps1"; DestDir: "{app}\integrations\krita"; Flags: ignoreversion; Components: krita
Source: "{#RepositoryRoot}\scripts\install_krita_release.ps1"; DestDir: "{app}\integrations\krita"; Flags: ignoreversion; Components: krita
Source: "{#RepositoryRoot}\LICENSE"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\Superstrike Pressure"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\Superstrike Pressure"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Run]
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\integrations\krita\install_krita_release.ps1"" -PayloadRoot ""{app}\integrations\krita"""; StatusMsg: "Installing the Krita integration..."; Flags: runhidden waituntilterminated; Components: krita
Filename: "{app}\{#MyAppExeName}"; Description: "Launch Superstrike Pressure"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\integrations\krita\install_krita_release.ps1"" -PayloadRoot ""{app}\integrations\krita"" -Uninstall"; Flags: runhidden waituntilterminated; RunOnceId: "RemoveSuperstrikeKrita"; Components: krita

[Code]
function VMultiDetected(): Boolean;
begin
  Result :=
    RegKeyExists(HKLM64, 'SYSTEM\CurrentControlSet\Enum\HID\VID_00FF&PID_BACC') or
    RegKeyExists(HKLM64, 'SYSTEM\CurrentControlSet\Enum\HID\VID_00FF&PID_CAFE');
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and not WizardSilent() and not VMultiDetected() then
    MsgBox(
      'A compatible VMulti virtual tablet was not detected.' + #13#10 + #13#10 +
      'Superstrike Pressure will use its synthetic Windows Ink fallback. ' +
      'This installer does not install a third-party tablet driver.',
      mbInformation,
      MB_OK
    );
end;
