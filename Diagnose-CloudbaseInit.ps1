<#
.SYNOPSIS
    Read-only diagnostic for a Tencent Cloud Windows Cloudbase-Init install.

.DESCRIPTION
    Answers the question "what is ACTUALLY on this machine?" without changing
    anything. Run this when Install-CloudbaseInit.ps1 reports a [FAIL] you do
    not understand, or before opening a ticket.

    It prints:
      * the resolved install directory and every candidate root it probed
      * the full contents of bin\ (so you can see the real service binary name)
      * the Python* folder layout and where localscripts.py actually lives
      * EVERY service whose name or binary path mentions cloudbase (including
        the Sysprep-phase cloudbase-init-unattend service)
      * the service account, startup mode and state
      * the effective cloudbase-init.conf values that matter
      * the NtfsLockSystemFilePages registry value
      * installed-product entries from the uninstall registry keys
      * the tail of the Cloudbase-Init runtime log, if present

    Nothing is written, installed, started or stopped.

    Run (elevated PowerShell recommended, but it works unelevated too):
        powershell -ExecutionPolicy Bypass -File .\Diagnose-CloudbaseInit.ps1
#>

[CmdletBinding()]
param()

$ErrorActionPreference = 'Continue'
$ProgressPreference    = 'SilentlyContinue'

function Write-Section {
    param([string]$Title)
    Write-Host ''
    Write-Host ('=' * 72)
    Write-Host "  $Title"
    Write-Host ('=' * 72)
}

function Write-KV {
    param([string]$Key, $Value)
    Write-Host ("  {0,-34} {1}" -f ($Key + ':'), $Value)
}

Write-Section 'Environment'
Write-KV 'Diagnostic script version' '1.1'
Write-KV 'PowerShell version' $PSVersionTable.PSVersion
try {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $elev = (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
    Write-KV 'Elevated' $elev
    Write-KV 'Running as' $id.Name
} catch {
    Write-KV 'Elevated' "unknown ($($_.Exception.Message))"
}
try {
    $os = Get-CimInstance Win32_OperatingSystem -ErrorAction Stop
    Write-KV 'OS' "$($os.Caption) ($($os.Version))"
} catch {
    Write-KV 'OS' 'could not query Win32_OperatingSystem'
}
Write-KV 'PROCESSOR_ARCHITECTURE' $env:PROCESSOR_ARCHITECTURE
Write-KV 'PROCESSOR_ARCHITEW6432' $(if ($env:PROCESSOR_ARCHITEW6432) { $env:PROCESSOR_ARCHITEW6432 } else { '<not set>' })
Write-KV 'TEMP' $env:TEMP

# ---------------------------------------------------------------- install dir
Write-Section 'Install directory resolution'
$rel = 'Cloudbase Solutions\Cloudbase-Init'
$roots = New-Object System.Collections.ArrayList
foreach ($r in @($env:ProgramW6432, $env:ProgramFiles, ${env:ProgramFiles(x86)})) {
    if ($r -and -not $roots.Contains($r)) { [void]$roots.Add($r) }
}
$installDir = ''
foreach ($r in $roots) {
    $p = Join-Path $r $rel
    $hit = Test-Path $p
    Write-Host ("  probe {0,-52} {1}" -f $p, $(if ($hit) { 'FOUND' } else { 'absent' }))
    if ($hit -and -not $installDir) { $installDir = $p }
}
if (-not $installDir) {
    Write-Host '  RESULT: Cloudbase-Init is NOT installed in any Program Files root.'
} else {
    Write-KV 'RESOLVED install dir' $installDir
}

# ------------------------------------------------------------------- bin dir
if ($installDir) {
    Write-Section 'bin\ contents (the real service binary name is here)'
    $bin = Join-Path $installDir 'bin'
    if (Test-Path $bin) {
        Get-ChildItem -Path $bin -File -ErrorAction SilentlyContinue |
            Sort-Object Name |
            ForEach-Object { Write-Host ("  {0,-42} {1,10:N0} bytes" -f $_.Name, $_.Length) }
    } else {
        Write-Host "  MISSING: $bin"
    }

    Write-Section 'Python layout / localscripts.py location'
    $pyDirs = @(Get-ChildItem -Path $installDir -Directory -Filter 'Python*' -ErrorAction SilentlyContinue)
    if ($pyDirs.Count -eq 0) {
        Write-Host '  No Python* folder found under the install directory.'
    } else {
        foreach ($d in $pyDirs) { Write-KV 'Python folder' $d.Name }
    }
    $found = @(Get-ChildItem -Path $installDir -Recurse -Filter 'localscripts.py*' -File -ErrorAction SilentlyContinue)
    if ($found.Count -eq 0) {
        Write-Host '  localscripts.py NOT found anywhere under the install directory.'
    } else {
        foreach ($f in $found) {
            Write-Host ("  {0,10:N0} bytes  {1}" -f $f.Length, $f.FullName)
        }
    }

    Write-Section 'LocalScripts\ contents'
    $ls = Join-Path $installDir 'LocalScripts'
    if (Test-Path $ls) {
        $items = @(Get-ChildItem -Path $ls -File -ErrorAction SilentlyContinue)
        if ($items.Count -eq 0) { Write-Host '  (empty)' }
        foreach ($f in $items) {
            Write-Host ("  {0,10:N0} bytes  {1}" -f $f.Length, $f.Name)
            # Show whether the file is still marked as downloaded-from-internet.
            # -Stream is a Windows-only parameter, so a plain -ErrorAction cannot
            # suppress the binding failure - it must be wrapped in try/catch.
            try {
                $zone = Get-Item -Path $f.FullName -Stream 'Zone.Identifier' -ErrorAction Stop
                if ($zone) { Write-Host '             ^ WARNING: still has a Zone.Identifier stream (not unblocked)' }
            } catch { }
        }
    } else {
        Write-Host "  MISSING: $ls"
    }
}

# ------------------------------------------------------------------- services
Write-Section 'Services mentioning "cloudbase" (name OR binary path)'
$svcRows = @()
try {
    $svcRows = @(Get-CimInstance Win32_Service -ErrorAction Stop | Where-Object {
        ($_.Name -imatch 'cloudbase') -or
        ($_.DisplayName -imatch 'cloudbase') -or
        ($_.PathName -and $_.PathName -imatch 'cloudbase')
    })
} catch {
    Write-Host "  Could not enumerate services via CIM: $($_.Exception.Message)"
}
if ($svcRows.Count -eq 0) {
    Write-Host '  NO matching service is registered on this machine.'
    Write-Host '  -> If the MSI reported success, the service creation step failed, or the'
    Write-Host '     image was Sysprepped with Cloudbase-Init in unattend mode (which can'
    Write-Host '     remove the persistent service). Re-run the installer with -Force.'
} else {
    foreach ($s in $svcRows) {
        Write-Host ''
        Write-KV 'Name' $s.Name
        Write-KV 'DisplayName' $s.DisplayName
        Write-KV 'StartName (account)' $s.StartName
        Write-KV 'StartMode' $s.StartMode
        Write-KV 'State' $s.State
        Write-KV 'PathName' $s.PathName
        if ($s.Name -imatch 'unattend') {
            Write-Host '    NOTE: this is the Sysprep-phase service, NOT the persistent one.'
        }
        if ($s.StartName -ne 'LocalSystem') {
            Write-Host "    WARNING: account is '$($s.StartName)', expected 'LocalSystem'."
            Write-Host '             Reinstall with RUN_SERVICE_AS_LOCAL_SYSTEM=1.'
        }
    }
}

# --------------------------------------------------------------------- config
if ($installDir) {
    Write-Section 'cloudbase-init.conf (key values)'
    $conf = Join-Path $installDir 'conf\cloudbase-init.conf'
    if (Test-Path $conf) {
        Write-KV 'Path' $conf
        Write-KV 'Size' ("{0:N0} bytes" -f (Get-Item $conf).Length)
        Write-Host ''
        $keys = 'volumes_to_extend|metadata_base_url|ec2_metadata_base_url|local_scripts_path|bsdtar_path|mtools_path|logdir|plugins|activate_windows|kms_host|username'
        Select-String -Path $conf -Pattern "^($keys)=" -ErrorAction SilentlyContinue |
            ForEach-Object { Write-Host "    $($_.Line)" }
        $bak = @(Get-ChildItem -Path "$conf.bak-*" -File -ErrorAction SilentlyContinue)
        if ($bak.Count -gt 0) { Write-Host ''; Write-KV 'Backups present' $bak.Count }
    } else {
        Write-Host "  MISSING: $conf"
    }
}

# ------------------------------------------------------------------- registry
Write-Section 'NtfsLockSystemFilePages'
try {
    $v = (Get-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem' `
            -Name 'NtfsLockSystemFilePages' -ErrorAction Stop).NtfsLockSystemFilePages
    Write-KV 'Value' "$v $(if ($v -eq 0) { '(correct)' } else { '(expected 0)' })"
} catch {
    Write-Host '  Value is not set (the installer step was skipped or failed).'
}

Write-Section 'Installed products matching Cloudbase (uninstall registry)'
$uninstKeys = @(
    'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*',
    'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*'
)
$prods = @()
foreach ($k in $uninstKeys) {
    $prods += @(Get-ItemProperty -Path $k -ErrorAction SilentlyContinue |
        Where-Object { $_.DisplayName -imatch 'cloudbase' })
}
if ($prods.Count -eq 0) {
    Write-Host '  No Cloudbase product is registered in the uninstall keys.'
} else {
    foreach ($p in $prods) {
        Write-KV 'DisplayName' $p.DisplayName
        Write-KV 'DisplayVersion' $p.DisplayVersion
        Write-KV 'InstallLocation' $p.InstallLocation
        Write-Host ''
    }
}

# ------------------------------------------------------------------ run logs
if ($installDir) {
    Write-Section 'Cloudbase-Init runtime log (last 25 lines)'
    $rt = Join-Path $installDir 'log\cloudbase-init.log'
    if (Test-Path $rt) {
        Write-KV 'Path' $rt
        Write-Host ''
        Get-Content -Path $rt -Tail 25 -ErrorAction SilentlyContinue |
            ForEach-Object { Write-Host "    $_" }
    } else {
        Write-Host "  Not present yet (normal before the service has ever run): $rt"
    }
}

Write-Section 'Installer log location'
# Cross-check which build of the installer actually produced the log. A stale
# copy of Install-CloudbaseInit.ps1 is a very common source of "already fixed"
# failures reappearing.
$sibling = Join-Path $PSScriptRoot 'Install-CloudbaseInit.ps1'
if (Test-Path $sibling) {
    $verLine = @(Select-String -Path $sibling -Pattern "^\`$ScriptVersion\s*=\s*'([^']+)'" -ErrorAction SilentlyContinue |
                    Select-Object -First 1)
    if ($verLine.Count -gt 0 -and $verLine[0].Matches.Count -gt 0) {
        Write-KV 'Installer script next to me' "v$($verLine[0].Matches[0].Groups[1].Value)"
    } else {
        Write-KV 'Installer script next to me' 'present, but carries NO version marker -> it is an OLD build, replace it'
    }
} else {
    Write-KV 'Installer script next to me' 'not in this folder'
}

$instLog = Join-Path (Join-Path $env:TEMP 'CloudbaseInitSetup') 'msi-install.log'
$runLog  = Join-Path (Join-Path $env:TEMP 'CloudbaseInitSetup') 'Install-CloudbaseInit.log'
if (Test-Path $runLog) {
    Write-KV 'Installer run log' $runLog
    $verRun = @(Select-String -Path $runLog -Pattern 'Install-CloudbaseInit\.ps1 v([0-9.]+) starting' -ErrorAction SilentlyContinue |
                    Select-Object -Last 1)
    if ($verRun.Count -gt 0 -and $verRun[0].Matches.Count -gt 0) {
        Write-KV 'Last run was version' "v$($verRun[0].Matches[0].Groups[1].Value)"
    } else {
        Write-Host '  Last run logged NO version banner -> an OLD build was executed.'
        Write-Host '  Copy the current Install-CloudbaseInit.ps1 over and re-run it.'
    }
} else {
    Write-KV 'Installer run log' "not found at $runLog"
}
Write-Host ''

if (Test-Path $instLog) {
    Write-KV 'MSI log' $instLog
    $errLines = @(Select-String -Path $instLog -Pattern 'Product: .*Error|Installation failed|return value 3' -ErrorAction SilentlyContinue)
    if ($errLines.Count -gt 0) {
        Write-Host ''
        Write-Host '  Suspicious lines from the MSI log:'
        foreach ($l in ($errLines | Select-Object -First 10)) { Write-Host "    $($l.Line.Trim())" }
    } else {
        Write-Host '  No obvious failure markers in the MSI log.'
    }
} else {
    Write-KV 'MSI log' "not found at $instLog"
}

Write-Host ''
Write-Host ('=' * 72)
Write-Host '  Diagnostic complete. Nothing was modified.'
Write-Host ('=' * 72)
