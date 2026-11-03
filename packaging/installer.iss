; Inno Setup 6 脚本：把 dist\OceanTravelAgent 打成安装包。
;
; 用法（在装了 Inno Setup 的机器上）：
;   iscc /DMyAppVersion=0.1.0 /DSourceDir=..\dist\OceanTravelAgent packaging\installer.iss
; 或者直接 `python scripts/build_desktop.py --installer`，参数会自动带上。
;
; **装在用户目录，不要 Program Files**
;
; 程序会把 config.json、grants.json、data/、logs/ 写在 exe 旁边。装进
; Program Files 之后那层目录不可写，用户换 key、授权目录、会话落盘全部失效，
; 而且不会报错——只是「设置不生效」。PrivilegesRequired=lowest 配合
; {autopf}，非管理员安装时自动落到 %LOCALAPPDATA%\Programs，写权限天然具备，
; 也省掉一次 UAC。

#define MyAppName "Ocean 旅行规划"
#define MyAppExeName "OceanTravelAgent.exe"

#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\OceanTravelAgent"
#endif

[Setup]
AppId={{8F3A6C21-4B7E-4C5A-9E10-6D2B7C9A1F30}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=Ocean
DefaultDirName={autopf}\OceanTravelAgent
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=OceanTravelAgent-{#MyAppVersion}-setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
; 中文语言包是 Inno 的非官方翻译，装在编译器 Languages 目录下才存在
#if FileExists(AddBackslash(CompilerPath) + "Languages\ChineseSimplified.isl")
[Languages]
Name: "cn"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
#endif

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"

[Files]
; 用户数据不随安装包走：data/ 是会话，config.json 是他的 key，
; grants.json 是他授权过的目录。升级覆盖安装时必须原样保留。
Source: "{#SourceDir}\*"; DestDir: "{app}"; \
    Excludes: "\data\*,\logs\*,\config.json,\grants.json,使用说明.txt"; \
    Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "立即运行 {#MyAppName}"; \
    Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 只清掉程序自己生成的日志。data/ 和 config.json 一律留着——
; 「卸载软件顺手删掉用户三个月前的行程」是不可接受的。
Type: filesandordirs; Name: "{app}\logs"
