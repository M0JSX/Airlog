; Inno Setup script for Airlog Viewer. Built by build_windows.ps1:
;   iscc /DAppVersion=1.6.4 packaging\viewer.iss
; Output: dist\Airlog-Viewer-Setup-<version>.exe
;
; A per-user install (no admin prompt): the viewer only opens recorders, it records nothing.

#ifndef AppVersion
  #define AppVersion "1.6.4"
#endif

[Setup]
AppId={{B3D7A1E2-5C48-4F9B-8E61-0A2C7D93F4B1}
AppName=Airlog Viewer
AppVersion={#AppVersion}
AppVerName=Airlog Viewer {#AppVersion}
AppPublisher=Airlog
VersionInfoVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\Airlog Viewer
DisableDirPage=yes
DefaultGroupName=Airlog Viewer
DisableProgramGroupPage=yes
UninstallDisplayName=Airlog Viewer
UninstallDisplayIcon={app}\AirlogViewer.exe
SetupIconFile=viewer.ico
OutputDir=..\dist
OutputBaseFilename=Airlog-Viewer-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
WizardStyle=modern
PrivilegesRequired=lowest
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "..\dist\AirlogViewer\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{userprograms}\Airlog Viewer"; Filename: "{app}\AirlogViewer.exe"; Comment: "Find and open Airlog recorders"
Name: "{userdesktop}\Airlog Viewer"; Filename: "{app}\AirlogViewer.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\AirlogViewer.exe"; Description: "Open Airlog Viewer"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}"
