# emergency_clean.ps1 — host-side emergency wipe (removal / shutdown / power-off)
# Called by:
#   - autostart_watcher.ps1 Handle-DriveRemoval (pendrive yanked)
#   - Task Scheduler JampanduShutdownClean (logoff/shutdown/power)
#   - stop_and_clean.bat / host_cleaner.py
#
# Must work even when pendrive is already gone, so it does NOT depend on the pendrive.
# It lives as %APPDATA%\Jampandu\emergency_clean.ps1 (copied by installer).

param(
    [string]$DriveRoot = "",
    [switch]$FullAmnesiac
)

$ErrorActionPreference = "SilentlyContinue"
$jDir = Join-Path $env:APPDATA "Jampandu"
$logFile = Join-Path $jDir "host_clean.log"

function Write-CleanLog([string]$msg) {
    try {
        $line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $msg"
        # Only log if not full amnesiac wipe (amnesiac deletes log itself)
        if (-not $FullAmnesiac -and -not $DriveRoot) {
            if (-not (Test-Path -LiteralPath $jDir)) { New-Item -ItemType Directory -Path $jDir -Force | Out-Null }
            Add-Content -LiteralPath $logFile -Value $line -ErrorAction SilentlyContinue
        }
    } catch {}
}

function Secure-Delete([string]$path) {
    try {
        if (-not (Test-Path -LiteralPath $path)) { return $true }
        $item = Get-Item -LiteralPath $path -ErrorAction SilentlyContinue
        if ($item -and $item.PSIsContainer) { return $false }
        $size = (Get-Item -LiteralPath $path).Length
        # Overwrite with random + zeros (best-effort, skip if huge >16MB for speed at shutdown)
        if ($size -gt 0 -and $size -lt 16MB) {
            try {
                $rand = New-Object byte[] 65536
                (New-Object Random).NextBytes($rand)
                $fs = [IO.File]::Open($path, [IO.FileMode]::Open, [IO.FileAccess]::Write, [IO.FileShare]::None)
                $remaining = $size
                while ($remaining -gt 0) {
                    $n = [Math]::Min(65536, $remaining)
                    $fs.Write($rand, 0, $n)
                    $remaining -= $n
                }
                $fs.Flush(); $fs.Close()
                # zero pass
                $fs2 = [IO.File]::Open($path, [IO.FileMode]::Open, [IO.FileAccess]::Write, [IO.FileShare]::None)
                $zero = New-Object byte[] 65536
                $remaining = $size
                while ($remaining -gt 0) {
                    $n = [Math]::Min(65536, $remaining)
                    $fs2.Write($zero, 0, $n)
                    $remaining -= $n
                }
                $fs2.Flush(); $fs2.Close()
            } catch {}
        }
        Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
        return -not (Test-Path -LiteralPath $path)
    } catch { return $false }
}

Write-CleanLog "emergency_clean start DriveRoot='$DriveRoot' FullAmnesiac=$FullAmnesiac"

# 1. Kill agent processes that belong to the removed drive (if we know the drive)
if ($DriveRoot) {
    $dl = $DriveRoot.Trim().ToUpper()
    Write-CleanLog "Killing agent processes for $dl"
    try {
        $procs = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
            $_.ExecutablePath -and $_.ExecutablePath.ToUpper().StartsWith($dl)
        }
        foreach ($p in $procs) {
            $name = $p.Name
            if ($name -match "python|llama") {
                try { Invoke-CimMethod -InputObject $p -MethodName Terminate -ErrorAction SilentlyContinue | Out-Null } catch {}
            }
        }
    } catch {}
    # Fallback taskkill for llama-server / python on that drive
    try {
        $wmic = (wmic process get ProcessId,ExecutablePath,CommandLine /FORMAT:CSV 2>$null)
        foreach ($line in $wmic) {
            if ($line.ToUpper().Contains($dl)) {
                if ($line.ToLower().Contains("llama") -or $line.ToLower().Contains("python") -or $line.ToLower().Contains("web_ui") -or $line.ToLower().Contains("run_agent")) {
                    $parts = $line.Split(",")
                    foreach ($part in $parts) { if ($part -match "^\d+$") {
                        $pid = [int]$part
                        if ($pid -gt 4) { taskkill /PID $pid /T /F 2>$null | Out-Null }
                    }}
                }
            }
        }
    } catch {}
}

# 2. Clear clipboard
try {
    Add-Type -AssemblyName System.Windows.Forms -ErrorAction SilentlyContinue
    [System.Windows.Forms.Clipboard]::Clear()
} catch {}
try { echo $null | clip 2>$null } catch {}

# 3. Wipe %APPDATA%\Jampandu traces
# Flags first
try {
    Get-ChildItem -LiteralPath $jDir -Filter "insert_*.flag" -ErrorAction SilentlyContinue | ForEach-Object { Secure-Delete $_.FullName | Out-Null }
    Get-ChildItem -LiteralPath $jDir -Filter "remove_*.flag" -ErrorAction SilentlyContinue | ForEach-Object { Secure-Delete $_.FullName | Out-Null }
} catch {}
# watcher.log — contains drive letters
try { if (Test-Path -LiteralPath (Join-Path $jDir "watcher.log")) { Secure-Delete (Join-Path $jDir "watcher.log") | Out-Null } } catch {}

# 4. TEMP traces (pen-ai / prompt / popup)
$tempDirs = @($env:TEMP, $env:TMP, (Join-Path $env:LOCALAPPDATA "Temp"), [IO.Path]::GetTempPath()) | Select-Object -Unique | Where-Object { $_ -and (Test-Path $_) }
$patterns = @("popup_in_*.txt","popup_out_*.txt","prompt_*.txt","popup_prompt_*.txt","pen-ai-*","jampandu-*","jarvis-*")
foreach ($td in $tempDirs) {
    foreach ($pat in $patterns) {
        try { Get-ChildItem -LiteralPath $td -Filter $pat -ErrorAction SilentlyContinue | ForEach-Object {
            if (-not $_.PSIsContainer) { Secure-Delete $_.FullName | Out-Null } else { Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue }
        }} catch {}
    }
}

# 5. If this is a drive-removal or full amnesiac shutdown — also wipe host_clean.log and try to remove dir
if ($DriveRoot -or $FullAmnesiac) {
    Write-CleanLog "Drive removal/shutdown — wiping host_clean.log and Jampandu dir"
    # Do NOT log after this point (would recreate)
    try { if (Test-Path -LiteralPath $logFile) { Secure-Delete $logFile | Out-Null } } catch {}
    try {
        if (Test-Path -LiteralPath $jDir) {
            $remaining = Get-ChildItem -LiteralPath $jDir -Force -ErrorAction SilentlyContinue | Where-Object { $_.Name -ne "autostart_watcher.ps1" -and $_.Name -ne "emergency_clean.ps1" }
            # Keep watcher scripts unless FullAmnesiac
            if ($FullAmnesiac) {
                # Secure delete everything then remove dir
                Get-ChildItem -LiteralPath $jDir -Force -ErrorAction SilentlyContinue | ForEach-Object { if (-not $_.PSIsContainer) { Secure-Delete $_.FullName | Out-Null } }
                Remove-Item -LiteralPath $jDir -Recurse -Force -ErrorAction SilentlyContinue
            } elseif (-not $remaining) {
                # Only flags/logs were there — directory is now empty except scripts; keep scripts
            }
        }
    } catch {}
} else {
    Write-CleanLog "emergency_clean done (normal exit)"
}

# 6. If FullAmnesiac and task exists and pendrive gone, optionally remove shutdown task? No — task is needed to keep cleaning.

exit 0
