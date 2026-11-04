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
; **目录钉死在用户目录，不用 {autopf}**
;
; {autopf} 会跟着提权状态走：安装程序被提权时它等于 Program Files，普通双击
; 时等于 %LOCALAPPDATA%\Programs。而本程序必须往 exe 旁边写 config.json、
; grants.json、data/、logs/ ——Program Files 那层不可写，实测普通权限进程
; 建 data 目录直接「访问被拒绝」。后果不是报错，是「设置不生效、会话全丢」，
; 用户只会说「这软件记不住东西」。
;
; 所以固定成 {localappdata}\Programs：不管以什么身份装，落点都可写。
DefaultDirName={localappdata}\Programs\OceanTravelAgent
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=OceanTravelAgent-{#MyAppVersion}-setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; 安装程序自己也用应用图标：用户双击 setup.exe 时看到的是同一个东西
#if FileExists(AddBackslash(SourcePath) + "app.ico")
SetupIconFile=app.ico
#endif
UninstallDisplayIcon={app}\{#MyAppExeName}
PrivilegesRequired=lowest
; 不提供「为所有用户安装」（没有 PrivilegesRequiredOverridesAllowed）：那会
; 提权，而提权后的 {localappdata} 指向的可能是另一个管理员账户，程序就装到
; 别人名下、当前用户找不到入口。这个软件本来就是「一个人的」，装给自己、
; 不需要管理员，也就不用弹 UAC。

[Languages]
; 这份中文语言包随仓库走（packaging/ChineseSimplified.isl，官方非官方翻译）。
; 不依赖构建机的 Inno 目录里有没有它——换台机器打包不用先手动塞文件。
; 没有 [Languages] 时会退回英文界面，中文用户看到英文向导就不像正经软件了。
Name: "cn"; MessagesFile: "ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"

[InstallDelete]
; 装之前先把旧的程序目录整个清掉，只清 _internal 和 exe，用户数据一个不碰。
;
; 原因是实测踩到的：0.1.0 第一版把静态资源打错了层级，_internal\web\index.html
; 是个**目录**。修好之后覆盖安装，Inno 发现目标位置是个同名目录，无法用文件
; 覆盖目录，于是**静默跳过**——用户装完还是坏的，界面上没有任何提示，只能说
; 「还是打不开」。这类「文件与目录同名」的冲突在升级路径里没有别的兜底手段，
; 只能每次安装前把程序目录整个换掉。
;
; 不能只依赖 [Files] 的 ignoreversion：那个只比版本和时间戳，管不了类型冲突。
; data/、logs/、config.json、grants.json 都不在这里，不会被删。
Type: filesandordirs; Name: "{app}\_internal"
Type: files; Name: "{app}\{#MyAppExeName}"

[Files]
; 只装程序本体：exe + _internal。
;
; 不用 `{SourceDir}\*` 配 Excludes——Excludes 的通配符不跨目录分隔符，
; data\ 下面还有 uploads\ 这种子目录，很容易漏掉一两个文件，把上一个用户的
; 会话记录装进下一个用户的机器。这里从来源上就排除了：
; data/、logs/、config.json、grants.json 一个都不在下面两条里。
Source: "{#SourceDir}\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceDir}\_internal\*"; DestDir: "{app}\_internal"; \
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
