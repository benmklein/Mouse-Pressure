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
Name: "application"; Description: "Mouse Pressure application"; Types: full compact custom; Flags: fixed
Name: "krita"; Description: "Mouse Pressure Brush for Krita 5.3.3"; Types: full
#ifdef IncludeVMultiDriver
Name: "vmulti"; Description: "Mouse Pressure low-latency virtual tablet driver"; Types: full; MinVersion: 10.0.22000
#endif

[Files]
Source: "{#RepositoryRoot}\dist\windows\MousePressure\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; Components: application
Source: "{#RepositoryRoot}\dist\windows\MousePressureSandbox\*"; DestDir: "{app}\sandbox"; Flags: ignoreversion recursesubdirs createallsubdirs; Components: application
Source: "{#RepositoryRoot}\dist\krita\5.3.3\*"; DestDir: "{app}\integrations\krita\5.3.3"; Flags: ignoreversion recursesubdirs createallsubdirs; Components: krita
Source: "{#RepositoryRoot}\scripts\install_krita_mouse_pressure.ps1"; DestDir: "{app}\integrations\krita"; Flags: ignoreversion; Components: krita
Source: "{#RepositoryRoot}\scripts\install_krita_release.ps1"; DestDir: "{app}\integrations\krita"; Flags: ignoreversion; Components: krita
Source: "{#RepositoryRoot}\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
#ifdef IncludeVMultiDriver
Source: "{#VMultiPayloadDir}\*"; DestDir: "{app}\drivers\vmulti"; Flags: ignoreversion; Components: vmulti; MinVersion: 10.0.22000
#endif

[Icons]
Name: "{group}\Mouse Pressure"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Pressure Sandbox"; Filename: "{app}\sandbox\MousePressureSandbox.exe"
Name: "{autodesktop}\Mouse Pressure"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Run]
#ifdef IncludeVMultiDriver
Filename: "{app}\drivers\vmulti\MousePressureDriverCtl.exe"; Parameters: "install --manifest ""{app}\drivers\vmulti\driver-manifest.json"""; StatusMsg: "Installing the Mouse Pressure virtual tablet driver..."; Flags: runhidden waituntilterminated; Components: vmulti; MinVersion: 10.0.22000
#endif
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\integrations\krita\install_krita_release.ps1"" -PayloadRoot ""{app}\integrations\krita"""; StatusMsg: "Installing the Krita integration..."; Flags: runhidden waituntilterminated; Components: krita
Filename: "{app}\{#MyAppExeName}"; Description: "Launch Mouse Pressure"; Flags: nowait postinstall skipifsilent

[UninstallRun]
#ifdef IncludeVMultiDriver
Filename: "{app}\drivers\vmulti\MousePressureDriverCtl.exe"; Parameters: "remove --manifest ""{app}\drivers\vmulti\driver-manifest.json"""; Flags: runhidden waituntilterminated; RunOnceId: "RemoveMousePressureVMulti"; Components: vmulti; MinVersion: 10.0.22000
#endif
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\integrations\krita\install_krita_release.ps1"" -PayloadRoot ""{app}\integrations\krita"" -Uninstall"; Flags: runhidden waituntilterminated; RunOnceId: "RemoveMousePressureKrita"; Components: krita

[Code]
function VMultiDetected(): Boolean;
begin
  Result :=
    RegKeyExists(HKLM64, 'SYSTEM\CurrentControlSet\Enum\ROOT\MOUSEPRESSUREVMULTI') or
    RegKeyExists(HKLM64, 'SYSTEM\CurrentControlSet\Enum\HID\VID_F055&PID_0001') or
    RegKeyExists(HKLM64, 'SYSTEM\CurrentControlSet\Enum\HID\VID_00FF&PID_BACC') or
    RegKeyExists(HKLM64, 'SYSTEM\CurrentControlSet\Enum\HID\VID_00FF&PID_CAFE');
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and not WizardSilent() and not VMultiDetected()
#ifdef IncludeVMultiDriver
     and not WizardIsComponentSelected('vmulti')
#endif
  then
    MsgBox(
      'A compatible VMulti virtual tablet was not detected.' + #13#10 + #13#10 +
      'Mouse Pressure will use its synthetic Windows Ink fallback. ' +
      'This installer does not install a third-party tablet driver.',
      mbInformation,
      MB_OK
    );
end;
