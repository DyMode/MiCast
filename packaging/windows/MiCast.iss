#define AppName "MiCast"
#ifndef AppVersion
  #define AppVersion "0.3.3"
#endif

[Setup]
AppId={{83B8FA25-613D-48DC-88E5-0A747D2A2FCB}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName}
AppPublisher=DyMode
VersionInfoDescription=MiCast 安装程序
MinVersion=10.0
DefaultDirName={autopf}\MiCast
; Update/reinstall must go back to the original directory: skip the directory
; page when a previous install is found, so the user can't install a second
; copy elsewhere and orphan the old one (uninstall only tracks one location).
DisableDirPage=auto
DefaultGroupName=MiCast
OutputDir=..\..\dist\installer
OutputBaseFilename=MiCast-Setup
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\MiCast.exe
SetupIconFile=..\..\assets\icons\windows\setup.ico
WizardSmallImageFile=..\..\assets\icons\windows\wizard-small.bmp,..\..\assets\icons\windows\wizard-small-2x.bmp
; A running MiCast.exe locks {app}\MiCast.exe and would make a same-version
; reinstall fail at the file-copy step. Detect it via the app's singleton
; mutex and offer to close it before reinstalling.
AppMutex=Local\MiCastDesktopSingleton
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "chinesesimplified"; MessagesFile: "languages\ChineseSimplified.isl"

[Files]
Source: "..\..\dist\MiCast.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\MiCast"; Filename: "{app}\MiCast.exe"
Name: "{autodesktop}\MiCast"; Filename: "{app}\MiCast.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式："

[Run]
; runasoriginaluser: the installer is elevated, but MiCast must NOT start
; elevated — an elevated first run would leave WebView2 data, the singleton
; mutex and config files owned by the High-IL context and break later
; normal-integrity launches.
Filename: "{app}\MiCast.exe"; Description: "启动 MiCast"; Flags: nowait postinstall skipifsilent runasoriginaluser

[Code]
var
  RemoveUserData: Boolean;

function IsWebView2Installed(): Boolean;
var
  Version: String;
begin
  // Edge WebView2 Evergreen Runtime registers its version under this GUID.
  Result :=
    (RegQueryStringValue(HKLM, 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Version) and (Version <> '') and (Version <> '0.0.0.0')) or
    (RegQueryStringValue(HKLM, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Version) and (Version <> '') and (Version <> '0.0.0.0')) or
    (RegQueryStringValue(HKCU, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Version) and (Version <> '') and (Version <> '0.0.0.0'));
end;

function InitializeSetup(): Boolean;
var
  ErrorCode: Integer;
begin
  Result := True;
  // pywebview renders the UI with Edge WebView2; without the runtime the app
  // installs fine but its window never opens.
  if not IsWebView2Installed() then begin
    if MsgBox('未检测到 Microsoft Edge WebView2 运行时，MiCast 需要它来显示界面。' + #13#10 + #13#10 +
      '选择“是”打开 WebView2 下载页面（装完请重新运行本安装程序）；' + #13#10 +
      '选择“否”仍然继续安装 MiCast，稍后可自行安装 WebView2。',
      mbConfirmation, MB_YESNO) = IDYES then begin
      ShellExec('open', 'https://go.microsoft.com/fwlink/p/?LinkId=2124703', '', '', SW_SHOWNORMAL, ewNoWait, ErrorCode);
      Result := False;
    end;
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  Result := '';
  // Fallback for a same-version reinstall: the tray app ignores WM_CLOSE,
  // so force-close it here or the {app}\MiCast.exe copy fails on the lock.
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM MiCast.exe', '',
    SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

function InitializeUninstall(): Boolean;
var
  Choice: Integer;
  ResultCode: Integer;
begin
  // Same lock problem on the uninstall path: a running tray app keeps
  // MiCast.exe busy and the uninstaller cannot delete it.
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM MiCast.exe', '',
    SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Choice := MsgBox(
    '是否同时删除 MiCast 设置、音箱配置和米家登录信息？' + #13#10 + #13#10 +
    '选择“否”将保留配置，重新安装后可以继续使用。' + #13#10 + #13#10 +
    '注意：删除操作只影响当前 Windows 账户的数据目录；' +
    '其他账户下的配置需要登录对应账户后手动删除 %APPDATA%\MiCast。',
    mbConfirmation, MB_YESNOCANCEL);
  if Choice = IDCANCEL then begin
    Result := False;
    exit;
  end;
  RemoveUserData := Choice = IDYES;
  Result := True;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then begin
    DelTree(ExpandConstant('{localappdata}\MiCast'), True, True, True);
    if RemoveUserData then
      DelTree(ExpandConstant('{userappdata}\MiCast'), True, True, True);
  end;
end;
