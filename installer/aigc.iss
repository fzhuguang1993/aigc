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
DefaultDirName={autopf}\{#MyAppName}
; 单文件 exe 没有多组件可装，目录页留默认值即可（要改的用户自己点"浏览"）
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
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
; 装进 Program Files 需要管理员；给"仅当前用户"留一条后路（选它就装到 %LOCALAPPDATA%\Programs）
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=dialog
; Win7 SP1 以下（Qt6 也装不上）不拦，安装时提示即可
MinVersion=6.1sp1
; 托盘常驻意味着"升级时程序大概率正开着"：不问一句就直接覆盖 exe，Windows 会
; 把旧文件留成 .tmp 名字，用户重启后发现两个图标、版本还是旧的
CloseApplications=yes
RestartApplications=no
; 与 core/single_instance.py 的命名互斥体同名：装/卸前先认出正在运行的那个实例
AppMutex=Local\AIGC 工厂

[Languages]
; 官方发行包只内置 Default（英文）；想要中文向导，把 ChineseSimplified.isl 放进
; Inno Setup\Languages\ 后再把下一行的注释去掉（找不到 .isl 会直接编译失败）
Name: "default"; MessagesFile: "compiler:Default.isl"
; Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "cli";     Description: "同时安装命令行版（调试用，会开黑窗口）"; Flags: unchecked
Name: "creator"; Description: "同时安装创作版（内置语音识别模型，包体大得多）"; Flags: unchecked

[Files]
Source: "dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "dist\AIGC视频助手-命令行.exe"; DestDir: "{app}"; Flags: ignoreversion skipifdoesntexist; Tasks: cli
Source: "dist\AIGC视频助手-创作版.exe"; DestDir: "{app}"; Flags: ignoreversion skipifdoesntexist; Tasks: creator

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
; 开机自启走设置页里的开关（写当前用户的注册表 Run）。这里额外给一个"后台启动"
; 快捷方式：想手动挂到启动文件夹、或习惯开机就点开的人，不用先进软件再翻设置
Name: "{autoprograms}\{#MyAppName}（后台启动）"; Filename: "{app}\{#MyAppExeName}"; Parameters: "--minimized"; Comment: "启动后直接收进系统托盘，不弹主窗口"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[Code]
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
