; NetAnchor-Campus 安装包（Inno Setup 6）
; 版本号由 build\make_installer.ps1 通过 /DAppVersion 注入，不在这里写第二份。

#define AppName "NetAnchor-Campus"
#define AppPublisher "AxiuCN"
#define TrayExe "NetAnchor.Tray.exe"
#define CliExe "NetAnchor.Cli.exe"
#define ServiceName "NetAnchorCampus"
; 固定 GUID：升级与卸载靠它认主，任何后续版本都不得改动
#define AppId "{{F99630C5-8D04-487D-A21D-216BBD46F3C1}"

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={#AppId}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\NetAnchor-Campus
DisableDirPage=auto
DisableProgramGroupPage=yes
DefaultGroupName={#AppName}
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\{#TrayExe}
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Windows 10 1607 及以上（14393）
MinVersion=10.0.14393
OutputDir=..\dist\installer
OutputBaseFilename=NetAnchor-Campus-{#AppVersion}-setup
SetupIconFile=..\src\netanchor\ui\assets\app.ico
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; 关闭由安装器自己处理（CLI prepare 负责停服务与托盘），不要让 Inno 弹重启提示
CloseApplications=no
RestartApplications=no
UsePreviousAppDir=yes
; 简体中文必须是默认语言：LanguageDetectionMethod=none 时取 [Languages] 第一项
LanguageDetectionMethod=none
ShowLanguageDialog=yes
; 将来有代码签名证书时取消注释并接 signtool 配置
;SignTool=signtool

[Languages]
Name: "chinesesimplified"; MessagesFile: "languages\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "..\dist\NetAnchor-Campus\*"; DestDir: "{app}"; \
  Flags: ignoreversion recursesubdirs createallsubdirs

[Tasks]
Name: "autostart"; Description: "开机自动启动托盘（所有本地用户）"; Flags: checkedonce

[Run]
Filename: "{app}\{#TrayExe}"; Description: "立即启动托盘"; \
  Flags: nowait postinstall runasoriginaluser skipifsilent

[Code]
var
  UpgradeMode: Boolean;
  BackupDir: String;
  NeedRollback: Boolean;

function ServiceKeyExists(): Boolean;
begin
  Result := RegKeyExists(HKLM, 'SYSTEM\CurrentControlSet\Services\{#ServiceName}');
end;

function InstalledVersion(): String;
begin
  Result := '';
  RegQueryStringValue(HKLM,
    'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\{#AppId}_is1',
    'DisplayVersion', Result);
end;

function CompareVer(A, B: String): Integer;
var
  Cut, NA, NB: Integer;
begin
  Result := 0;
  while (A <> '') or (B <> '') do
  begin
    Cut := Pos('.', A);
    if Cut > 0 then
    begin
      NA := StrToIntDef(Copy(A, 1, Cut - 1), 0);
      A := Copy(A, Cut + 1, Length(A));
    end
    else
    begin
      NA := StrToIntDef(A, 0);
      A := '';
    end;
    Cut := Pos('.', B);
    if Cut > 0 then
    begin
      NB := StrToIntDef(Copy(B, 1, Cut - 1), 0);
      B := Copy(B, Cut + 1, Length(B));
    end
    else
    begin
      NB := StrToIntDef(B, 0);
      B := '';
    end;
    if NA < NB then
    begin
      Result := -1;
      Exit;
    end;
    if NA > NB then
    begin
      Result := 1;
      Exit;
    end;
  end;
end;

function RunCli(const Params: String; var Code: Integer): Boolean;
var
  Cli: String;
begin
  Cli := ExpandConstant('{app}\{#CliExe}');
  Result := FileExists(Cli) and
    Exec(Cli, Params, ExtractFileDir(Cli), SW_HIDE, ewWaitUntilTerminated, Code);
end;

function InitializeSetup(): Boolean;
var
  Have: String;
begin
  UpgradeMode := ServiceKeyExists();
  Result := True;
  if not UpgradeMode then
    Exit;
  Have := InstalledVersion();
  if (Have <> '') and (CompareVer('{#AppVersion}', Have) < 0) then
    Result := MsgBox('已安装版本 ' + Have + ' 比本安装包（{#AppVersion}）更新。' + #13#10 +
      '继续会降级到旧版本。确定继续吗？', mbConfirmation, MB_YESNO) = IDYES;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Code: Integer;
  Target: String;
begin
  Result := '';
  NeedRollback := False;
  if not UpgradeMode then
    Exit;

  Target := ExpandConstant('{app}');
  if not FileExists(Target + '\{#CliExe}') then
  begin
    Result := '检测到已有安装，但找不到 ' + Target + '\{#CliExe}。' + #13#10 +
      '请先用它执行 uninstall，再重新运行本安装包。';
    Exit;
  end;

  Code := -1;
  if (not RunCli('prepare', Code)) or (Code <> 0) then
  begin
    Result := '升级前清场失败（prepare 退出码 ' + IntToStr(Code) + '），已中止。' + #13#10 +
      '旧版本的文件与服务都没有被改动。';
    Exit;
  end;

  BackupDir := Target + '.previous';
  if DirExists(BackupDir) then
    DelTree(BackupDir, True, True, True);
  if not RenameFile(Target, BackupDir) then
  begin
    Result := '无法把旧安装目录移开（可能仍被占用）：' + Target + #13#10 +
      '请重启后再试；旧版本的文件与服务都没有被改动。';
    Exit;
  end;
  NeedRollback := True;
end;

procedure RollbackToPrevious(Code: Integer);
var
  Target, Params: String;
  Dummy: Integer;
begin
  Target := ExpandConstant('{app}');
  { 先停掉可能已经起来的新服务，再删掉半成品目录 }
  RunCli('prepare', Dummy);
  if DirExists(Target) then
    DelTree(Target, True, True, True);
  RenameFile(BackupDir, Target);
  Params := 'register --dir "' + Target + '"';
  RunCli(Params, Dummy);
end;

function AutostartWanted(): Boolean;
var
  Tasks: String;
begin
  if WizardIsTaskSelected('autostart') then
  begin
    Result := True;
    Exit;
  end;
  { 静默安装不会自动勾选任何任务。此时若用户没有显式给 /TASKS，
    按默认（开）处理——否则 /VERYSILENT 会静默把已有的自启项删掉。 }
  if WizardSilent then
  begin
    Tasks := ExpandConstant('{param:TASKS|}');
    Result := Tasks = '';
    Exit;
  end;
  Result := False;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Code: Integer;
  Params: String;
begin
  if CurStep = ssPostInstall then
  begin
    Params := 'register --dir "' + ExpandConstant('{app}') + '"';
    if not AutostartWanted() then
      Params := Params + ' --no-autostart';

    Code := -1;
    if (not RunCli(Params, Code)) or (Code <> 0) then
    begin
      RollbackToPrevious(Code);
      MsgBox('服务登记失败（register 退出码 ' + IntToStr(Code) + '），已回滚到旧版本。' + #13#10 +
        '请查看 %ProgramData%\NetAnchor-Campus\logs\service.log。', mbError, MB_OK);
      Abort;
    end;
  end;

  if CurStep = ssDone then
  begin
    if DirExists(BackupDir) then
      DelTree(BackupDir, True, True, True);
    NeedRollback := False;
  end;
end;

procedure DeinitializeSetup();
var
  Dummy: Integer;
begin
  { 安装被中断/失败时把旧版本放回去 }
  if NeedRollback and DirExists(BackupDir) then
  begin
    Dummy := -1;
    RunCli('prepare', Dummy);
    if DirExists(ExpandConstant('{app}')) then
      DelTree(ExpandConstant('{app}'), True, True, True);
    RenameFile(BackupDir, ExpandConstant('{app}'));
    RunCli('register --dir "' + ExpandConstant('{app}') + '"', Dummy);
  end;
end;

procedure CurUninstallStepChanged(CurStep: TUninstallStep);
var
  Code: Integer;
  Params: String;
begin
  if CurStep = usUninstall then
  begin
    Params := 'unregister';
    { 默认按钮是"否"：/VERYSILENT 会把 MsgBox 当成选了默认按钮，
      不能让它静默删掉用户的配置与日志 }
    if MsgBox('是否同时删除配置与日志？' + #13#10 +
      '%ProgramData%\NetAnchor-Campus', mbConfirmation,
      MB_YESNO or MB_DEFBUTTON2) = IDYES then
      Params := Params + ' --purge';

    Code := -1;
    if (not RunCli(Params, Code)) or (Code <> 0) then
    begin
      MsgBox('拆除服务失败（unregister 退出码 ' + IntToStr(Code) + '），卸载已中止。' + #13#10 +
        '文件仍保留在 ' + ExpandConstant('{app}') + '，可修正后重试。', mbError, MB_OK);
      Abort;
    end;
  end;
end;