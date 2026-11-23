; PixelPress - Windows installer (Inno Setup 6). Built by make_installer_windows.py ("Make Installer (Windows).bat"), which passes:
;   /DAppVersion=1.5.14  /DSourceDir=<PyInstaller output folder PixelPress>  /DOutputDir=<release folder>
;   /DLicenceDir=<Source Code\licenses>  /DPphapLicence=<3rd Party\pphap-licenses.txt>
; Installs to C:\Program Files\Pixel Mosaic\PixelPress (the PixelPress HAP plugin's tools folder,
; C:\Program Files\PixelPress, is separate), adds a Start menu entry, and puts the install folder
; on the system PATH so "pixelpress" works in a terminal (PixelPress.com, the console build).

#ifndef AppVersion
  #error AppVersion not defined - run "Make Installer (Windows).bat"
#endif

[Setup]
AppId={{6F1C9C52-6D2B-4E0B-9C1D-5B0E7A1E3C41}
AppName=PixelPress
AppVersion={#AppVersion}
AppVerName=PixelPress {#AppVersion}
AppPublisher=Pixel Mosaic
DefaultDirName={autopf}\Pixel Mosaic\PixelPress
DefaultGroupName=PixelPress
DisableProgramGroupPage=yes
LicenseFile={#LicenceDir}\THIRD-PARTY-LICENSES.txt
OutputDir={#OutputDir}
OutputBaseFilename=PixelPress-{#AppVersion}-Windows-x64
SetupIconFile=AppIcon.ico
UninstallDisplayIcon={app}\PixelPress.exe
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
ChangesEnvironment=yes
WizardStyle=modern
VersionInfoVersion={#AppVersion}
VersionInfoProductName=PixelPress
VersionInfoCompany=Pixel Mosaic
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#LicenceDir}\THIRD-PARTY-LICENSES.txt"; DestDir: "{app}\Licences"; Flags: ignoreversion
Source: "{#LicenceDir}\LICENSE.txt"; DestDir: "{app}\Licences"; Flags: ignoreversion
Source: "{#PphapLicence}"; DestDir: "{app}\Licences"; Flags: ignoreversion

[Icons]
Name: "{group}\PixelPress"; Filename: "{app}\PixelPress.exe"
Name: "{group}\PixelPress Licences"; Filename: "{app}\Licences"
Name: "{autodesktop}\PixelPress"; Filename: "{app}\PixelPress.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\PixelPress.exe"; Description: "Open PixelPress"; Flags: nowait postinstall skipifsilent

[Code]
const
  EnvKey = 'SYSTEM\CurrentControlSet\Control\Session Manager\Environment';

function PathHas(Paths, Dir: string): Boolean;
begin
  Result := Pos(';' + Uppercase(Dir) + ';', ';' + Uppercase(Paths) + ';') > 0;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Paths: string;
begin
  if CurStep = ssPostInstall then
  begin
    if not RegQueryStringValue(HKLM, EnvKey, 'Path', Paths) then Paths := '';
    if not PathHas(Paths, ExpandConstant('{app}')) then
    begin
      if (Paths <> '') and (Copy(Paths, Length(Paths), 1) <> ';') then Paths := Paths + ';';
      RegWriteExpandStringValue(HKLM, EnvKey, 'Path', Paths + ExpandConstant('{app}'));
    end;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Paths, Dir: string;
  P: Integer;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    if RegQueryStringValue(HKLM, EnvKey, 'Path', Paths) then
    begin
      Dir := ExpandConstant('{app}');
      Paths := ';' + Paths + ';';
      P := Pos(';' + Uppercase(Dir) + ';', Uppercase(Paths));
      if P > 0 then
      begin
        Delete(Paths, P, Length(Dir) + 1);
        Paths := Copy(Paths, 2, Length(Paths) - 2);
        RegWriteExpandStringValue(HKLM, EnvKey, 'Path', Paths);
      end;
    end;
  end;
end;
