#!/usr/bin/env python3
"""
Media controller for Jampandu - provides media playback and system audio control.
Supports local music playback, YouTube/Spotify control, and system volume.
"""
import os
import sys
import subprocess
import json
import re
from pathlib import Path

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Default music directories to search
DEFAULT_MUSIC_DIRS = [
    os.path.expanduser("~/Music"),
    os.path.expanduser("~/Downloads/Music"),
    "D:/Music",
    "E:/Music",
]

# Supported audio extensions
AUDIO_EXTENSIONS = {'.mp3', '.wav', '.flac', '.m4a', '.ogg', '.wma', '.aac'}

# Media player commands
MEDIA_PLAYERS = {
    'vlc': 'vlc.exe',
    'windows_media_player': 'wmplayer.exe',
    'spotify': 'spotify.exe',
}


class MediaController:
    """Controls media playback and system audio."""
    
    def __init__(self, config=None):
        self.config = config or {}
        self.music_dirs = self.config.get('music_dirs', DEFAULT_MUSIC_DIRS)
        self.player = self.config.get('media_player', 'vlc')
        self._music_library = None
        self._queue = []
        self._current_index = -1
        
    def _find_music_files(self):
        """Scan music directories for audio files."""
        if self._music_library is not None:
            return self._music_library
            
        library = []
        for dir_path in self.music_dirs:
            if not os.path.isdir(dir_path):
                continue
            try:
                for root, dirs, files in os.walk(dir_path):
                    for file in files:
                        if Path(file).suffix.lower() in AUDIO_EXTENSIONS:
                            library.append({
                                'name': Path(file).stem,
                                'path': os.path.join(root, file),
                                'album': os.path.basename(root) if root != dir_path else None,
                            })
            except PermissionError:
                pass
                
        self._music_library = library
        return library
    
    def search_song(self, query):
        """
        Search for a song in the local library.
        
        Args:
            query: Search query (song name, artist, etc.)
            
        Returns:
            List of matching songs
        """
        library = self._find_music_files()
        query_lower = query.lower()
        
        # Try various matching strategies
        matches = []
        for song in library:
            name_lower = song['name'].lower()
            # Exact match
            if query_lower == name_lower:
                matches.insert(0, song)
            # Contains match
            elif query_lower in name_lower:
                matches.append(song)
            # Word match (all query words in name)
            elif all(word in name_lower for word in query_lower.split()):
                matches.append(song)
                
        return matches[:10]  # Return top 10 matches
    
    def play_song(self, song_path):
        """
        Play a specific song file.
        
        Args:
            song_path: Path to the audio file
            
        Returns:
            True if playback started, False otherwise
        """
        if not os.path.exists(song_path):
            return False
            
        try:
            if self.player == 'vlc' and self._find_player('vlc'):
                subprocess.Popen([self._find_player('vlc'), song_path], 
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                # Use default system player
                os.startfile(song_path)
            return True
        except Exception as e:
            print(f"Error playing song: {e}")
            return False
    
    def play_song_by_name(self, song_name):
        """
        Search and play a song by name.
        
        Args:
            song_name: Name of the song to play
            
        Returns:
            Tuple of (success: bool, message: str)
        """
        matches = self.search_song(song_name)
        if not matches:
            return False, f"Could not find '{song_name}' in your music library."
            
        # Play the first match
        song = matches[0]
        if self.play_song(song['path']):
            return True, f"Playing: {song['name']}"
        else:
            return False, f"Found '{song['name']}' but could not play it."
    
    def _find_player(self, player_name):
        """Find the path to a media player executable."""
        # Common installation paths
        paths_to_check = [
            os.environ.get('PROGRAMFILES', 'C:/Program Files'),
            os.environ.get('PROGRAMFILES(X86)', 'C:/Program Files (x86)'),
            os.environ.get('LOCALAPPDATA', ''),
        ]
        
        player_executables = {
            'vlc': ['VideoLAN/VLC/vlc.exe', 'vlc.exe'],
            'windows_media_player': ['Windows Media Player/wmplayer.exe'],
        }
        
        executables = player_executables.get(player_name, [f'{player_name}.exe'])
        
        for base_path in paths_to_check:
            if not base_path:
                continue
            for exe_path in executables:
                full_path = os.path.join(base_path, exe_path)
                if os.path.isfile(full_path):
                    return full_path
                    
        return None
    
    def set_volume(self, level):
        """
        Set system volume level.
        
        Args:
            level: Volume level (0-100)
            
        Returns:
            True if successful
        """
        level = max(0, min(100, int(level)))
        
        try:
            # Use nircmd if available
            nircmd_path = self._find_nircmd()
            if nircmd_path:
                subprocess.run([nircmd_path, 'setsysvolume', str(int(level * 655.35))],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return True
                
            # Fall back to simulating volume-up/down key presses via SendKeys,
            # driven toward the requested level from an assumed 50% baseline
            # (Windows exposes no simple stdlib/PowerShell "set absolute volume"
            # call without a helper like nircmd).
            baseline = 50
            key = 175 if level > baseline else 174  # Volume Up / Volume Down virtual keys
            steps = abs(level - baseline)
            if steps:
                script = (
                    "$wsh = New-Object -ComObject WScript.Shell; "
                    f"1..{steps} | ForEach-Object {{ $wsh.SendKeys([char]{key}) }}"
                )
                subprocess.run(['powershell', '-Command', script],
                             capture_output=True, timeout=5)
            return True
        except Exception as e:
            print(f"Error setting volume: {e}")
            return False
    
    def get_volume(self):
        """Get current system volume level (0-100) via CoreAudio endpoint."""
        try:
            ps = (
                "Add-Type -AssemblyName System.Windows.Forms -ErrorAction SilentlyContinue; "
                "try { "
                "  Add-Type -TypeDefinition '"
                "using System; using System.Runtime.InteropServices;"
                "[Guid(\"5CDF2C82-841E-4546-9722-0CF74078229A\"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]"
                "interface IAudioEndpointVolume { int _a(); int _b(); int _c(); int _d(); int GetMasterVolumeLevelScalar(out float pfLevel); }"
                "[Guid(\"D666063F-1587-4E43-81F1-B948E807363F\"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]"
                "interface IMMDevice { int Activate(ref Guid iid, int dwClsCtx, IntPtr pActivationParams, [MarshalAs(UnmanagedType.IUnknown)] out object ppInterface); }"
                "class _V {}' -ErrorAction Stop; "
                "  $v = 50; "
                "  try { "
                "    $o = [System.Activator]::CreateInstance([System.Type]::GetTypeFromCLSID('BCDE0395-E52F-467C-8E3D-C4579291692E'));"
                "    $e = $o.GetType().GetMethod('GetDefaultAudioEndpoint').Invoke($o, @(0,1));"
                "    $g=[Guid]'5CDF2C82-841E-4546-9722-0CF74078229A'; $a=$null;"
                "    $e.Activate([ref]$g, 1, [IntPtr]::Zero, [ref]$a) | Out-Null;"
                "    $lvl=[float]0; $a.GetMasterVolumeLevelScalar([ref]$lvl) | Out-Null;"
                "    $v=[int][Math]::Round($lvl*100);"
                "  } catch { $v = 50 }"
                "  Write-Output $v; "
                "} catch { Write-Output 50 }"
            )
            result = subprocess.run(
                ['powershell', '-Command', ps],
                capture_output=True, text=True, timeout=5
            )
            raw = (result.stdout or "").strip().splitlines()[-1].strip() if result.stdout else ""
            try:
                lvl = int(float(raw))
                if 0 <= lvl <= 100:
                    return lvl
            except Exception:
                pass
            return None
        except Exception:
            return None
    
    def _find_nircmd(self):
        """Find nircmd.exe for system control."""
        paths = [
            os.path.join(BASE_DIR, 'bin', 'nircmd.exe'),
            os.path.join(os.path.dirname(BASE_DIR), 'bin', 'nircmd.exe'),
            'C:/Windows/nircmd.exe',
        ]
        for path in paths:
            if os.path.isfile(path):
                return path
        return None
    
    def get_song_list(self, limit=20):
        """Get a list of available songs."""
        library = self._find_music_files()
        return [song['name'] for song in library[:limit]]
    
    def get_library_stats(self):
        """Get statistics about the music library."""
        library = self._find_music_files()
        albums = set(song.get('album') for song in library if song.get('album'))
        return {
            'total_songs': len(library),
            'total_albums': len(albums),
            'scanned_dirs': [d for d in self.music_dirs if os.path.isdir(d)],
        }

    def pause(self):
        """Pause current VLC playback (no new process)."""
        vlc = self._find_player('vlc')
        if vlc:
            try:
                # VLC RC interface not assumed; fallback to media keys
                ps = "$wsh = New-Object -ComObject WScript.Shell; $wsh.SendKeys([char]179)"
                subprocess.run(['powershell', '-Command', ps], capture_output=True, timeout=5)
                return True
            except Exception:
                return False
        try:
            ps = "$wsh = New-Object -ComObject WScript.Shell; $wsh.SendKeys([char]179)"
            subprocess.run(['powershell', '-Command', ps], capture_output=True, timeout=5)
            return True
        except Exception:
            return False

    def resume(self):
        """Resume playback. Media Play/Pause key (VK 179) is a toggle, so
        resume is intentionally the same key event as pause — document it
        explicitly instead of silently aliasing."""
        return self.pause()

    def next_track(self):
        if self._queue and self._current_index + 1 < len(self._queue):
            self._current_index += 1
            return self.play_song(self._queue[self._current_index]['path'])
        # Fallback to media key Next Track
        try:
            ps = "$wsh = New-Object -ComObject WScript.Shell; $wsh.SendKeys([char]176)"
            subprocess.run(['powershell', '-Command', ps], capture_output=True, timeout=5)
            return True
        except Exception:
            return False

    def prev_track(self):
        if self._queue and self._current_index > 0:
            self._current_index -= 1
            return self.play_song(self._queue[self._current_index]['path'])
        try:
            ps = "$wsh = New-Object -ComObject WScript.Shell; $wsh.SendKeys([char]177)"
            subprocess.run(['powershell', '-Command', ps], capture_output=True, timeout=5)
            return True
        except Exception:
            return False

    def queue_song(self, song_name):
        matches = self.search_song(song_name)
        if not matches:
            return False, f"Could not find '{song_name}' to queue."
        song = matches[0]
        self._queue.append(song)
        if self._current_index == -1:
            self._current_index = 0
        return True, f"Queued: {song['name']} (position {len(self._queue)})"

    def get_queue(self):
        if not self._queue:
            return "Queue is empty."
        lines = []
        for i, s in enumerate(self._queue):
            marker = "▶" if i == self._current_index else f"{i+1}."
            lines.append(f"{marker} {s['name']}")
        return "\n".join(lines)


def parse_media_command(command):
    """
    Parse a natural language media command.
    
    Examples:
        "play honey sing song from brave" -> {'action': 'play', 'query': 'honey sing song brave'}
        "play brave soundtrack" -> {'action': 'play', 'query': 'brave soundtrack'}
        "volume up" -> {'action': 'volume', 'direction': 'up'}
        "set volume to 50" -> {'action': 'volume', 'level': 50}
    
    Args:
        command: Natural language command string
        
    Returns:
        Dictionary with parsed command details
    """
    cmd_lower = command.lower().strip()
    
    # Play commands
    play_patterns = [
        r'play\s+(?:the\s+)?(.+)',
        r'(?:can you )?play\s+(.+)',
        r'(?:i want to hear|let me hear)\s+(.+)',
    ]
    
    for pattern in play_patterns:
        match = re.search(pattern, cmd_lower)
        if match:
            query = match.group(1).strip()
            # Clean up common phrases
            query = re.sub(r'\b(from the movie|from movie|from|soundtrack)\b', '', query)
            query = query.strip()
            return {'action': 'play', 'query': query}
    
    # Volume commands
    if 'volume up' in cmd_lower or 'turn up' in cmd_lower or 'increase volume' in cmd_lower:
        return {'action': 'volume', 'direction': 'up', 'amount': 10}
    if 'volume down' in cmd_lower or 'turn down' in cmd_lower or 'decrease volume' in cmd_lower:
        return {'action': 'volume', 'direction': 'down', 'amount': 10}
    
    vol_match = re.search(r'(?:set\s+)?volume\s+(?:to\s+)?(\d+)', cmd_lower)
    if vol_match:
        return {'action': 'volume', 'level': int(vol_match.group(1))}
    
    # Pause / resume
    if cmd_lower in ('pause', '/pause', 'resume', '/resume'):
        return {'action': 'pause'}
    if cmd_lower in ('next', '/next', 'skip', '/skip', 'next track'):
        return {'action': 'next'}
    if cmd_lower in ('prev', '/prev', 'previous', '/previous', 'prev track'):
        return {'action': 'prev'}
    if 'queue' in cmd_lower:
        q = re.sub(r'.*queue\s*', '', cmd_lower).strip()
        if q:
            return {'action': 'queue', 'query': q}
        return {'action': 'queue_list'}
    if cmd_lower in ('queue', '/queue', 'show queue'):
        return {'action': 'queue_list'}

    # Stop/pause commands
    if any(word in cmd_lower for word in ['stop', 'pause', "that's all"]):
        return {'action': 'stop'}
    
    # List commands
    if any(phrase in cmd_lower for phrase in ['list songs', 'what songs', 'available songs']):
        return {'action': 'list'}
    
    return {'action': 'unknown', 'original': command}


def handle_media_command(controller, command):
    """
    Handle a media command and return a response.
    
    Args:
        controller: MediaController instance
        command: Natural language command
        
    Returns:
        Tuple of (response_text, success)
    """
    parsed = parse_media_command(command)
    action = parsed.get('action')
    
    if action == 'play':
        query = parsed.get('query', '')
        success, message = controller.play_song_by_name(query)
        return message, success
        
    elif action == 'volume':
        if 'level' in parsed:
            level = parsed['level']
            success = controller.set_volume(level)
            if success:
                return f"Volume set to {level}%", True
            else:
                return "Failed to set volume.", False
        elif 'direction' in parsed:
            # Get current volume and adjust
            current = controller.get_volume() or 50
            amount = parsed.get('amount', 10)
            if parsed['direction'] == 'up':
                new_level = min(100, current + amount)
            else:
                new_level = max(0, current - amount)
            success = controller.set_volume(new_level)
            if success:
                return f"Volume adjusted to {new_level}%", True
            else:
                return "Failed to adjust volume.", False
                
    elif action == 'pause':
        ok = controller.pause()
        return ("Paused." if ok else "Could not pause."), ok
    elif action == 'next':
        ok = controller.next_track()
        return ("Skipped to next." if ok else "Could not skip."), ok
    elif action == 'prev':
        ok = controller.prev_track()
        return ("Went to previous." if ok else "Could not go back."), ok
    elif action == 'queue':
        ok, msg = controller.queue_song(parsed.get('query',''))
        return msg, ok
    elif action == 'queue_list':
        return controller.get_queue(), True
    elif action == 'stop':
        # Try to stop current playback
        try:
            subprocess.run(['taskkill', '/IM', 'vlc.exe', '/FI', 'WINDOWTITLE eq VLC*'],
                         capture_output=True, timeout=5)
            return "Playback stopped.", True
        except:
            return "Could not stop playback.", False
            
    elif action == 'list':
        songs = controller.get_song_list(10)
        if songs:
            return f"Available songs: {', '.join(songs[:5])}{'...' if len(songs) > 5 else ''}", True
        else:
            return "No songs found in your music library.", True
            
    else:
        return f"Sorry, I don't understand the media command: {command}", False


if __name__ == "__main__":
    # Test the media controller
    print("Jarvis Media Controller Test")
    print("=" * 40)
    
    controller = MediaController()
    
    # Show library stats
    stats = controller.get_library_stats()
    print(f"Music Library: {stats['total_songs']} songs in {stats['total_albums']} albums")
    print(f"Scanned directories: {stats['scanned_dirs']}")
    
    # Test search
    if stats['total_songs'] > 0:
        print("\nFirst 5 songs in library:")
        for song in controller.get_song_list(5):
            print(f"  - {song}")
    
    # Test parsing
    test_commands = [
        "play honey sing song from brave",
        "play brave soundtrack",
        "volume up",
        "set volume to 50",
        "stop",
    ]
    
    print("\nCommand parsing test:")
    for cmd in test_commands:
        parsed = parse_media_command(cmd)
        print(f"  '{cmd}' -> {parsed}")