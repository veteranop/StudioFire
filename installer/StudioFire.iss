; StudioFire — Inno Setup 6 script.
;
; Build steps (see installer\README.md):
;   1. python installer\build_payload.py     (embedded Python + NSSM)
;   2. ISCC.exe installer\StudioFire.iss     -> installer\Output\StudioFire-Setup-<ver>.exe
;
; The installer ships NO emergency filler audio: the engine uses cached
; rotation music from precache\ as its emergency tier (PLAN §10.1).

#define AppName "StudioFire"
#define VerHandle FileOpen("..\VERSION")
#define AppVersion Trim(FileRead(VerHandle))
#expr FileClose(VerHandle)

; --- Authenticode code signing (OPT-IN) ------------------------------------
; An unsigned setup .exe triggers SmartScreen ("Windows protected your PC" /
; "Unknown publisher"), so customer installs look like malware. Signing needs a
; real OV/EV code-signing certificate; this build stays unsigned unless a
; SignTool is defined on the ISCC command line:
;
;   set STUDIOFIRE_SIGN_TOOL=studiofire
;   ISCC /Sstudiofire="signtool.exe sign /fd sha256 /tr http://timestamp.digicert.com /td sha256 /f path\to\cert.pfx /p %CERT_PW% $f" installer\StudioFire.iss
;
; $f is substituted by Inno with each file to sign (the setup .exe and, with
; SignedUninstaller=yes, the generated uninstaller). Details: installer\README.md.
#define SignToolName GetEnv("STUDIOFIRE_SIGN_TOOL")

[Setup]
AppId={{7E1C33F4-5B8A-4B0E-9C3D-2A9A1F0D5B21}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=VeteranOp
DefaultDirName={autopf}\{#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible
OutputDir=Output
OutputBaseFilename=StudioFire-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; brand icon for the installer itself and every shortcut it creates
SetupIconFile=..\web\static\sf_icon_dark.ico
UninstallDisplayIcon={app}\web\static\sf_icon_dark.ico
UninstallDisplayName={#AppName} radio automation

; Signing only kicks in when a SignTool was supplied on the ISCC command line
; (see the #define SignToolName block above); otherwise the build is unchanged.
#if SignToolName != ""
SignTool={#SignToolName}
SignedUninstaller=yes
SignToolRetryCount=3
SignToolMinimumTimeBetween=2000
#endif

[Tasks]
Name: "services"; Description: "Install as auto-restarting Windows services (production on-air PC — starts at boot, self-heals on crash)"; Flags: unchecked
Name: "firewall"; Description: "Open the Windows Firewall port (private and domain networks only) so DJs on your LAN can reach the web GUI"
Name: "desktopicons"; Description: "Put Start StudioFire and Quit StudioFire shortcuts on the desktop"

[Files]
Source: "..\services\*"; DestDir: "{app}\services"; Flags: recursesubdirs ignoreversion; Excludes: "__pycache__\*"
Source: "..\web\*"; DestDir: "{app}\web"; Flags: recursesubdirs ignoreversion
Source: "..\scripts\*.py"; DestDir: "{app}\scripts"; Flags: ignoreversion; Excludes: "__pycache__\*"
Source: "..\scripts\*.bat"; DestDir: "{app}\scripts"; Flags: ignoreversion
Source: "..\scripts\*.ps1"; DestDir: "{app}\scripts"; Flags: ignoreversion
Source: "..\start-all.bat"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\stop-all.bat"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\restart-all.bat"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\healthcheck.bat"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\update.bat"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\requirements.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\VERSION"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\CHANGELOG.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\config\config.example.json"; DestDir: "{app}\config"; Flags: ignoreversion
Source: "..\bin\mpv.exe"; DestDir: "{app}\bin"; Flags: ignoreversion
Source: "payload\nssm.exe"; DestDir: "{app}\bin"; Flags: ignoreversion
Source: "payload\runtime\*"; DestDir: "{app}\runtime"; Flags: recursesubdirs ignoreversion

[Dirs]
; the install folder must be writable by the (non-admin) account the services
; run as: the self-updater (Settings -> Software updates, update.bat) replaces
; code here, and data\ / logs\ live here too. Keep the file list in [Files]
; in sync with MANAGED_* in services\updater.py.
Name: "{app}"; Permissions: users-modify
; runtime state stays LOCAL to the box (never the NAS) — created up front so
; the services can write from second zero
Name: "{app}\assets\emergency"
Name: "{app}\data"
Name: "{app}\logs"
Name: "{app}\precache"
; station data lives OUTSIDE the install folder so an upgrade, a 
; reinstall or an uninstall-with-keep-data never touches it
Name: "{commonappdata}\StudioFire"

[Icons]
Name: "{autoprograms}\{#AppName}\Start {#AppName}"; Filename: "{app}\start-all.bat"; WorkingDir: "{app}"; IconFilename: "{app}\web\static\sf_icon_dark.ico"
Name: "{autoprograms}\{#AppName}\Stop {#AppName}"; Filename: "{app}\stop-all.bat"; WorkingDir: "{app}"; IconFilename: "{app}\web\static\sf_icon_dark.ico"
Name: "{autoprograms}\{#AppName}\Health check"; Filename: "{app}\healthcheck.bat"; WorkingDir: "{app}"; IconFilename: "{app}\web\static\sf_icon_dark.ico"
Name: "{autoprograms}\{#AppName}\Update {#AppName} from GitHub"; Filename: "{app}\update.bat"; WorkingDir: "{app}"; IconFilename: "{app}\web\static\sf_icon_dark.ico"
Name: "{autoprograms}\{#AppName}\{#AppName} web GUI"; Filename: "http://localhost:{code:GetPort}"; IconFilename: "{app}\web\static\sf_icon_dark.ico"
Name: "{commondesktop}\Start StudioFire"; Filename: "{app}\start-all.bat"; WorkingDir: "{app}"; IconFilename: "{app}\web\static\sf_icon_dark.ico"; Tasks: desktopicons
Name: "{commondesktop}\Quit StudioFire"; Filename: "{app}\stop-all.bat"; WorkingDir: "{app}"; IconFilename: "{app}\web\static\sf_icon_dark.ico"; Tasks: desktopicons

[Run]
Filename: "netsh"; Parameters: "advfirewall firewall add rule name=""StudioFire Web"" dir=in action=allow protocol=TCP localport={code:GetPort} profile=private,domain"; Tasks: firewall; Flags: runhidden
Filename: "{cmd}"; Parameters: "/c ""{tmp}\sf-services.cmd"""; Tasks: services; Check: SvcCredsGiven; Flags: runhidden waituntilterminated; StatusMsg: "Registering Windows services..."
Filename: "{cmd}"; Parameters: "/c del /q ""{tmp}\sf-services.cmd"""; Tasks: services; Flags: runhidden
Filename: "{app}\start-all.bat"; Description: "Start StudioFire now"; Flags: postinstall nowait skipifsilent unchecked; Check: not WizardIsTaskSelected('services')
Filename: "http://localhost:{code:GetPort}"; Description: "Open the web GUI (first visit creates the admin login)"; Flags: postinstall shellexec nowait skipifsilent

[UninstallRun]
Filename: "{app}\scripts\remove-services.bat"; RunOnceId: "RemoveServices"; Flags: runhidden waituntilterminated
Filename: "{app}\stop-all.bat"; RunOnceId: "StopAll"; Flags: runhidden waituntilterminated
Filename: "netsh"; Parameters: "advfirewall firewall delete rule name=""StudioFire Web"""; RunOnceId: "Firewall"; Flags: runhidden

[Code]
var
  StationPage: TInputQueryWizardPage;
  SvcPage: TInputQueryWizardPage;
  PortNum: Integer;

procedure InitializeWizard;
begin
  StationPage := CreateInputQueryPage(wpSelectDir,
    'Station setup',
    'Tell StudioFire about this station',
    'These land in config\config.json — you can edit that file later. ' +
    'Everything else (cache, database, logs) lives inside the install folder.');
  StationPage.Add('Station name:', False);
  StationPage.Add('Music library folder (UNC share or mapped drive), e.g. \\NAS\music or D:\Music:', False);
  StationPage.Add('Web GUI port for DJs on your network (1024-65535):', False);
  StationPage.Values[0] := 'My Station';
  StationPage.Values[1] := '';
  StationPage.Values[2] := '8080';

  { The services start before anyone logs in, so they need a real account that
    can read the music library. This used to be asked by the batch file - which
    the installer runs with no console, so setup sat on "Registering Windows
    services..." forever. Ask here, where the prompt is visible. }
  SvcPage := CreateInputQueryPage(wpSelectTasks,
    'Windows service account',
    'Which Windows account should StudioFire run as?',
    'StudioFire runs as Windows services so the station comes back by itself ' +
    'after a reboot. Windows services cannot see your mapped drives or your ' +
    'saved passwords, so they need an account that can open the music library ' +
    'itself. Use the account you use on this PC.');
  SvcPage.Add('Account:', False);
  SvcPage.Add('Password:', True);
  SvcPage.Values[0] := '';
end;

function ConfigPath: String;
begin
  Result := ExpandConstant('{app}\config\config.json');
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  { upgrades keep their existing config — don't re-ask }
  if (SvcPage <> nil) and (PageID = SvcPage.ID) then
  begin
    Result := not WizardIsTaskSelected('services');
    Exit;
  end;
  if (StationPage <> nil) and (PageID = StationPage.ID) then
  begin
    PortNum := StrToIntDef(StationPage.Values[2], 0);
    if (PortNum < 1024) or (PortNum > 65535) then
    begin
      MsgBox('Please enter a port between 1024 and 65535.' + #13#10 + #13#10 +
        'DJs will reach the web GUI at http://this-pc:' + StationPage.Values[2],
        mbError, MB_OK);
      Result := False;
      Exit;
    end;
  end;
  Result := (StationPage <> nil) and (PageID = StationPage.ID)
            and FileExists(ConfigPath);
end;

function JsonSlashes(const S: String): String;
var T: String;
begin
  { forward slashes are valid everywhere StudioFire reads paths, and they
    save us JSON backslash-escaping }
  T := S;
  StringChangeEx(T, '\', '/', True);
  Result := T;
end;

{ Is there a password to register the services with? Without one the batch
  file has nothing to log on with, so the step is skipped rather than retried
  blindly (the station can be pointed at the services later in Settings). }
function SvcCredsGiven: Boolean;
begin
  Result := (SvcPage <> nil) and (Trim(SvcPage.Values[1]) <> '');
end;

function SvcAccount: String;
begin
  Result := Trim(SvcPage.Values[0]);
  if Result = '' then
    Result := '.\' + GetUserNameString;
end;

{ The batch reads SFC_SVCUSER / SFC_SVCPASS from the environment, so the
  password never appears in a command line, an installer log or the Services
  UI. The file lives in the temporary folder and is deleted by the next
  following run entry. }
procedure WriteServicesWrapper;
var
  Lines: TStringList;
begin
  Lines := TStringList.Create;
  try
    Lines.Add('@echo off');
    Lines.Add('set "SFC_NONINTERACTIVE=1"');
    Lines.Add('set "SFC_SVCUSER=' + SvcAccount + '"');
    Lines.Add('set "SFC_SVCPASS=' + SvcPage.Values[1] + '"');
    Lines.Add('call "' + ExpandConstant('{app}\scripts\install-services.bat') + '"');
    Lines.Add('exit /b %ERRORLEVEL%');
    Lines.SaveToFile(ExpandConstant('{tmp}\sf-services.cmd'));
  finally
    Lines.Free;
  end;
end;

{ The web port, in precedence order: what the station's config already says
  (an upgrade keeps the port it was installed with), then the wizard answer,
  then 8080 for a silent install. The firewall rule, the shortcut and the
  config file all use this one function, so they cannot drift apart. }
function GetPort(Param: String): String;
var
  Lines: TStringList;
  Text: String;
  At: Integer;
  Port: String;
begin
  Port := '';
  if FileExists(ConfigPath) then
  begin
    { TStringList rather than LoadStringFromFile: the latter wants an
      AnsiString and this script is Unicode. }
    Lines := TStringList.Create;
    try
      try
        Lines.LoadFromFile(ConfigPath);
        Text := Lines.Text;
      except
        Text := '';
      end;
    finally
      Lines.Free;
    end;
    At := Pos('"bind_port"', Text);
    if At > 0 then
    begin
      Text := Copy(Text, At, 40);
      At := Pos(':', Text);
      if At > 0 then
      begin
        Port := Trim(Copy(Text, At + 1, 40));
        Port := Trim(Copy(Port, 1, Pos(',', Port + ',') - 1));
      end;
    end;
  end;
  if StrToIntDef(Port, 0) > 0 then
    Result := Port
  else if (StationPage <> nil) and (StrToIntDef(StationPage.Values[2], 0) > 0) then
    Result := StationPage.Values[2]
  else
    Result := '8080';
end;

procedure CurStepChanged(CurStep: TSetupStep);
var Cfg: String;
begin
  if (CurStep = ssPostInstall) and not FileExists(ConfigPath) then
  begin
    Cfg :=
      '{' + #13#10 +
      '  "schema_version": 1,' + #13#10 +
      '  "station_name": "' + StationPage.Values[0] + '",' + #13#10 +
      '  "paths": {' + #13#10 +
      '    "nas_music_root": "' + JsonSlashes(StationPage.Values[1]) + '",' + #13#10 +
      '    "path_aliases": {}' + #13#10 +
      '  },' + #13#10 +
      '  "engine": {' + #13#10 +
      '    "audio_device_guid": ""' + #13#10 +
      '  },' + #13#10 +
      '  "core": {' + #13#10 +
      '    "bind_host": "0.0.0.0",' + #13#10 +
      '    "bind_port": ' + GetPort('') + #13#10 +
      '  }' + #13#10 +
      '}' + #13#10;
    SaveStringToFile(ConfigPath, Cfg, False);
  end;
  if (CurStep = ssPostInstall) and WizardIsTaskSelected('services') then
  begin
    if SvcCredsGiven then
      WriteServicesWrapper
    else
      MsgBox('StudioFire was installed, but the Windows services were not ' +
        'registered because no password was given.' + #13#10 + #13#10 +
        'Start StudioFire from the Start menu for now; you can turn on ' +
        '"start at boot" later in Settings.', mbInformation, MB_OK);
  end;
end;


{ Ask before throwing away the station's data. Default is KEEP: a station
  manager uninstalling to reinstall must not lose playlists, users or history. }
function InitializeUninstall(): Boolean;
var
  Answer: Integer;
begin
  Answer := MsgBox('Keep your settings, playlists and logs?' + #13#10 + #13#10 +
    'Yes  - keep them (recommended). Reinstalling StudioFire brings everything ' +
    'back.' + #13#10 +
    'No   - delete them too. Your music files and .lst files are never touched.',
    mbConfirmation, MB_YESNO);
  if Answer = IDNO then
    DelTree(ExpandConstant('{commonappdata}\StudioFire'), True, True, True);
  Result := True;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  { nothing else to clean: the [UninstallRun] entries stop the services, remove
    them and drop the firewall rule before this runs }
end;
