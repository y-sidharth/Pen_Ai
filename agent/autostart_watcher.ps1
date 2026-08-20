# autostart_watcher.ps1
# Runs in background; watches for USB drive insertion events and starts the agent
# if the USB drive contains this project with AUTOSTART.marker file.
# Installation requires explicit confirmation in install_autostart.bat.

$projectFolder = Split-Path -Leaf (Split-Path -Parent $PSScriptRoot)
$marker = Join-Path $projectFolder 'AUTOSTART.marker'
$started = @{}

# Function to check if a drive contains the marker and start the agent
function Start-AgentIfMarkerPresent {
    param([string]$driveLetter)
    
    $root = $driveLetter + '\'
    
    # Skip if already started for this drive
    if ($started[$root]) {
        return
    }
    
    $path = Join-Path $root $marker
    if (Test-Path $path) {
        $agentDir = Join-Path (Join-Path $root $projectFolder) 'agent'
        $startBat = Join-Path $agentDir 'start-agent.bat'
        if (Test-Path $startBat) {
            Write-Host "[$(Get-Date)] Starting agent from $driveLetter"
            Start-Process -FilePath $startBat -WorkingDirectory $agentDir -WindowStyle Normal
            $started[$root] = $true
        }
    }
}

# Function to handle drive removal
function Handle-DriveRemoval {
    param([string]$driveLetter)
    
    $root = $driveLetter + '\'
    if ($started[$root]) {
        Write-Host "[$(Get-Date)] Drive $driveLetter removed, resetting state"
        $started.Remove($root)
    }
}

Write-Host "[$(Get-Date)] Starting USB autostart watcher for project: $projectFolder"

# First, check for any USB drives already present at startup
try {
    $drives = Get-WmiObject Win32_LogicalDisk | Where-Object { $_.DriveType -eq 2 }
    foreach ($d in $drives) {
        Start-AgentIfMarkerPresent -driveLetter $d.DeviceID
    }
} catch {
    Write-Host "[$(Get-Date)] Error checking initial drives: $_"
}

# Set up WMI event watchers for drive insertion and removal
# DriveType 2 = Removable disk (USB drive)

# Watch for drive insertion (TargetInstance has DriveType = 2)
$insertionQuery = "SELECT TargetInstance FROM __InstanceCreationEvent WITHIN 2 WHERE TargetInstance ISA 'Win32_LogicalDisk' AND TargetInstance.DriveType = 2"
$deletionQuery = "SELECT TargetInstance FROM __InstanceDeletionEvent WITHIN 2 WHERE TargetInstance ISA 'Win32_LogicalDisk' AND TargetInstance.DriveType = 2"

try {
    Register-WmiEvent -Query $insertionQuery -SourceIdentifier "USBInsertion" -Action {
        $drive = $Event.SourceEventArgs.NewEvent.TargetInstance
        $driveLetter = $drive.DeviceID
        Write-Host "[$(Get-Date)] USB drive inserted: $driveLetter"
        Start-AgentIfMarkerPresent -driveLetter $driveLetter
    } | Out-Null

    Register-WmiEvent -Query $deletionQuery -SourceIdentifier "USBRemoval" -Action {
        $drive = $Event.SourceEventArgs.NewEvent.TargetInstance
        $driveLetter = $drive.DeviceID
        Write-Host "[$(Get-Date)] USB drive removed: $driveLetter"
        Handle-DriveRemoval -driveLetter $driveLetter
    } | Out-Null

    Write-Host "[$(Get-Date)] WMI event watchers registered. Waiting for USB events..."

    # Keep the script running indefinitely
    while ($true) {
        Start-Sleep -Seconds 5
        
        # Periodically check if any marked drives need to be started (safety net)
        try {
            $drives = Get-WmiObject Win32_LogicalDisk | Where-Object { $_.DriveType -eq 2 }
            foreach ($d in $drives) {
                $root = $d.DeviceID + '\'
                if (-not $started[$root]) {
                    Start-AgentIfMarkerPresent -driveLetter $d.DeviceID
                }
            }
        } catch {
            # Ignore errors
        }
    }
} catch {
    Write-Host "[$(Get-Date)] Error setting up WMI watchers: $_"
    Write-Host "[$(Get-Date)] Falling back to polling mode..."
    
    # Fallback to polling if WMI events fail
    while ($true) {
        try {
            $drives = Get-WmiObject Win32_LogicalDisk | Where-Object { $_.DriveType -eq 2 }
            foreach ($d in $drives) {
                Start-AgentIfMarkerPresent -driveLetter $d.DeviceID
            }
        } catch {
            # Ignore errors
        }
        Start-Sleep -Seconds 2
    }
}