; ============================================================================
;  TikTok 下载器 —— 安装脚本（Inno Setup 6）
;
;  版本号由 installer/build.ps1 从 outputs/TikTokBatchMVP/VERSION 读出，
;  通过 ISCC 的 /DAppVersion= 传进来；下面只是直接编译 .iss 时的兜底值，
;  这样版本号只有一个来源，不会出现安装包和程序各说各话。
; ============================================================================

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

#define AppName "TikTok 下载器"
#define AppExeName "TikTokBatchMVP.exe"
#define AppPublisher "TikTokBatchMVP"
#define AppURL "https://github.com/bradpittwyc/Tiktokdownloader-in-DS-Harness"
#define AppCopyright "Copyright (C) 2026 TikTokBatchMVP"

[Setup]
; AppId 是升级安装的身份证：同一 AppId 才会被识别为"同一个程序的更新"。
; 一旦发布就绝不能改，改了会变成两个互不相干的程序，旧版也卸不掉。
AppId={{A9E58468-D9D8-4D62-98A2-64C71A1B34C8}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}/issues
AppUpdatesURL={#AppURL}/releases
AppCopyright={#AppCopyright}

; 「程序和功能」/「应用和功能」里显示的样子
UninstallDisplayName={#AppName} {#AppVersion}
UninstallDisplayIcon={app}\{#AppExeName}

; setup.exe 自身的文件属性（右键属性 -> 详细信息）
VersionInfoVersion={#AppVersion}
VersionInfoCompany={#AppPublisher}
VersionInfoDescription={#AppName} 安装程序
VersionInfoProductName={#AppName}
VersionInfoProductVersion={#AppVersion}
VersionInfoCopyright={#AppCopyright}

DefaultDirName={autopf}\TikTokBatchMVP
DefaultGroupName={#AppName}
AllowNoIcons=yes
LicenseFile=LICENSE.txt

OutputDir=..\release
OutputBaseFilename=TikTokBatchMVP-Setup-{#AppVersion}
SetupIconFile=..\outputs\TikTokBatchMVP\ui\tiktok-logo.ico

Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
WizardImageFile=wizard-large.bmp
WizardSmallImageFile=wizard-small.bmp
WizardImageStretch=no

; 程序是 64 位、需要 WebView2（Windows 10 起自带安装器）
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0

; 默认装到 Program Files（需要管理员），但允许用户改成"仅为我安装"
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=dialog

; 如果程序还开着，先提示关闭，避免装出一个半新半旧的目录
CloseApplications=yes
RestartApplications=no

SetupLogging=yes

[Languages]
; Inno 自带 29 种语言但没有中文，这份简体中文来自社区维护的翻译
; （文件头有作者与出处），已随仓库一并保存，编译不依赖网络。
Name: "chinese"; MessagesFile: "ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: checkedonce

[Files]
Source: "..\outputs\TikTokBatchMVP\TikTokBatchMVP.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\outputs\TikTokBatchMVP\README.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "LICENSE.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "THIRD-PARTY-NOTICES.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "licenses\*"; DestDir: "{app}\licenses"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(AppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[Code]
const
  WebView2Client = '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';

{ 程序界面依赖 Microsoft Edge WebView2 运行时。Win11 和大部分 Win10 自带，
  但并非绝对。这里只做提醒、不阻止安装 —— 万一检测有误，用户仍能继续。 }
function WebView2Version(): String;
begin
  Result := '';
  if not RegQueryStringValue(HKLM, 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\' + WebView2Client, 'pv', Result) then
    if not RegQueryStringValue(HKLM, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\' + WebView2Client, 'pv', Result) then
      RegQueryStringValue(HKCU, 'Software\Microsoft\EdgeUpdate\Clients\' + WebView2Client, 'pv', Result);
end;

function InitializeSetup(): Boolean;
var
  Version: String;
begin
  Result := True;
  Version := WebView2Version();
  if (Version = '') or (Version = '0.0.0.0') then
    Result := MsgBox('没有检测到 Microsoft Edge WebView2 运行时，程序界面可能无法启动。' + #13#10 + #13#10 +
                     '可以继续安装，但建议先在 Microsoft 官网免费获取 WebView2 运行时。' + #13#10 + #13#10 +
                     '仍要继续安装吗？', mbConfirmation, MB_YESNO) = IDYES;
end;
