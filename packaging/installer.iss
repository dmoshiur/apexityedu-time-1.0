; ApexityEdu Smart Attendance Bridge — Inno Setup 6 script.
; Compiled by packaging/build.ps1 (or GitHub Actions) AFTER PyInstaller.
;
; Output:  dist\ApexityEduBridge-Setup-1.0.0.exe

#define MyAppName      "ApexityEdu Smart Attendance Bridge"
#define MyAppShort     "ApexityEdu Bridge"
#define MyAppVersion   "1.0.3"
#define MyAppPublisher "ApexityEdu"
#define MyAppURL       "https://apexityedu.com"
#define MyAppExeName   "ApexityEduBridge.exe"

#ifndef SourceDir
  #define SourceDir "..\dist\ApexityEduBridge"
#endif
#ifndef OutputDir
  #define OutputDir "..\dist"
#endif
#ifndef BrandingDir
  #define BrandingDir "..\branding"
#endif

[Setup]
AppId={{8F3C1A2E-7B54-4D91-9E6A-A1B2C3D4E5F6}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={autopf}\ApexityEdu\Attendance Bridge
DefaultGroupName=ApexityEdu
DisableProgramGroupPage=yes
LicenseFile={#BrandingDir}\..\LICENSE.txt
OutputDir={#OutputDir}
OutputBaseFilename=ApexityEduBridge-Setup-{#MyAppVersion}
SetupIconFile={#BrandingDir}\app-icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
WizardImageFile={#BrandingDir}\installer-sidebar.bmp
WizardSmallImageFile={#BrandingDir}\installer-header.bmp
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
MinVersion=10.0
CloseApplications=yes
RestartApplications=no
SetupLogging=yes
UsePreviousAppDir=yes
DisableWelcomePage=no
VersionInfoVersion={#MyAppVersion}
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription={#MyAppName} Setup
VersionInfoProductName={#MyAppName}
VersionInfoProductVersion={#MyAppVersion}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"
Name: "autostart"; Description: "Start the bridge automatically when I sign in to Windows"; GroupDescription: "Startup:"; Flags: checkedonce

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#BrandingDir}\..\LICENSE.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppShort}"; Filename: "{app}\{#MyAppExeName}"; Comment: "Open ApexityEdu Attendance Bridge"
Name: "{group}\Uninstall {#MyAppShort}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppShort}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon; Comment: "ApexityEdu Smart Attendance Bridge"

[Dirs]
Name: "{commonappdata}\ApexityEduBridge"; Permissions: users-modify
Name: "{commonappdata}\ApexityEduBridge\logs"; Permissions: users-modify
Name: "{commonappdata}\ApexityEduBridge\backups"; Permissions: users-modify

[Run]
; Prepare data dir + SQLite, then register Task Scheduler auto-start.
Filename: "{app}\{#MyAppExeName}"; Parameters: "init-db"; StatusMsg: "Preparing the local database…"; Flags: runhidden waituntilterminated
Filename: "{app}\{#MyAppExeName}"; Parameters: "service install"; StatusMsg: "Registering Windows auto-start…"; Flags: runhidden waituntilterminated; Tasks: autostart
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppShort} now"; Flags: nowait postinstall skipifsilent
Filename: "{app}\{#MyAppExeName}"; Flags: nowait; Check: IsUpdate

[UninstallRun]
Filename: "{app}\{#MyAppExeName}"; Parameters: "service stop"; Flags: runhidden waituntilterminated; RunOnceId: "StopBridge"
Filename: "{app}\{#MyAppExeName}"; Parameters: "service uninstall"; Flags: runhidden waituntilterminated; RunOnceId: "UnregTask"
Filename: "{cmd}"; Parameters: "/C taskkill /IM {#MyAppExeName} /F"; Flags: runhidden waituntilterminated; RunOnceId: "KillExe"

[Code]
function IsUpdate(): Boolean;
begin
  Result := ExpandConstant('{param:UPDATE|0}') = '1';
end;

function InitializeSetup(): Boolean;
begin
  Result := True;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  DataDir, EnvFile, ExampleFile: String;
begin
  if CurStep = ssPostInstall then
  begin
    DataDir := ExpandConstant('{commonappdata}\ApexityEduBridge');
    EnvFile := DataDir + '\.env';
    ExampleFile := ExpandConstant('{app}\.env.example');
    if (not FileExists(EnvFile)) and FileExists(ExampleFile) then
      FileCopy(ExampleFile, EnvFile, False);
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
  begin
    if MsgBox('Remove attendance data, logs and backups from this PC as well?' + #13#10 +
              '(C:\ProgramData\ApexityEduBridge)' + #13#10#13#10 +
              'Choose No to keep the data for a future reinstall.',
              mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
    begin
      DelTree(ExpandConstant('{commonappdata}\ApexityEduBridge'), True, True, True);
    end;
  end;
end;
