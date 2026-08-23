; ============================================================================
;  DualTouch - Installer (Inno Setup 6)
;  Forced-dark wizard with a Gruvbox background (#282828), Start menu /
;  desktop shortcuts, graceful shutdown of a running instance before
;  installing, and a clean uninstall (stops processes, removes the two
;  scheduled tasks, optional removal of %APPDATA%\DualTouch settings).
;
;  HOW TO COMPILE:
;   1) Build the app first:   cd windows && python build.py
;      (produces dist\DualTouch-windows\)
;   2) Install Inno Setup 6 -> https://jrsoftware.org/isdl.php
;      (6.7+ required for the native dark wizard styling used here)
;   3) Compile this script with Inno Setup (F9), or simply run:
;          python build.py --installer
;      The finished setup exits to  Installer\Output\DualTouch-Setup-x.y.z.exe
;
;  If you change version/paths, edit only the #define block below.
;  The version override comes from build.py:  /DAPP_VERSION=x.y.z
; ============================================================================

#ifndef APP_VERSION
#define APP_VERSION "0.0.0"
#endif

#define MyAppName        "DualTouch"
#define MyAppVersion     APP_VERSION
#define MyAppPublisher   "mateuszklysz"
#define MyAppURL         "https://github.com/mateuszklysz/dualtouch"
#define MyAppExeName     "DualTouch-windows.exe"
#define HelperExeName    "DualTouch-cursor-helper.exe"

; Folder produced by build.py (relative to this .iss).
#define SourceDir        "..\dist\DualTouch-windows"
#define AppIcon          "..\data\images\app_icon.ico"

[Setup]
AppId={{6B2F4C8A-91D3-4E57-A0C8-3D9E1F4B7C2D}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}/releases
VersionInfoVersion={#MyAppVersion}
VersionInfoProductVersion={#MyAppVersion}
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription={#MyAppName} Setup

; Per-machine install into Program Files (the app runs elevated by design,
; so an admin setup matches its nature). The dialog override still allows
; choosing per-user at setup time; under lowest rights {autopf} resolves to
; %LOCALAPPDATA%\Programs instead.
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=dialog
DefaultDirName={autopf}\{#MyAppName}
DisableProgramGroupPage=yes
DefaultGroupName={#MyAppName}
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}

; No Restart Manager prompt - we stop the tray/helper ourselves in code.
CloseApplications=no

; x64 only.
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

; Output.
OutputDir=Output
OutputBaseFilename=DualTouch-Setup-{#MyAppVersion}
SetupIconFile={#AppIcon}
Compression=lzma2/ultra64
SolidCompression=yes

; Wizard look: native dark styling with a Gruvbox page background. The two
; BMPs are generated from data/images/icon.png on the same background and
; committed next to this script.
WizardStyle=modern dark
WizardBackColor=#282828
WizardImageBackColor=#282828
WizardSizePercent=110
ShowLanguageDialog=yes
UsePreviousLanguage=no
DisableWelcomePage=no
WizardImageFile=wizard-banner.bmp
WizardSmallImageFile=wizard-small.bmp

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"
#if FileExists(CompilerPath + "\Languages\Polish.isl")
Name: "pl"; MessagesFile: "compiler:Languages\Polish.isl"
#endif

[CustomMessages]
en.StartMenuShortcut=Create a Start menu shortcut
pl.StartMenuShortcut=Utwrz skrt w menu Start
en.CloseRunningText=DualTouch is still running.%n%nClose it so the installer can continue?
pl.CloseRunningText=DualTouch jest nadal uruchomiony.%n%nZamkn go, aby instalator mg kontynuowa?
en.StillRunning=DualTouch could not be stopped. Close it manually and run the setup again.
pl.StillRunning=Nie udao si zamkn DualTouch. Zamkn je rcznie i uruchom instalator ponownie.
en.RemoveSettings=Also remove settings and logs (%APPDATA%\DualTouch)
pl.RemoveSettings=Usuw take ustawienia i dzienniki (%APPDATA%\DualTouch)

[Tasks]
Name: "startmenu";   Description: "{cm:StartMenuShortcut}";        GroupDescription: "{cm:AdditionalIcons}"
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}";        GroupDescription: "{cm:AdditionalIcons}"; Flags: checkedonce

; NOTE: no "start with Windows" shortcut here on purpose - DualTouch manages
; its own autostart scheduled task from the tray menu (Startup -> Start with
; Windows), which must point at the installed exe non-elevated first.

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
; Start menu (created unless the user unticks the task).
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: startmenu
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"; Tasks: startmenu
; Desktop (optional).
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
; Launch after install (checkbox ticked by default). The exe self-elevates
; on its own, so no runas flags are needed here.
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent

[Code]
// ---------------------------------------------------------------------------
//  Running-instance handling
// ---------------------------------------------------------------------------

function IsProcessRunning(ImageName: String): Boolean;
var
  Locator, Service, Processes: Variant;
begin
  Result := False;
  try
    Locator := CreateOleObject('WbemScripting.SWbemLocator');
    Service := Locator.ConnectServer('.', 'root\CIMV2', '', '');
    Processes := Service.ExecQuery(
      Format('SELECT ProcessId FROM Win32_Process WHERE Name="%s"', [ImageName]));
    Result := Processes.Count > 0;
  except
    // WMI unavailable - assume not running rather than blocking setup.
  end;
end;

function AnyComponentRunning: Boolean;
begin
  Result := IsProcessRunning('{#MyAppExeName}') or IsProcessRunning('{#HelperExeName}');
end;

procedure KillProcesses;
var
  RC: Integer;
begin
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM "{#MyAppExeName}"', '',
    SW_HIDE, ewWaitUntilTerminated, RC);
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM "{#HelperExeName}"', '',
    SW_HIDE, ewWaitUntilTerminated, RC);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if AnyComponentRunning then
  begin
    if MsgBox(ExpandConstant('{cm:CloseRunningText}'), mbConfirmation, MB_YESNO) = IDYES then
    begin
      KillProcesses;
      Sleep(700);
    end;
    if AnyComponentRunning then
      Result := ExpandConstant('{cm:StillRunning}');
  end;
end;

var
  RemoveSettingsWithApp: Boolean;

function InitializeUninstall: Boolean;
var
  OptionsForm: TSetupForm;
  DescriptionLabel: TNewStaticText;
  RemoveSettingsCheck: TNewCheckBox;
  OkButton: TNewButton;
  CancelButton: TNewButton;
begin
  RemoveSettingsWithApp := False;

  OptionsForm := CreateCustomForm(ScaleX(460), ScaleY(150), True, False);
  try
    OptionsForm.Caption := 'Uninstall ' + '{#MyAppName}';
    OptionsForm.Position := poScreenCenter;

    DescriptionLabel := TNewStaticText.Create(OptionsForm);
    DescriptionLabel.Parent := OptionsForm;
    DescriptionLabel.Left := ScaleX(20);
    DescriptionLabel.Top := ScaleX(20);
    DescriptionLabel.Width := ScaleX(420);
    DescriptionLabel.Caption :=
      'The tray app and its cursor-helper daemon will be stopped, and both' + #13#10 +
      'scheduled tasks (cursor helper, optional autostart) will be removed.';

    RemoveSettingsCheck := TNewCheckBox.Create(OptionsForm);
    RemoveSettingsCheck.Parent := OptionsForm;
    RemoveSettingsCheck.Left := ScaleX(20);
    RemoveSettingsCheck.Top := ScaleY(62);
    RemoveSettingsCheck.Width := ScaleX(420);
    RemoveSettingsCheck.Checked := False;
    RemoveSettingsCheck.Caption := ExpandConstant('{cm:RemoveSettings}');

    OkButton := TNewButton.Create(OptionsForm);
    OkButton.Parent := OptionsForm;
    OkButton.Caption := SetupMessage(msgButtonOK);
    OkButton.ModalResult := mrOk;
    OkButton.Default := True;
    OkButton.Left := ScaleX(360);
    OkButton.Top := ScaleY(104);

    CancelButton := TNewButton.Create(OptionsForm);
    CancelButton.Parent := OptionsForm;
    CancelButton.Caption := SetupMessage(msgButtonCancel);
    CancelButton.ModalResult := mrCancel;
    CancelButton.Cancel := True;
    CancelButton.Left := ScaleX(250);
    CancelButton.Top := ScaleY(104);

    Result := OptionsForm.ShowModal = mrOk;
    RemoveSettingsWithApp := Result and RemoveSettingsCheck.Checked;
  finally
    OptionsForm.Free;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  RC: Integer;
begin
  case CurUninstallStep of
    usUninstall:
      // Before files are deleted.
      KillProcesses;
    usPostUninstall:
      begin
        // Both tasks re-register themselves pointing wherever the exe lives,
        // but they must not survive an uninstall.
        Exec(ExpandConstant('{sys}\schtasks.exe'),
          '/Delete /TN "DualTouchCursor" /F', '', SW_HIDE, ewWaitUntilTerminated, RC);
        Exec(ExpandConstant('{sys}\schtasks.exe'),
          '/Delete /TN "DualTouchAutostart" /F', '', SW_HIDE, ewWaitUntilTerminated, RC);
        if RemoveSettingsWithApp then
          DelTree(ExpandConstant('{userappdata}\DualTouch'), True, True, True);
      end;
  end;
end;

// ============================================================================
//  NOTES
//  - Default install is per-machine ({autopf} -> Program Files under
//    PrivilegesRequired=admin); the dialog override allows per-user instead.
//  - The app itself runs elevated by design (it types into elevated games),
//    which is why neither a shortcut nor the launched exe needs extra rights.
//  - Settings live in %APPDATA%\DualTouch and survive uninstall unless the
//    checkbox in the uninstall dialog is ticked.
// ============================================================================
