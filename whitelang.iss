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
Source: "release_pkg\bin\*"; DestDir: "{app}\bin"; Flags: recursesubdirs createallsubdirs
Source: "release_pkg\std\*"; DestDir: "{app}\std"; Flags: recursesubdirs createallsubdirs
Source: "release_pkg\runtime\*"; DestDir: "{app}\runtime"; Flags: recursesubdirs createallsubdirs

Source: "release_pkg\tools\*"; DestDir: "{app}\tools"; Flags: recursesubdirs createallsubdirs onlyifdoesntexist

[Registry]
Root: HKCU; Subkey: "Environment"; ValueType: string; ValueName: "WL_PATH"; ValueData: "{app}"; Flags: uninsdeletevalue
Root: HKCU; Subkey: "Environment"; ValueType: expandsz; ValueName: "Path"; ValueData: "{olddata};{app}\bin"; Check: NeedsAddPath('{app}\bin')

[Code]
const
  ForceUpdateLLVM = False; // 更新llvm时，要将这个改为True

procedure CurStepChanged(CurStep: TSetupStep);
var
  ToolsDir: string;
begin
  if CurStep = ssInstall then
  begin
    if ForceUpdateLLVM then
    begin
      ToolsDir := ExpandConstant('{app}\tools');
      if DirExists(ToolsDir) then
      begin
        DelTree(ToolsDir, True, True, True);
      end;
    end;
  end;
end;

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