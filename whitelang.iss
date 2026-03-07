[Setup]
AppName=WhiteLanguage
AppVersion=0.1
AppPublisher=White Language Team
AppPublisherURL=https://white-lang.org
SetupIconFile=icon.ico
DefaultDirName={localappdata}\Programs\WhiteLanguage
DefaultGroupName=WhiteLanguage
Compression=lzma2/ultra64
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64
ChangesEnvironment=yes
PrivilegesRequired=lowest
Uninstallable=no

[Files]
Source: "release_pkg\bin\*"; DestDir: "{app}\bin"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "release_pkg\std\*"; DestDir: "{app}\std"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "release_pkg\runtime\*"; DestDir: "{app}\runtime"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "release_pkg\tools\*"; DestDir: "{app}\tools"; Flags: ignoreversion recursesubdirs createallsubdirs

[Registry]
; 写入 WL_PATH
Root: HKCU; Subkey: "Environment"; ValueType: string; ValueName: "WL_PATH"; ValueData: "{app}"; Flags: uninsdeletevalue
; 写入 Path
Root: HKCU; Subkey: "Environment"; ValueType: expandsz; ValueName: "Path"; ValueData: "{olddata};{app}\bin"; Check: NeedsAddPath('{app}\bin')

[Code]
function NeedsAddPath(Param: string): boolean;
var
  OrigPath: string;
begin
  if not RegQueryStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', OrigPath) then
  begin
    Result := True;
    exit;
  end;
  Result := Pos(';' + Param + ';', ';' + OrigPath + ';') = 0;
end;