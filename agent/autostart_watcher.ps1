# autostart_watcher.ps1 - Portable USB watcher (FIXED VERSION)
# Runs from %APPDATA%\Jampandu\ - watches ALL removable drives for AUTOSTART.marker
# Works regardless of drive letter. No hard-coded E:\ path.

$markerName = "AUTOSTART.marker"
$started = @{}
$logFile = Join-Path $env:APPDATA "Jampandu\watcher.log"
$emergencyClean = Join-Path $env:APPDATA "Jampandu\emergency_clean.ps1"

function Write-Log {
    param([string]$msg)
    $line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $msg"
    Write-Host $line
    try { Add-Content -LiteralPath $logFile -Value $line -ErrorAction SilentlyContinue } catch {}
}

# ---- Host clean on removal / power-off (amnesiac) ----
function Secure-DeleteFile([string]$path) {
    try {
        if (-not (Test-Path -LiteralPath $path)) { return }
        $sz = (Get-Item -LiteralPath $path).Length
        if ($sz -gt 0 -and $sz -lt 8MB) {
            try {
                $rand = New-Object byte[] 65536
                (New-Object Random).NextBytes($rand)
                $fs = [IO.File]::Open($path, [IO.FileMode]::Open, [IO.FileAccess]::Write, [IO.FileShare]::None)
                $rem = $sz
                while ($rem -gt 0) { $n=[Math]::Min(65536,$rem); $fs.Write($rand,0,$n); $rem-=$n }
                $fs.Flush(); $fs.Close()
                $fs2=[IO.File]::Open($path,[IO.FileMode]::Open,[IO.FileAccess]::Write,[IO.FileShare]::None)
                $zero=New-Object byte[] 65536
                $rem=$sz; while($rem -gt 0){$n=[Math]::Min(65536,$rem); $fs2.Write($zero,0,$n); $rem-=$n}
                $fs2.Flush(); $fs2.Close()
            } catch {}
        }
        Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
    } catch {}
}

function Invoke-HostClean {
    param([string]$driveLetter = "", [switch]$FullAmnesiac)
    $dl = $driveLetter.Trim()
    Write-Log "Host clean triggered: drive='$dl' Full=$FullAmnesiac"
    # Prefer emergency_clean.ps1 if present (more thorough, reuses host_cleaner.py via python if pendrive still present)
    if (Test-Path -LiteralPath $emergencyClean) {
        try {
            $args = @()
            if ($dl) { $args += "-DriveRoot"; $args += $dl }
            if ($FullAmnesiac) { $args += "-FullAmnesiac" }
            & powershell -NoProfile -ExecutionPolicy Bypass -File $emergencyClean @args 2>$null
            Write-Log "emergency_clean.ps1 executed for '$dl'"
        } catch { Write-Log "emergency_clean.ps1 failed: $_" }
    }
    # Fallback inline: kill agent processes for that drive, wipe flags/watcher.log/temp/clipboard
    if ($dl) {
        $root = $dl.ToUpper().TrimEnd(':') + ':\'
        try {
            $procs = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.ExecutablePath -and $_.ExecutablePath.ToUpper().StartsWith($root) }
            foreach ($p in $procs) { if ($p.Name -match "python|llama") { try{Invoke-CimMethod -InputObject $p -MethodName Terminate -ErrorAction SilentlyContinue|Out-Null}catch{} } }
        } catch {}
        # taskkill fallback for wmic-noticed pids
        try {
            # quick kill known binaries on that drive
            $wmic = (wmic process get ProcessId,ExecutablePath /FORMAT:CSV 2>$null)
            foreach ($line in $wmic) { if ($line.ToUpper().Contains($root)) { foreach($part in $line.Split(",")){ if($part -match "^\d+$"){ $pid=[int]$part; if($pid -gt 4){ taskkill /PID $pid /T /F 2>$null | Out-Null } } } } }
        } catch {}
    }
    try { echo $null | clip 2>$null } catch {}
    try {
        $jDir = Join-Path $env:APPDATA "Jampandu"
        Get-ChildItem -LiteralPath $jDir -Filter "insert_*.flag" -ErrorAction SilentlyContinue | ForEach-Object { Secure-DeleteFile $_.FullName }
        Get-ChildItem -LiteralPath $jDir -Filter "remove_*.flag" -ErrorAction SilentlyContinue | ForEach-Object { Secure-DeleteFile $_.FullName }
        foreach ($td in @($env:TEMP,$env:TMP,[IO.Path]::GetTempPath()) | Where-Object {$_ -and (Test-Path $_)}) {
            foreach ($pat in @("popup_in_*.txt","popup_out_*.txt","prompt_*.txt","popup_prompt_*.txt","pen-ai-*","jampandu-*")) {
                Get-ChildItem -LiteralPath $td -Filter $pat -ErrorAction SilentlyContinue | ForEach-Object { if(-not $_.PSIsContainer){Secure-DeleteFile $_.FullName}else{Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue} }
            }
        }
    } catch {}
    # On drive removal, also securely delete watcher.log + host_clean.log (no recreation)
    if ($dl) {
        try { if (Test-Path -LiteralPath $logFile) { Secure-DeleteFile $logFile } } catch {}
        try { $hcl=Join-Path (Join-Path $env:APPDATA "Jampandu") "host_clean.log"; if(Test-Path -LiteralPath $hcl){ Secure-DeleteFile $hcl } } catch {}
        Write-Host "Host traces erased for $dl (amnesiac removal)"
    }
}

function Find-ProjectAndStart {
    param([string]$driveLetter)

    $root = $driveLetter + '\'
    if ($started[$root]) { return }

    $candidatePaths = @()

    # 1. Check root of drive: F:\AUTOSTART.marker
    $candidatePaths += Join-Path $root $markerName

    # 2. Check one level deep: F:\pen AI\AUTOSTART.marker, F:\Jampandu\AUTOSTART.marker etc.
    try {
        $dirs = Get-ChildItem -LiteralPath $root -Directory -ErrorAction SilentlyContinue
        foreach ($d in $dirs) {
            $candidatePaths += Join-Path $d.FullName $markerName
        }
    } catch {}

    foreach ($markerPath in $candidatePaths) {
        if (Test-Path -LiteralPath $markerPath) {
            $projectDir = Split-Path -Parent $markerPath
            # PREFER browser UI (start-ui.bat) over CLI (start-agent.bat)
            $uiBat = Join-Path $projectDir "agent\start-ui.bat"
            $cliBat = Join-Path $projectDir "agent\start-agent.bat"
            $uiBatAlt = Join-Path $projectDir "start-ui.bat"

            $targetBat = $null
            if (Test-Path -LiteralPath $uiBat) { $targetBat = $uiBat }
            elseif (Test-Path -LiteralPath $cliBat) { $targetBat = $cliBat }
            elseif (Test-Path -LiteralPath $uiBatAlt) { $targetBat = $uiBatAlt }

            if ($targetBat) {
                $agentDir = Split-Path -Parent $targetBat
                Write-Log "Found marker at $markerPath -> Starting BROWSER UI $targetBat"
                try {
                    Start-Process -FilePath $targetBat -WorkingDirectory $agentDir -WindowStyle Normal
                    $started[$root] = $true
                    Write-Log "Launched browser agent from $driveLetter (project: $projectDir) -> http://127.0.0.1:8765"
                } catch {
                    Write-Log "Failed to start $targetBat : $_"
                }
                return
            } else {
                Write-Log "Marker found at $markerPath but no start-ui.bat/start-agent.bat in $projectDir\agent"
            }
        }
    }
}

function Handle-DriveRemoval {
    param([string]$driveLetter)
    $root = $driveLetter + '\'
    $wasStarted = $started.ContainsKey($root)
    if ($wasStarted) {
        Write-Log "Drive $driveLetter removed, resetting state"
        $started.Remove($root)
    }
    # ALWAYS erase host traces on removal, even if we didn't start it (user may have run manually)
    Write-Log "Pendrive removed ($driveLetter) — erasing host traces (amnesiac)..."
    Invoke-HostClean -driveLetter $driveLetter
    if ($wasStarted) { Write-Log "Host clean complete for $driveLetter" } else { Write-Log "Host clean complete (drive not in started map)" }
}

Write-Log "=== Jampandu USB watcher started (PID $PID) ==="
Write-Log "Watching for removable drives (DriveType=2) containing $markerName"

# Check drives already present at boot
try {
    $drives = Get-CimInstance Win32_LogicalDisk -ErrorAction Stop | Where-Object { $_.DriveType -eq 2 }
    foreach ($d in $drives) {
        Find-ProjectAndStart -driveLetter $d.DeviceID
    }
} catch {
    Write-Log "Initial scan failed: $_"
    try {
        $drives = Get-WmiObject Win32_LogicalDisk -ErrorAction SilentlyContinue | Where-Object { $_.DriveType -eq 2 }
        foreach ($d in $drives) { Find-ProjectAndStart -driveLetter $d.DeviceID }
    } catch {}
}

# --- WMI Event Watchers (instant, no polling delay) ---
$insertionQuery = "SELECT TargetInstance FROM __InstanceCreationEvent WITHIN 2 WHERE TargetInstance ISA 'Win32_LogicalDisk' AND TargetInstance.DriveType = 2"
$deletionQuery  = "SELECT TargetInstance FROM __InstanceDeletionEvent WITHIN 2 WHERE TargetInstance ISA 'Win32_LogicalDisk' AND TargetInstance.DriveType = 2"

$wmiOk = $false
try {
    Register-WmiEvent -Query $insertionQuery -SourceIdentifier "USBInsertion" -Action {
        $drive = $Event.SourceEventArgs.NewEvent.TargetInstance
        $letter = $drive.DeviceID
        Write-Host "[$(Get-Date)] USB inserted: $letter"
        # Need to re-define function in event scope - use polling fallback to handle it
        # Event action runs in separate runspace, so we touch a flag file instead
        $flag = Join-Path $env:APPDATA "Jampandu\insert_$($letter.TrimEnd(':')).flag"
        Set-Content -LiteralPath $flag -Value $letter -ErrorAction SilentlyContinue
    } | Out-Null

    Register-WmiEvent -Query $deletionQuery -SourceIdentifier "USBRemoval" -Action {
        $drive = $Event.SourceEventArgs.NewEvent.TargetInstance
        $letter = $drive.DeviceID
        Write-Host "[$(Get-Date)] USB removed: $letter"
        $flag = Join-Path $env:APPDATA "Jampandu\remove_$($letter.TrimEnd(':')).flag"
        Set-Content -LiteralPath $flag -Value $letter -ErrorAction SilentlyContinue
    } | Out-Null

    Write-Log "WMI event watchers registered. Waiting..."
    $wmiOk = $true
} catch {
    Write-Log "WMI registration failed: $_ - using polling only"
}

# --- Power-off / shutdown / logoff: erase host traces even on unexpected power cut ---
# Browsers/session ending events — best-effort. The Task Scheduler shutdown task (JampanduShutdownClean)
# is the durable guarantee; these handlers catch in-session shutdown/logoff for faster wipe.
try {
    $null = Register-EngineEvent -SourceIdentifier PowerShell.Exiting -Action {
        try {
            $jDir = Join-Path $env:APPDATA "Jampandu"
            $ec = Join-Path $jDir "emergency_clean.ps1"
            if (Test-Path -LiteralPath $ec) { & powershell -NoProfile -ExecutionPolicy Bypass -File $ec -FullAmnesiac 2>$null }
        } catch {}
    } -ErrorAction SilentlyContinue
    Write-Log "PowerShell.Exiting handler registered (shutdown clean)"
} catch { Write-Log "PowerShell.Exiting handler failed: $_" }

try {
    # SystemEvents.SessionEnding fires on logoff/shutdown (needs a message loop — main loop keeps it alive)
    Add-Type -AssemblyName System.Windows.Forms -ErrorAction SilentlyContinue
    $sessionEndingHandler = {
        Write-Host "[$(Get-Date)] SessionEnding — erasing host traces"
        try { Invoke-HostClean -FullAmnesiac } catch {}
    }
    # Use Register-ObjectEvent for SessionEnding if available
    try {
        Register-ObjectEvent -InputObject ([Microsoft.Win32.SystemEvents]) -EventName SessionEnding -SourceIdentifier JampanduSessionEnding -Action $sessionEndingHandler -ErrorAction SilentlyContinue | Out-Null
        Write-Log "SessionEnding handler registered"
    } catch { Write-Log "SessionEnding handler not available: $_" }
} catch {}

# Main loop - handles both WMI flags and periodic polling safety-net
while ($true) {
    Start-Sleep -Seconds 2

    # Process WMI flags (since event Action runs isolated)
    try {
        $flagDir = Join-Path $env:APPDATA "Jampandu"
        $insertFlags = Get-ChildItem -LiteralPath $flagDir -Filter "insert_*.flag" -ErrorAction SilentlyContinue
        foreach ($f in $insertFlags) {
            $letter = (Get-Content -LiteralPath $f.FullName -ErrorAction SilentlyContinue | Select-Object -First 1)
            if ($letter) { $letter = $letter.Trim() }
            Remove-Item -LiteralPath $f.FullName -Force -ErrorAction SilentlyContinue
            if ($letter -match "^[A-Z]:$") {
                Write-Log "WMI event: insert $letter"
                Find-ProjectAndStart -driveLetter $letter
            }
        }
        $removeFlags = Get-ChildItem -LiteralPath $flagDir -Filter "remove_*.flag" -ErrorAction SilentlyContinue
        foreach ($f in $removeFlags) {
            $letter = (Get-Content -LiteralPath $f.FullName -ErrorAction SilentlyContinue | Select-Object -First 1)
            if ($letter) { $letter = $letter.Trim() }
            Remove-Item -LiteralPath $f.FullName -Force -ErrorAction SilentlyContinue
            if ($letter -match "^[A-Z]:$") {
                Handle-DriveRemoval -driveLetter $letter
            }
        }
    } catch {}

    # Safety-net polling: also scan all removables every 2s (handles missed WMI)
    try {
        $drives = Get-CimInstance Win32_LogicalDisk -ErrorAction SilentlyContinue | Where-Object { $_.DriveType -eq 2 }
        if (-not $drives) {
            $drives = Get-WmiObject Win32_LogicalDisk -ErrorAction SilentlyContinue | Where-Object { $_.DriveType -eq 2 }
        }
        foreach ($d in $drives) {
            $root = $d.DeviceID + '\'
            if (-not $started.ContainsKey($root)) {
                Find-ProjectAndStart -driveLetter $d.DeviceID
            }
        }
        # Clean up $started for drives no longer present — also erase host traces (polling fallback for yank)
        $currentRoots = @($drives | ForEach-Object { $_.DeviceID + '\' })
        foreach ($k in @($started.Keys)) {
            if ($currentRoots -notcontains $k) {
                $dl = $k.TrimEnd('\')
                Write-Log "Polling detected removal: $dl — erasing host traces"
                $started.Remove($k)
                Invoke-HostClean -driveLetter $dl
            }
        }
    } catch {}
}
