; installer/aigc.iss —— Windows 安装包（Inno Setup 6）
;
; 怎么编：先 build.bat 出 exe，再 build_installer.bat（它负责找 ISCC.exe 与读版本号）。
; 没装 Inno Setup 时只需装一次：  winget install JRSoftware.InnoSetup
; 或 CI 上：                      choco install innosetup -y
;
; 为什么装到 Program Files 还能正常干活：exe 旁边不可写，所以数据家（任务库/日志/
; 成品/素材）落在 %LOCALAPPDATA%\AIGC视频助手，凭证配置落在 %APPDATA%\AIGC视频助手。
; 卸载程序默认保留数据家（那是用户的成品视频），删不删由用户在卸载最后一步决定。
;
; 中文写在带 BOM 的 UTF-8 里，否则英文系统的编译机会把 AppName 编成乱码。

#define MyAppName    "AIGC视频助手"
#define MyAppExeName "AIGC视频助手.exe"
#define MyPublisher  "AIGC 工厂"
; 物理安装文件夹：不管用户在目录页选哪个父文件夹，[Code] 都把文件收进这个名字的子文件夹
#define MyAppFolder  "AIGC瑞士军刀"
; 版本号由 build_installer.bat / CI 用 /DMyAppVersion= 传进来（源头是 core/config.py
; 的 APP_VERSION）。留个兜底值，直接双击 ISCC 编译也不会报错，只是包名写着 0.0.0。
#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif

[Setup]
; AppId 一旦发布就不能改：改了以后旧版本的"升级安装"会装出第二份、卸载也清不掉旧的
AppId={{6B7E4C1A-2F3D-4A58-9C0E-1D2E3F4A5B6C}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyPublisher}
DefaultGroupName={#MyAppName}
DefaultDirName={autopf}\{#MyAppFolder}
; 每次都用上面的默认目录（{autopf}\AIGC瑞士军刀），不让 Inno 复用旧版装过的
; 其它路径（否则旧版本装的是 AIGC视频助手 目录，升级会把新文件夹嵌进去）
UsePreviousAppDir=no
; 单文件 exe 全量代码都在包里；这里“分模块安装”只是让用户勾要启用哪些板块，
; 勾的结果在装完后写进 ProgramData 清单，软件首启据此决定侧栏/扇形/工具区露不露入口。
; 目录页仍关着（单 exe 无多目录可选），组件选择页由下面的 [Components] 自动出现。
DisableProgramGroupPage=yes
DisableWelcomePage=no
SourceDir=..
OutputDir=dist
OutputBaseFilename={#MyAppName}-Setup-{#MyAppVersion}
SetupIconFile=assets\app.ico
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; 装进 Program Files 需要管理员；给"仅当前用户"留一条后路（选它就装到 %LOCALAPPDATA%\Programs）
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=dialog
; Win7 SP1 以下（Qt6 也装不上）不拦，安装时提示即可
MinVersion=6.1sp1
; 托盘常驻意味着"升级时程序大概率正开着"：不问一句就直接覆盖 exe，Windows 会
; 把旧文件留成 .tmp 名字，用户重启后发现两个图标、版本还是旧的
CloseApplications=yes
RestartApplications=no
; 升级时旧 exe 常被运行中的托盘进程占用（code 5 拒绝访问）：除了 CloseApplications 询问，
; 还在 PrepareToInstall 里按镜像名强杀两个 exe 作兜底（见下方 [Code]）
; 与 core/single_instance.py 的命名互斥体同名：装/卸前先认出正在运行的那个实例
AppMutex=Local\AIGC 工厂

[Languages]
; 官方发行包只内置 Default（英文）；想要中文向导，把 ChineseSimplified.isl 放进
; Inno Setup\Languages\ 后再把下一行的注释去掉（找不到 .isl 会直接编译失败）
Name: "default"; MessagesFile: "compiler:Default.isl"
; Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："
Name: "autostart"; Description: "开机后自动在后台启动"; Flags: unchecked
Name: "creator"; Description: "同时安装创作版"; Flags: unchecked

; 安装类型：默认 full（四块全勾）；用户取消任一勾即转为 custom
[Types]
Name: "full";   Description: "完整安装"
Name: "custom"; Description: "自定义安装"; Flags: iscustom

; 四个功能模块：只是入口开关（代码全在单 exe 里），勾选结果由 [Code] 写进
; {commonappdata}\AIGC视频助手\install.json，供 core/modules.py 首启采纳。
[Components]
Name: "dashboard"; Description: "数据中台：经营看板、多维统计与执行记录"; Types: full custom
Name: "aigc";      Description: "AIGC 板块：任务中心、产品中心、线路负载、爆款拆解、素材工坊"; Types: full custom
Name: "build";     Description: "基建板块：批量基建、素材工坊"; Types: full custom
Name: "tools";     Description: "工具中心：录屏、语音识别、一键发布、本地文件搜索等小工具"; Types: full custom

[Files]
Source: "dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "dist\AIGC视频助手-创作版.exe"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist; Tasks: creator

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
; 开机自启走设置页里的开关（写当前用户的注册表 Run）。这里额外给一个"后台启动"
; 快捷方式：想手动挂到启动文件夹、或习惯开机就点开的人，不用先进软件再翻设置
Name: "{autoprograms}\{#MyAppName}（后台启动）"; Filename: "{app}\{#MyAppExeName}"; Parameters: "--minimized"; Comment: "启动后直接收进系统托盘，不弹主窗口"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Registry]
; 开机自启：勾了「开机自启」任务就写当前用户 Run 键（值名与 gui/tray.py 的 RUN_VALUE 一致，
; 命令行带 --minimized 与运行时自启命令同口径）；卸载时 uninsdeletevalue 自动清理。
; 编译会警告 "admin 但用了 HKCU 区域"：这是故意取舍——自启必须跟着登录的人走，写 HKLM
; 会变成全机所有人开机都拉起来。目标机都是“同用户 UAC 提权”，HKCU 指向的就是交互登录用户。
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
    ValueType: string; ValueName: "AIGC视频助手"; \
    ValueData: """{app}\{#MyAppExeName}"" --minimized"; \
    Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[Code]
var
  OutputDirPage: TInputDirWizardPage;
  ChosenOutputDir: String;

function JsonEscape(const S: String): String;
var
  I: Integer;
  C: String;
begin
  Result := '';
  for I := 1 to Length(S) do
  begin
    C := Copy(S, I, 1);
    // Inno 的 Pascal Script 字符串里反斜杠不是转义符：'\' 就是一个反斜杠、'\\' 是两个
    if C = '\' then Result := Result + '\\'
    else if C = '"' then Result := Result + '\"'
    else Result := Result + C;
  end;
end;

// 判断路径是否已以 Tail 结尾（大小写不敏感）：Inno 的 [Code] 没有 GetFileName，
// 自己用 Copy/Length 取末尾一段比对，用来判断目录页是否已经带上子文件夹名。
function EndsWith(const S, Tail: String): Boolean;
begin
  Result := (Length(S) >= Length(Tail)) and
            (CompareText(Copy(S, Length(S) - Length(Tail) + 1, Length(Tail)), Tail) = 0);
end;

procedure InitializeWizard;
begin
  ChosenOutputDir := '';
  OutputDirPage := CreateInputDirPage(wpSelectComponents,
    '选择成品输出目录', '设置生成视频与素材的默认存放位置',
    '安装程序会把这个目录作为成品视频的输出根目录，之后可在软件「设置」里随时修改。',
    False, '');
  OutputDirPage.Add('输出目录：');
  OutputDirPage.Values[0] := ExpandConstant('{userdocs}\AIGC视频助手成品');
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  D: String;
begin
  if CurPageID = wpSelectDir then
  begin
    // 不管用户在目录页填了哪个父文件夹，都强制收进一个 AIGC瑞士军刀 子文件夹
    D := RemoveBackslashUnlessRoot(WizardForm.DirEdit.Text);
    if not EndsWith(D, '{#MyAppFolder}') then
      WizardForm.DirEdit.Text := AddBackslash(D) + '{#MyAppFolder}';
  end
  else if CurPageID = OutputDirPage.ID then
    ChosenOutputDir := Trim(OutputDirPage.Values[0]);
  Result := True;
end;

// 覆盖安装时旧 exe 常被运行中的托盘进程占用，直接报 code 5「拒绝访问」删不掉旧文件。
// 装前先按镜像名强杀两个 exe 释放文件句柄，再交给 Inno 覆盖。taskkill 经 CreateProcess
// 直接收到 Unicode 参数，中文进程名能正确匹配（不经 cmd 的 chcp，避开 ANSI 乱码坑）。
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Code: Integer;
begin
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM "{#MyAppExeName}"', '',
       SW_HIDE, ewWaitUntilTerminated, Code);
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM "AIGC视频助手-创作版.exe"', '',
       SW_HIDE, ewWaitUntilTerminated, Code);
  Result := '';
end;

// 安装写完后把勾选的功能模块记进机器级清单（ProgramData）：那是“所有用户共用”
// 的位置，装时用管理员写、日后任意用户双击运行都能读到；软件首启据此把启用集
// seed 进用户层（见 core/modules.py）。一个都没勾时写“全开”，免得首启侧栏是空的。
procedure CurStepChanged(CurStep: TSetupStep);
var
  Dir, Mods, Body: String;
begin
  if CurStep <> ssPostInstall then
    Exit;
  Dir := ExpandConstant('{commonappdata}\AIGC视频助手');
  if not ForceDirectories(Dir) then
  begin
    Log('无法创建 ProgramData 目录，跳过模块清单写入');
    Exit;
  end;
  Mods := '';
  if WizardIsComponentSelected('dashboard') then Mods := Mods + '"dashboard",';
  if WizardIsComponentSelected('aigc')      then Mods := Mods + '"aigc",';
  if WizardIsComponentSelected('build')     then Mods := Mods + '"build",';
  if WizardIsComponentSelected('tools')     then Mods := Mods + '"tools",';
  while (Length(Mods) > 0) and (Mods[Length(Mods)] = ',') do
    Delete(Mods, Length(Mods), 1);
  if Mods = '' then
    Mods := '"dashboard","aigc","build","tools"';   // 全没勾：退成全开，不空侧栏
  Body := '{"modules":[' + Mods + ']'
          + ',"output_dir":"' + JsonEscape(ChosenOutputDir) + '"}' + #13#10;
  if not SaveStringToFile(Dir + '\install.json', Body, False) then
    Log('写入 install.json 失败');
end;

// 卸载默认不动数据家：成品视频与历史任务库都在那里，几十 GB 的东西绝不能跟着
// 卸载消失（用户重装还要靠它）。真想清干净就在最后一步选"删除"。
const
  DATA_HOME = 'AIGC视频助手';

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Dir: String;
begin
  if CurUninstallStep <> usPostUninstall then
    Exit;
  Dir := ExpandConstant('{localappdata}\' + DATA_HOME);
  if not DirExists(Dir) then
    Exit;
  if MsgBox('卸载完成。' + #13#10 + #13#10 +
            '数据目录仍保留在：' + #13#10 + Dir + #13#10 +
            '（任务库、日志、成品视频、素材都在这里。重装会自动接着用。）' + #13#10 + #13#10 +
            '要一并删除这些数据吗？此操作不可恢复。',
            mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
    DelTree(Dir, True, True, True);
end;
