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
UninstallDisplayName={#AppName} radio automation

[Tasks]
Name: "services"; Description: "Install as auto-restarting Windows services (production on-air PC — starts at boot, self-heals on crash)"; Flags: unchecked
Name: "firewall"; Description: "Open Windows Firewall port 8080 so DJs on the LAN can reach the web GUI"

[Files]
Source: "..\services\*"; DestDir: "{app}\services"; Flags: recursesubdirs ignoreversion; Excludes: "__pycache__\*"
Source: "..\web\*"; DestDir: "{app}\web"; Flags: recursesubdirs ignoreversion
Source: "..\scripts\*.py"; DestDir: "{app}\scripts"; Flags: ignoreversion; Excludes: "__pycache__\*"
Source: "..\scripts\*.bat"; DestDir: "{app}\scripts"; Flags: ignoreversion
Source: "..\start-all.bat"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\stop-all.bat"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\restart-all.bat"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\healthcheck.bat"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\requirements.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\VERSION"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\CHANGELOG.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\config\config.example.json"; DestDir: "{app}\config"; Flags: ignoreversion
Source: "..\bin\mpv.exe"; DestDir: "{app}\bin"; Flags: ignoreversion
Source: "payload\nssm.exe"; DestDir: "{app}\bin"; Flags: ignoreversion
Source: "payload\runtime\*"; DestDir: "{app}\runtime"; Flags: recursesubdirs ignoreversion

[Dirs]
; runtime state stays LOCAL to the box (never the NAS) — created up front so
; the services can write from second zero
Name: "{app}\assets\emergency"
Name: "{app}\data"
Name: "{app}\logs"
Name: "{app}\precache"

[Icons]
Name: "{autoprograms}\{#AppName}\Start {#AppName}"; Filename: "{app}\start-all.bat"; WorkingDir: "{app}"
Name: "{autoprograms}\{#AppName}\Stop {#AppName}"; Filename: "{app}\stop-all.bat"; WorkingDir: "{app}"
Name: "{autoprograms}\{#AppName}\Health check"; Filename: "{app}\healthcheck.bat"; WorkingDir: "{app}"
Name: "{autoprograms}\{#AppName}\{#AppName} web GUI"; Filename: "http://localhost:8080"

[Run]
Filename: "netsh"; Parameters: "advfirewall firewall add rule name=""StudioFire Web"" dir=in action=allow protocol=TCP localport=8080"; Tasks: firewall; Flags: runhidden
Filename: "{app}\scripts\install-services.bat"; Tasks: services; Flags: runhidden waituntilterminated; StatusMsg: "Registering Windows services..."
Filename: "{app}\start-all.bat"; Description: "Start StudioFire now"; Flags: postinstall nowait skipifsilent unchecked; Check: not WizardIsTaskSelected('services')
Filename: "http://localhost:8080"; Description: "Open the web GUI (first visit creates the admin login)"; Flags: postinstall shellexec nowait skipifsilent

[UninstallRun]
Filename: "{app}\scripts\remove-services.bat"; RunOnceId: "RemoveServices"; Flags: runhidden waituntilterminated
Filename: "{app}\stop-all.bat"; RunOnceId: "StopAll"; Flags: runhidden waituntilterminated
Filename: "netsh"; Parameters: "advfirewall firewall delete rule name=""StudioFire Web"""; RunOnceId: "Firewall"; Flags: runhidden

[Code]
var
  StationPage: TInputQueryWizardPage;

procedure InitializeWizard;
begin
  StationPage := CreateInputQueryPage(wpSelectDir,
    'Station setup',
    'Tell StudioFire about this station',
    'These land in config\config.json — you can edit that file later. ' +
    'Everything else (cache, database, logs) lives inside the install folder.');
  StationPage.Add('Station name:', False);
  StationPage.Add('Music library folder (mapped drive or UNC share), e.g. Z:\Music or \\SYNOLOGY\music:', False);
  StationPage.Values[0] := 'My Station';
  StationPage.Values[1] := '';
end;

function ConfigPath: String;
begin
  Result := ExpandConstant('{app}\config\config.json');
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  { upgrades keep their existing config — don't re-ask }
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
      '    "bind_port": 8080' + #13#10 +
      '  }' + #13#10 +
      '}' + #13#10;
    SaveStringToFile(ConfigPath, Cfg, False);
  end;
end;
