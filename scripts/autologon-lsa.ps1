<#
    StudioFire - Windows autologon that keeps the password OUT of the registry.

    WHY THIS EXISTS
    ---------------
    A station box has to come back on its own after a power bump or a Windows
    restart, WITH SOUND, and with nobody at the keyboard. Audio only exists in an
    interactive user session, so the box must log itself in (autologon) and the
    stack must start inside that session. (A Windows service runs in session 0,
    which has no audio device - that is why the NSSM route was rejected.)

    The built-in registry way of doing autologon keeps the password in
    CLEARTEXT under:
        HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon\DefaultPassword
    which any account that can read that key can read back. This helper stores
    it as an LSA secret instead (HKLM\SECURITY\Policy\Secrets, restricted to
    SYSTEM/administrators) - exactly what the Sysinternals Autologon tool does,
    but without needing that tool to be copied onto the box first.

    HOW IT WORKS
    ------------
    * At boot, Winlogon reads DefaultUserName / DefaultDomainName / AutoAdminLogon
      from the Winlogon registry key.
    * If the DefaultPassword REG_SZ value is ABSENT, the logon process reads the
      password from the LSA private-data secret named "DefaultPassword".
    * So we: set the three registry values, DELETE any cleartext DefaultPassword
      registry value, and call LsaStorePrivateData("DefaultPassword", <password>).
    * Microsoft documents that LsaOpenPolicy requires the process to run
      "As Administrator". install-autostart.bat runs this helper elevated.

    USAGE (elevated)
    ----------------
        # enable, password prompted securely (recommended)
        powershell -NoProfile -ExecutionPolicy Bypass -File scripts\autologon-lsa.ps1 -User kdpi -Domain KDPI-STREAMING

        # enable, password supplied (or leave -Password off and set the env var
        # STUDIOFIRE_AUTOLOGON_PW so it never lands on a command line)
        powershell ... -File scripts\autologon-lsa.ps1 -User kdpi -Domain . -Password 'secret'

        # report the current state, change nothing, no password needed
        powershell ... -File scripts\autologon-lsa.ps1 -Check

        # turn autologon off and remove the stored secret
        powershell ... -File scripts\autologon-lsa.ps1 -Disable

    NOTES
    -----
    * -Domain: pass the machine name, ".", or the AD domain. "." = the local box.
    * The secret is RECOVERABLE by a local administrator. That is exactly how the
      Sysinternals tool behaves and is documented by Microsoft. Because the box
      then auto-logs-in unattended, treat it as a physically-secured kiosk: no
      browsing, no shared use, BIOS "restore on AC power loss" = On.
    * An interactive console logon as a DIFFERENT user rewrites DefaultUserName
      (Windows tracks the last logged-on user), which breaks autologon. On a
      locked-down kiosk this does not happen; if it ever does, re-run this.
    * Exit codes: 0 ok, 1 error, 3 needs Administrator.
#>
[CmdletBinding()]
param(
    [string]$User,
    [string]$Domain = '.',
    [string]$Password,
    [switch]$Check,
    [switch]$Disable
)

$ErrorActionPreference = 'Stop'

$WinlogonKey = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
$SecretName  = 'DefaultPassword'

# ---------------------------------------------------------------------------
# LSA private-data (secret) store, via advapi32. Pure P/Invoke, no downloads.
# ---------------------------------------------------------------------------
$LsaCode = @'
using System;
using System.Runtime.InteropServices;

namespace StudioFire.LSA {
    public class LsaSecret {
        [StructLayout(LayoutKind.Sequential)]
        private struct LSA_UNICODE_STRING {
            public ushort Length;
            public ushort MaximumLength;
            public IntPtr Buffer;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct LSA_OBJECT_ATTRIBUTES {
            public int Length;
            public IntPtr RootDirectory;
            public LSA_UNICODE_STRING ObjectName;
            public uint Attributes;
            public IntPtr SecurityDescriptor;
            public IntPtr SecurityQualityOfService;
        }

        private const uint POLICY_VIEW_LOCAL_INFORMATION = 0x00000001;
        private const uint POLICY_GET_PRIVATE_INFORMATION = 0x00000004;
        private const uint POLICY_CREATE_SECRET = 0x00000020;

        [DllImport("advapi32.dll", SetLastError = true, PreserveSig = true)]
        private static extern uint LsaOpenPolicy(
            ref LSA_UNICODE_STRING SystemName,
            ref LSA_OBJECT_ATTRIBUTES ObjectAttributes,
            uint DesiredAccess,
            out IntPtr PolicyHandle);

        [DllImport("advapi32.dll", SetLastError = true, PreserveSig = true)]
        private static extern uint LsaStorePrivateData(
            IntPtr PolicyHandle,
            ref LSA_UNICODE_STRING KeyName,
            ref LSA_UNICODE_STRING PrivateData);

        [DllImport("advapi32.dll", SetLastError = true, PreserveSig = true)]
        private static extern uint LsaRetrievePrivateData(
            IntPtr PolicyHandle,
            ref LSA_UNICODE_STRING KeyName,
            out IntPtr PrivateData);

        [DllImport("advapi32.dll", SetLastError = true, PreserveSig = true)]
        private static extern uint LsaClose(IntPtr PolicyHandle);

        [DllImport("advapi32.dll", SetLastError = true, PreserveSig = true)]
        private static extern uint LsaFreeMemory(IntPtr Buffer);

        [DllImport("advapi32.dll", SetLastError = true, PreserveSig = true)]
        private static extern uint LsaNtStatusToWinError(uint Status);

        private static IntPtr Open(uint access) {
            LSA_OBJECT_ATTRIBUTES oa = new LSA_OBJECT_ATTRIBUTES();
            LSA_UNICODE_STRING system = new LSA_UNICODE_STRING();
            IntPtr handle;
            uint status = LsaOpenPolicy(ref system, ref oa, access, out handle);
            uint err = LsaNtStatusToWinError(status);
            if (err != 0) {
                throw new Exception("LsaOpenPolicy failed (win error " + err + ")");
            }
            return handle;
        }

        private static LSA_UNICODE_STRING Make(string s) {
            LSA_UNICODE_STRING u = new LSA_UNICODE_STRING();
            if (s == null) {
                u.Buffer = IntPtr.Zero;
                u.Length = 0;
                u.MaximumLength = 0;
            } else {
                u.Buffer = Marshal.StringToHGlobalUni(s);
                u.Length = (ushort)(s.Length * 2);
                u.MaximumLength = (ushort)((s.Length + 1) * 2);
            }
            return u;
        }

        // value == null  =>  delete the secret.
        public static void Set(string key, string value) {
            IntPtr handle = Open(POLICY_CREATE_SECRET);
            try {
                LSA_UNICODE_STRING k = Make(key);
                LSA_UNICODE_STRING v = Make(value);
                uint status = LsaStorePrivateData(handle, ref k, ref v);
                uint err = LsaNtStatusToWinError(status);
                if (err != 0) {
                    throw new Exception("LsaStorePrivateData failed (win error " + err + ")");
                }
            } finally {
                LsaClose(handle);
            }
        }

        // Returns a description of the secret, NEVER its value: "absent",
        // "present (N chars)", or "unreadable (...)". A check must not be able
        // to leak the password, so the value is never converted to a string.
        public static string Info(string key) {
            IntPtr handle = Open(POLICY_GET_PRIVATE_INFORMATION);
            IntPtr pd = IntPtr.Zero;
            try {
                LSA_UNICODE_STRING k = Make(key);
                uint status = LsaRetrievePrivateData(handle, ref k, out pd);
                uint err = LsaNtStatusToWinError(status);
                if (err != 0 || pd == IntPtr.Zero) {
                    return "absent";
                }
                LSA_UNICODE_STRING u = (LSA_UNICODE_STRING)Marshal.PtrToStructure(pd, typeof(LSA_UNICODE_STRING));
                return "present (" + (u.Length / 2) + " chars)";
            } finally {
                if (pd != IntPtr.Zero) { LsaFreeMemory(pd); }
                LsaClose(handle);
            }
        }
    }
}
'@

Add-Type -TypeDefinition $LsaCode -Language CSharp | Out-Null

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
function Test-IsAdmin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    return (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-WinlogonValue($name) {
    try {
        return (Get-ItemProperty -Path $WinlogonKey -Name $name -ErrorAction Stop).$name
    } catch {
        return $null
    }
}

function Get-SecretState {
    try {
        return [StudioFire.LSA.LsaSecret]::Info($SecretName)
    } catch {
        return 'unreadable (' + $_.Exception.Message + ')'
    }
}

# ---------------------------------------------------------------------------
# -Check : report only, never change, never needs the password
# ---------------------------------------------------------------------------
if ($Check) {
    Write-Host 'StudioFire autologon state (read-only):'
    Write-Host ('  AutoAdminLogon      : ' + [string](Get-WinlogonValue 'AutoAdminLogon'))
    Write-Host ('  DefaultUserName     : ' + [string](Get-WinlogonValue 'DefaultUserName'))
    Write-Host ('  DefaultDomainName   : ' + [string](Get-WinlogonValue 'DefaultDomainName'))

    if ($null -ne (Get-WinlogonValue 'DefaultPassword')) {
        Write-Host '  DefaultPassword(reg): PRESENT - CLEARTEXT in the registry (not what we want)'
    } else {
        Write-Host '  DefaultPassword(reg): absent (good - no cleartext stored)'
    }
    Write-Host ('  LSA secret          : ' + (Get-SecretState))
    exit 0
}

# ---------------------------------------------------------------------------
# everything below changes the machine -> must be elevated
# ---------------------------------------------------------------------------
if (-not (Test-IsAdmin)) {
    Write-Host '[X] This needs Administrator rights (LsaOpenPolicy requires an elevated process).'
    Write-Host '    Right-click the .bat / PowerShell, then "Run as administrator".'
    exit 3
}

# ---------------------------------------------------------------------------
# -Disable : autologon off, secret removed, cleartext removed
# ---------------------------------------------------------------------------
if ($Disable) {
    Set-ItemProperty -Path $WinlogonKey -Name 'AutoAdminLogon' -Value '0' -Type String
    Remove-ItemProperty -Path $WinlogonKey -Name 'DefaultPassword' -ErrorAction SilentlyContinue
    [StudioFire.LSA.LsaSecret]::Set($SecretName, $null)
    Write-Host '[ok] autologon disabled; LSA secret removed; cleartext registry value removed'
    exit 0
}

# ---------------------------------------------------------------------------
# enable
# ---------------------------------------------------------------------------
if (-not $User) {
    Write-Host '[X] -User is required (which Windows account should log the box in).'
    exit 1
}

if (-not $Password) { $Password = $env:STUDIOFIRE_AUTOLOGON_PW }
if (-not $Password) {
    $secure = Read-Host -AsSecureString ('Password for ' + $User)
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        $Password = [Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
}
if (-not $Password) {
    Write-Host '[X] No password supplied. Nothing was changed.'
    exit 1
}

$dom = if ($Domain) { $Domain } else { '.' }

# Registry: the three values Winlogon reads. Delete any cleartext password so
# the logon process falls through to the LSA secret.
Set-ItemProperty -Path $WinlogonKey -Name 'DefaultUserName'   -Value $User -Type String
Set-ItemProperty -Path $WinlogonKey -Name 'DefaultDomainName' -Value $dom  -Type String
Set-ItemProperty -Path $WinlogonKey -Name 'AutoAdminLogon'    -Value '1'   -Type String
Remove-ItemProperty -Path $WinlogonKey -Name 'DefaultPassword' -ErrorAction SilentlyContinue

# Store the password as the LSA secret Winlogon reads.
[StudioFire.LSA.LsaSecret]::Set($SecretName, $Password)

# Prove it landed (reports presence only, never the value).
Write-Host ('[ok] autologon enabled for ' + $dom + '\' + $User)
Write-Host ('[ok] LSA secret ' + $SecretName + ' : ' + (Get-SecretState))
exit 0
