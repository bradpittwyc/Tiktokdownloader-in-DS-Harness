#define AppName "TikTok 下载器"
#define AppVersion "1.0.1"
#define AppPublisher "TikTokBatchMVP"
#define AppExeName "TikTokBatchMVP.exe"

[Setup]
AppId={{A9E58468-D9D8-4D62-98A2-64C71A1B34C8}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\TikTokBatchMVP
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=..\release
OutputBaseFilename=TikTokBatchMVP-Setup-1.0.1
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#AppExeName}

[Files]
Source: "..\outputs\TikTokBatchMVP\TikTokBatchMVP.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\outputs\TikTokBatchMVP\README.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加图标："; Flags: unchecked

[Run]
Filename: "{app}\{#AppExeName}"; Description: "启动 {#AppName}"; Flags: nowait postinstall skipifsilent
