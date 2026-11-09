; Inno Setup 脚本（可选）
; 先用 packaging/build_windows.ps1 生成 dist\MultiAgentCodegenTeam
; 再用 Inno Setup Compiler 编译本文件

#define MyAppName "Multi-Agent Codegen Team"
#define MyAppVersion "1.1.0"
#define MyAppExeName "MultiAgentCodegenTeam.exe"

[Setup]
AppId={{5E5D6E64-7D7A-4F7E-9B4D-7A5D6A3C2B11}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
DefaultDirName={autopf}\MultiAgentCodegenTeam
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\..\dist\installer
OutputBaseFilename=MultiAgentCodegenTeam-Setup
Compression=lzma
SolidCompression=yes
WizardStyle=modern
SetupIconFile=..\..\desktop\resources\icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}

[Files]
Source: "..\..\dist\MultiAgentCodegenTeam\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; Flags: unchecked

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent
