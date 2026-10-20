; Inno Setup script for DualLink. Built by GitHub Actions (see .github/workflows/build.yml):
;   ISCC.exe /DAppVersion=1.6.0 installer\DualLink.iss
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{8E5B6D2C-6C1F-4C8B-9B0E-5D3B2A1F0C77}
AppName=DualLink
AppVersion={#AppVersion}
AppVerName=DualLink {#AppVersion}
AppPublisher=Rihards
DefaultDirName={localappdata}\Programs\DualLink
DefaultGroupName=DualLink
DisableProgramGroupPage=yes
; installs for the current user only - no administrator prompt
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=DualLink-Setup-{#AppVersion}
SetupIconFile=..\build\icon.ico
UninstallDisplayIcon={app}\DualLink.exe
UninstallDisplayName=DualLink
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; close a running DualLink before replacing it during an update
CloseApplications=force
RestartApplications=no

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\DualLink.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\DualLink"; Filename: "{app}\DualLink.exe"
Name: "{autodesktop}\DualLink"; Filename: "{app}\DualLink.exe"; Tasks: desktopicon

[Registry]
; "Start with Windows" is set by the app itself; make sure uninstalling removes it
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: none; ValueName: "DualLink"; Flags: uninsdeletevalue

[Run]
Filename: "{app}\DualLink.exe"; Description: "{cm:LaunchProgram,DualLink}"; Flags: nowait postinstall skipifsilent
; in-app updates run this installer silently with /update=1 - start the new version afterwards
Filename: "{app}\DualLink.exe"; Parameters: "--updated"; Flags: nowait; Check: IsInAppUpdate

[UninstallRun]
; stop DualLink, then give qBittorrent its own settings back if DualLink was on
Filename: "{sys}\taskkill.exe"; Parameters: "/f /im DualLink.exe"; Flags: runhidden; RunOnceId: "StopDualLink"
Filename: "{app}\DualLink.exe"; Parameters: "--cleanup"; Flags: runhidden waituntilterminated; RunOnceId: "CleanupDualLink"

[Code]
function IsInAppUpdate: Boolean;
begin
  Result := ExpandConstant('{param:update|0}') = '1';
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    SuppressibleMsgBox('DualLink has been removed.' + #13#10 + #13#10 +
      'If you set up Free Download Manager, JDownloader, Internet Download Manager, Firefox or another app ' +
      'to use DualLink, set its proxy back to "No proxy" so it can download again.',
      mbInformation, MB_OK, IDOK);
end;
