; Inno Setup script for Airlog. Built by build_windows.ps1 after PyInstaller:
;   iscc /DAppVersion=1.1.0 packaging\installer.iss
; Output: dist\Airlog-Setup-<version>.exe
;
; Installs to Program Files and registers the "Airlog Recorder" Windows service
; (Airlog.exe --install-service), which records from boot whether or not the UI is open.
; Running a newer Setup over an existing install upgrades it in place.

#ifndef AppVersion
  #define AppVersion "1.1.0"
#endif

[Setup]
AppId={{6F1E4C2A-9B7D-4E21-A3C5-2D8F0B7A41E9}
AppName=Airlog
AppVersion={#AppVersion}
AppVerName=Airlog {#AppVersion}
AppPublisher=Airlog
VersionInfoVersion={#AppVersion}
; Must match INSTALL_DIR in airlog\winservice.py
DefaultDirName={autopf}\Airlog
DisableDirPage=yes
DefaultGroupName=Airlog
DisableProgramGroupPage=yes
UninstallDisplayName=Airlog
UninstallDisplayIcon={app}\Airlog.exe
SetupIconFile=airlog.ico
OutputDir=..\dist
OutputBaseFilename=Airlog-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
WizardStyle=modern
; A Windows service needs an all-users, admin install.
PrivilegesRequired=admin
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[InstallDelete]
; Shortcuts made by a manual "Airlog.exe --install-service"; Setup makes its own below.
Type: files; Name: "{commonprograms}\Airlog.lnk"
Type: files; Name: "{commondesktop}\Airlog.lnk"

[Files]
Source: "..\dist\Airlog\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\Airlog"; Filename: "{app}\Airlog.exe"; Comment: "Open the Airlog recorder"
Name: "{group}\Airlog (classic desktop window)"; Filename: "{app}\Airlog.exe"; Parameters: "--classic"; Comment: "All-in-one window; don't record with it while the service runs"
Name: "{group}\Airlog service log"; Filename: "{commonappdata}\Airlog\logs\service.log"
Name: "{group}\Uninstall Airlog"; Filename: "{uninstallexe}"
Name: "{commondesktop}\Airlog"; Filename: "{app}\Airlog.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Airlog.exe"; Description: "Open Airlog"; Flags: nowait postinstall skipifsilent runasoriginaluser

[UninstallRun]
Filename: "{app}\Airlog.exe"; Parameters: "--uninstall-service"; RunOnceId: "RemoveService"; Flags: waituntilterminated

[UninstallDelete]
; Anything a manual install copied in that Setup didn't. Settings and recordings live elsewhere and are kept.
Type: filesandordirs; Name: "{app}"

[Messages]
FinishedLabel=Airlog is installed and the Airlog Recorder service is running. It records in the background from startup, whether or not the Airlog window is open.%n%nOther computers on your network can use http://<this-pc>:8080/

[Code]
// Upgrade: stop the running service so its files can be replaced.
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Code: Integer;
begin
  Exec('powershell.exe', '-NoProfile -Command "Stop-Service AirlogRecorder -ErrorAction SilentlyContinue"',
       '', SW_HIDE, ewWaitUntilTerminated, Code);
  Sleep(1500);   // let the process exit after the service reports stopped
  Result := '';
end;

// Register (or re-register) and start the service once the files are in place.
procedure CurStepChanged(CurStep: TSetupStep);
var
  Code: Integer;
begin
  if CurStep = ssPostInstall then
  begin
    WizardForm.StatusLabel.Caption := 'Installing and starting the Airlog Recorder service...';
    if not Exec(ExpandConstant('{app}\Airlog.exe'), '--install-service --no-shortcuts', '', SW_HIDE,
                ewWaitUntilTerminated, Code) or (Code <> 0) then
      SuppressibleMsgBox('The Airlog Recorder service could not be installed or started.' + #13#10#13#10 +
                         'Details: ' + ExpandConstant('{commonappdata}\Airlog\logs\install.log'),
                         mbError, MB_OK, IDOK);
  end;
end;
