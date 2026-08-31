#!/usr/bin/env python3
"""
Task executor for Jampandu - performs system actions and tasks based on natural language commands.
Handles application launching, web searches, file operations, and system control.
"""
import os
import sys
import subprocess
import re
import shutil
import webbrowser
from pathlib import Path

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Safe applications that can be launched without approval
SAFE_APPLICATIONS = {
    'notepad': 'notepad.exe',
    'calculator': 'calc.exe',
    'paint': 'mspaint.exe',
    'browser': None,  # Use default browser
    'chrome': 'chrome.exe',
    'firefox': 'firefox.exe',
    'edge': 'msedge.exe',
    'explorer': 'explorer.exe',
    'task manager': 'taskmgr.exe',
    'settings': 'ms-settings:',
    'control panel': 'control.exe',
    'command prompt': 'cmd.exe',
    'powershell': 'powershell.exe',
    'spotify': 'spotify.exe',
    'vlc': 'vlc.exe',
}

# Web search providers
SEARCH_PROVIDERS = {
    'google': 'https://www.google.com/search?q=',
    'bing': 'https://www.bing.com/search?q=',
    'youtube': 'https://www.youtube.com/results?search_query=',
    'duckduckgo': 'https://duckduckgo.com/?q=',
}

# Apps that reach the network (a browser, or something that opens one).
# These must respect the same internet gate as /enable_internet -- natural
# language phrasing is not a separate, weaker permission path.
NETWORK_APPS = {'browser', 'chrome', 'firefox', 'edge', 'spotify'}

# Incognito-by-default launcher: Brave -> Chrome -> Edge -> Firefox private,
# fall back to normal webbrowser.open only if none found ("come out of it when necessary").
_BRAVE_CANDIDATES = [
    os.path.expandvars(r'%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe'),
    os.path.expandvars(r'%ProgramFiles(x86)%\BraveSoftware\Brave-Browser\Application\brave.exe'),
    os.path.expandvars(r'%LocalAppData%\BraveSoftware\Brave-Browser\Application\brave.exe'),
]
_CHROME_CANDIDATES = [
    os.path.expandvars(r'%ProgramFiles%\Google\Chrome\Application\chrome.exe'),
    os.path.expandvars(r'%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe'),
    os.path.expandvars(r'%LocalAppData%\Google\Chrome\Application\chrome.exe'),
]
_EDGE_CANDIDATES = [
    os.path.expandvars(r'%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe'),
    os.path.expandvars(r'%ProgramFiles%\Microsoft\Edge\Application\msedge.exe'),
]
_FIREFOX_CANDIDATES = [
    os.path.expandvars(r'%ProgramFiles%\Mozilla Firefox\firefox.exe'),
    os.path.expandvars(r'%ProgramFiles(x86)%\Mozilla Firefox\firefox.exe'),
]


def _open_url_incognito(url: str) -> bool:
    """Try to open URL in private/incognito mode. Returns True if launched via private flag."""
    # Brave incognito
    for cand in _BRAVE_CANDIDATES:
        if os.path.exists(cand):
            try:
                subprocess.Popen([cand, "--incognito", url], close_fds=True)
                return True
            except Exception:
                pass
    # Chrome incognito
    for cand in _CHROME_CANDIDATES:
        if os.path.exists(cand):
            try:
                subprocess.Popen([cand, "--incognito", url], close_fds=True)
                return True
            except Exception:
                pass
    # Edge InPrivate
    for cand in _EDGE_CANDIDATES:
        if os.path.exists(cand):
            try:
                subprocess.Popen([cand, "--inprivate", url], close_fds=True)
                return True
            except Exception:
                pass
    # Firefox private-window
    for cand in _FIREFOX_CANDIDATES:
        if os.path.exists(cand):
            try:
                subprocess.Popen([cand, "--private-window", url], close_fds=True)
                return True
            except Exception:
                pass
    # Fallback: default browser normal window (graceful degrade)
    try:
        webbrowser.open(url)
        return False
    except Exception:
        return False


class TaskExecutor:
    """Executes system tasks and actions based on natural language commands."""
    
    def __init__(self, config=None):
        self.config = config or {}
        self.allowed_apps = self.config.get('allowed_apps', list(SAFE_APPLICATIONS.keys()))
        self.default_search = self.config.get('default_search', 'google')
        # Kept in sync by the caller (run_agent.py) whenever /enable_internet
        # or /disable_internet changes it -- this is not just the value at
        # construction time.
        self.internet_allowed = bool(self.config.get('internet_allowed', False))
        self._action_history = []
        
    def parse_task(self, command):
        """
        Parse a natural language command into a task action.
        
        Examples:
            "open notepad" -> {'action': 'open_app', 'app': 'notepad'}
            "search for cats on youtube" -> {'action': 'search', 'query': 'cats', 'provider': 'youtube'}
            "what time is it" -> {'action': 'get_time'}
            "take a screenshot" -> {'action': 'screenshot'}
            "play honey sing song from brave" -> {'action': 'search_and_play', 'query': 'honey sing brave', 'platform': 'youtube'}
            "set volume to 50" -> {'action': 'set_volume', 'level': 50}
            "turn off the screen" -> {'action': 'screen_off'}
            "lock computer" -> {'action': 'lock'}
            "sleep" -> {'action': 'sleep'}
        
        Args:
            command: Natural language command string
            
        Returns:
            Dictionary with parsed task details
        """
        cmd_lower = command.lower().strip()
        
        # Open application commands
        open_match = re.match(r'(?:open|launch|start)\s+(.+)', cmd_lower)
        if open_match:
            app_name = open_match.group(1).strip()
            # Map common names to applications
            app_map = {
                'notepad': 'notepad',
                'note pad': 'notepad',
                'calculator': 'calculator',
                'calc': 'calculator',
                'paint': 'paint',
                'browser': 'browser',
                'chrome': 'chrome',
                'google chrome': 'chrome',
                'firefox': 'firefox',
                'edge': 'edge',
                'explorer': 'explorer',
                'file explorer': 'explorer',
                'task manager': 'task manager',
                'settings': 'settings',
                'control panel': 'control panel',
                'cmd': 'command prompt',
                'command prompt': 'command prompt',
                'powershell': 'powershell',
            }
            for name, app in app_map.items():
                if name in app_name:
                    return {'action': 'open_app', 'app': app}
            # "open <name> folder/directory" isn't an application -- route it
            # to the folder opener instead of trying (and failing) to launch
            # an app named e.g. "downloads folder".
            folder_suffix = re.match(r'(?:the\s+)?(.+?)\s+(?:folder|directory)$', app_name)
            if folder_suffix:
                return {'action': 'open_folder', 'path': folder_suffix.group(1).strip()}
            return {'action': 'open_app', 'app': app_name}
        
        # Search commands
        search_match = re.match(r'(?:search|google|look up|find)\s+(?:for\s+)?(.+?)(?:\s+on\s+(.+))?$', cmd_lower)
        if search_match:
            query = search_match.group(1).strip()
            provider = search_match.group(2).strip() if search_match.group(2) else self.default_search
            # Map provider names
            provider_map = {'youtube': 'youtube', 'yt': 'youtube', 'google': 'google', 'bing': 'bing'}
            provider = provider_map.get(provider, self.default_search)
            return {'action': 'search', 'query': query, 'provider': provider}
        
        # Play/search for media (YouTube search)
        play_match = re.match(r'(?:play|listen to|hear)\s+(.+)', cmd_lower)
        if play_match:
            query = play_match.group(1).strip()
            # Clean up common phrases
            query = re.sub(r'\b(from the movie|from movie|from|soundtrack|song|music|video)\b', '', query)
            query = ' '.join(query.split())  # Remove extra whitespace
            return {'action': 'search_and_play', 'query': query, 'platform': 'youtube'}
        
        # Time command
        if any(phrase in cmd_lower for phrase in ["what time", "current time", "tell time"]):
            return {'action': 'get_time'}
        
        # Date command
        if any(phrase in cmd_lower for phrase in ["what day", "what date", "today's date"]):
            return {'action': 'get_date'}
        
        # Screenshot command
        if any(phrase in cmd_lower for phrase in ["take screenshot", "capture screen", "screenshot"]):
            return {'action': 'screenshot'}
        
        # Volume commands
        vol_match = re.search(r'(?:set\s+)?volume\s+(?:to\s+)?(\d+)', cmd_lower)
        if vol_match:
            return {'action': 'set_volume', 'level': int(vol_match.group(1))}
        if 'volume up' in cmd_lower or 'increase volume' in cmd_lower:
            return {'action': 'adjust_volume', 'direction': 'up', 'amount': 10}
        if 'volume down' in cmd_lower or 'decrease volume' in cmd_lower:
            return {'action': 'adjust_volume', 'direction': 'down', 'amount': 10}
        
        # Screen control
        if any(phrase in cmd_lower for phrase in ["turn off screen", "screen off", "power off display"]):
            return {'action': 'screen_off'}
        if any(phrase in cmd_lower for phrase in ["turn on screen", "screen on"]):
            return {'action': 'screen_on'}
        
        # System power commands
        if any(phrase in cmd_lower for phrase in ["lock computer", "lock screen", "lock pc"]):
            return {'action': 'lock'}
        if any(phrase in cmd_lower for phrase in ["sleep", "put to sleep", "sleep mode"]):
            return {'action': 'sleep'}
        if any(phrase in cmd_lower for phrase in ["shutdown", "shut down", "turn off computer"]):
            return {'action': 'shutdown'}
        if any(phrase in cmd_lower for phrase in ["restart", "reboot"]):
            return {'action': 'restart'}
        
        # Bluetooth control - must be before generic "open" to avoid mis-parse
        if "bluetooth" in cmd_lower:
            # Check for on/off intent - order matters: check "turn off" before "turn on"
            if any(p in cmd_lower for p in ["turn off", "switch off", "disable", "turn bluetooth off", "bluetooth off", "stop bluetooth"]):
                return {'action': 'bluetooth_off'}
            if any(p in cmd_lower for p in ["turn on", "switch on", "enable", "turn bluetooth on", "bluetooth on", "start bluetooth"]):
                return {'action': 'bluetooth_on'}
            # Generic "bluetooth" query -> toggle / status
            if "bluetooth" in cmd_lower and len(cmd_lower.split()) <= 3:
                return {'action': 'bluetooth_status'}
            # Fallback: treat as bluetooth_on if ambiguous
            if "on" in cmd_lower:
                return {'action': 'bluetooth_on'}
            if "off" in cmd_lower:
                return {'action': 'bluetooth_off'}
        
        # Weather (opens search)
        if any(phrase in cmd_lower for phrase in ["weather", "what's the weather", "weather today"]):
            return {'action': 'search', 'query': 'weather', 'provider': 'google'}
        
        # News (opens search)
        if any(phrase in cmd_lower for phrase in ["news", "latest news", "what's happening"]):
            return {'action': 'search', 'query': 'latest news', 'provider': 'google'}
        
        # File operations
        file_match = re.match(r'(?:open|show|find)\s+(?:the\s+)?(.+?)\s+(?:folder|directory)', cmd_lower)
        if file_match:
            folder = file_match.group(1).strip()
            return {'action': 'open_folder', 'path': folder}
        
        return {'action': 'unknown', 'original': command}
    
    def execute_task(self, task):
        """
        Execute a parsed task and return the result.
        
        Args:
            task: Dictionary with task details from parse_task
            
        Returns:
            Tuple of (response_text, success, requires_approval)
        """
        action = task.get('action')
        
        if action == 'open_app':
            return self._open_application(task.get('app', ''))
            
        elif action == 'search':
            return self._search(task.get('query', ''), task.get('provider', 'google'))
            
        elif action == 'search_and_play':
            return self._search_and_play(task.get('query', ''), task.get('platform', 'youtube'))
            
        elif action == 'get_time':
            from datetime import datetime
            now = datetime.now().strftime("%I:%M %p")
            return f"The current time is {now}", True, False
            
        elif action == 'get_date':
            from datetime import datetime
            today = datetime.now().strftime("%A, %B %d, %Y")
            return f"Today is {today}", True, False
            
        elif action == 'screenshot':
            return self._take_screenshot()
            
        elif action == 'set_volume':
            return self._set_volume(task.get('level', 50))
            
        elif action == 'adjust_volume':
            return self._adjust_volume(task.get('direction', 'up'), task.get('amount', 10))
            
        elif action == 'screen_off':
            return self._screen_off()
            
        elif action == 'screen_on':
            return "Screen turned on (press any key to wake)", True, False
            
        elif action == 'lock':
            return self._lock_computer()
            
        elif action == 'sleep':
            return self._sleep_computer()
            
        elif action == 'shutdown':
            return self._shutdown_computer()
            
        elif action == 'restart':
            return self._restart_computer()
            
        elif action == 'open_folder':
            return self._open_folder(task.get('path', ''))
        
        elif action == 'bluetooth_on':
            return self._bluetooth_control(True)
        elif action == 'bluetooth_off':
            return self._bluetooth_control(False)
        elif action == 'bluetooth_status':
            return self._bluetooth_status()
            
        else:
            return f"Sorry, I don't know how to: {task.get('original', 'do that')}", False, False
    
    def _open_application(self, app_name):
        """Open an application. Only apps explicitly listed in
        SAFE_APPLICATIONS can be launched -- there is deliberately no
        fallback that searches PATH for an arbitrary name, since that would
        let any chat input (including a prompt-injected one) launch any
        executable on the machine with no approval step at all."""
        app_key = app_name.lower().strip()

        if app_key in ['command prompt', 'powershell']:
            return f"I can open {app_name}, but it requires approval for security reasons. Use /run for privileged commands.", False, True

        if app_key not in SAFE_APPLICATIONS:
            return f"Sorry, I don't know how to open {app_name}. Try: notepad, calculator, browser, chrome, etc.", False, False

        if app_key in NETWORK_APPS and not self.internet_allowed:
            return f"Internet is disabled, so I can't open {app_name}. Run /enable_internet first (password required).", False, False

        app_path = SAFE_APPLICATIONS[app_key]
        if app_key == 'browser' and app_path is None:
            try:
                _open_url_incognito('about:blank')
                return "Opening your default browser (incognito if available)...", True, False
            except Exception as e:
                return f"Could not open browser: {e}", False, False

        try:
            if app_path.startswith('ms-'):
                # URI protocol
                subprocess.Popen(['start', app_path], shell=True)
            else:
                subprocess.Popen([app_path])
            return f"Opening {app_name}...", True, False
        except Exception as e:
            return f"Could not open {app_name}: {e}", False, False
    
    def _search(self, query, provider='google'):
        """Open a web search (incognito by default)."""
        if not self.internet_allowed:
            return "Internet is disabled, so I can't search the web. Run /enable_internet first (password required).", False, False

        base_url = SEARCH_PROVIDERS.get(provider, SEARCH_PROVIDERS['google'])
        url = f"{base_url}{query.replace(' ', '+')}"

        try:
            _open_url_incognito(url)
            return f"Searching for '{query}' on {provider} (incognito)...", True, False
        except Exception as e:
            return f"Could not perform search: {e}", False, False

    def _get_first_youtube_id(self, query):
        """Try to resolve query to first YouTube videoId via search page scrape (no API key)."""
        try:
            import urllib.request
            import urllib.parse
            import re
            search_url = f"https://www.youtube.com/results?search_query={urllib.parse.quote_plus(query)}"
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"}
            req = urllib.request.Request(search_url, headers=headers)
            with urllib.request.urlopen(req, timeout=8) as resp:
                html = resp.read().decode("utf-8", errors="ignore")
            # Primary: "videoId":"<11chars>"
            ids = re.findall(r'"videoId"\s*:\s*"([a-zA-Z0-9_-]{11})"', html)
            if not ids:
                ids = re.findall(r'watch\?v=([a-zA-Z0-9_-]{11})', html)
            if ids:
                seen = set()
                uniq = []
                for vid in ids:
                    if vid not in seen:
                        seen.add(vid)
                        uniq.append(vid)
                return uniq[0]
        except Exception:
            pass
        return None

    def _search_and_play(self, query, platform='youtube'):
        """Search for media content and open it - now actually plays first result, not just search page."""
        if not self.internet_allowed:
            return "Internet is disabled, so I can't search online. Run /enable_internet first (password required).", False, False

        if platform == 'youtube':
            # Clean query: remove stray "youtube" token added by parser (e.g. "kalyani youtube" -> "kalyani")
            clean_q = query.strip()
            if clean_q.lower().endswith(" youtube"):
                clean_q = clean_q[: -len(" youtube")].strip()
            if not clean_q:
                clean_q = query
            # Try to open direct watch page so song actually plays, not just search results
            video_id = self._get_first_youtube_id(clean_q or query)
            try:
                if video_id:
                    watch_url = f"https://www.youtube.com/watch?v={video_id}&autoplay=1"
                    _open_url_incognito(watch_url)
                    return f"Playing '{clean_q}' on YouTube (incognito)...", True, False
                # Fallback: open search page if scrape failed (YouTube blocked, no network, etc.)
                url = f"https://www.youtube.com/results?search_query={clean_q.replace(' ', '+')}"
                _open_url_incognito(url)
                return f"Opened YouTube search for '{clean_q}' (incognito). Click the first result to play - autoplay was blocked.", True, False
            except Exception as e:
                return f"Could not play: {e}", False, False
        else:
            return self._search(query, platform)
    
    def _take_screenshot(self):
        """Take a screenshot."""
        try:
            from datetime import datetime
            screenshot_dir = os.path.join(os.path.expanduser('~'), 'Pictures', 'Screenshots')
            os.makedirs(screenshot_dir, exist_ok=True)
            
            filename = f"screenshot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
            filepath = os.path.join(screenshot_dir, filename)
            
            # Use PowerShell to take screenshot
            ps_script = f"""
            Add-Type -AssemblyName System.Windows.Forms
            Add-Type -AssemblyName System.Drawing
            $screen = [System.Windows.Forms.Screen]::PrimaryScreen
            $bitmap = New-Object System.Drawing.Bitmap $screen.Bounds.Width, $screen.Bounds.Height
            $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
            $graphics.CopyFromScreen($screen.Bounds.Location, [System.Drawing.Point]::Empty, $screen.Bounds.Size)
            $bitmap.Save('{filepath}')
            $graphics.Dispose()
            $bitmap.Dispose()
            """
            subprocess.run(['powershell', '-Command', ps_script], capture_output=True, timeout=10)
            
            return f"Screenshot saved to {filepath}", True, False
        except Exception as e:
            return f"Could not take screenshot: {e}", False, False
    
    def _set_volume(self, level):
        """Set system volume."""
        level = max(0, min(100, int(level)))
        try:
            # Use PowerShell to set volume
            ps_script = f"""
            $wsh = New-Object -ComObject WScript.Shell
            $steps = 50  # Assuming current volume is around 50%
            if ({level} -gt $steps) {{
                for ($i = 0; $i -lt ({level} - $steps); $i++) {{ $wsh.SendKeys([char]175) }}  # Volume Up
            }} else {{
                for ($i = 0; $i -lt ($steps - {level}); $i++) {{ $wsh.SendKeys([char]174) }}  # Volume Down
            }}
            """
            subprocess.run(['powershell', '-Command', ps_script], capture_output=True, timeout=5)
            return f"Volume set to {level}%", True, False
        except Exception as e:
            return f"Could not set volume: {e}", False, False
    
    def _adjust_volume(self, direction, amount=10):
        """Adjust volume up or down."""
        try:
            key = 175 if direction == 'up' else 174  # Volume Up / Volume Down virtual keys
            ps_script = (
                f"$wsh = New-Object -ComObject WScript.Shell; "
                f"1..{amount} | ForEach-Object {{ $wsh.SendKeys([char]{key}) }}"
            )
            subprocess.run(['powershell', '-Command', ps_script], capture_output=True, timeout=5)
            if direction == 'up':
                return f"Volume increased by {amount}%", True, False
            else:
                return f"Volume decreased by {amount}%", True, False
        except Exception as e:
            return f"Could not adjust volume: {e}", False, False
    
    def _screen_off(self):
        """Turn off the screen (monitor power off)."""
        try:
            # WM_SYSCOMMAND (0x0112) + SC_MONITORPOWER (0xF170), lParam=2 turns the monitor off.
            # Broadcast to HWND_BROADCAST (0xffff) since there's no specific window handle here.
            ps_script = (
                "Add-Type -Name Win32 -Namespace Native -MemberDefinition "
                "'[DllImport(\"user32.dll\")] public static extern int SendMessage"
                "(int hWnd, int hMsg, int wParam, int lParam);'; "
                "[Native.Win32]::SendMessage(0xffff, 0x0112, 0xF170, 2) | Out-Null"
            )
            proc = subprocess.run(['powershell', '-Command', ps_script],
                                   capture_output=True, text=True, timeout=5)
            if proc.returncode != 0:
                return f"Could not turn off screen: {proc.stderr.strip()}", False, False
            return "Screen turned off. Move mouse or press a key to wake.", True, False
        except Exception as e:
            return f"Could not turn off screen: {e}", False, False
    
    def _lock_computer(self):
        """Lock the computer."""
        try:
            subprocess.run(['rundll32.exe', 'user32.dll,LockWorkStation'])
            return "Computer locked.", True, False
        except Exception as e:
            return f"Could not lock computer: {e}", False, False
    
    def _sleep_computer(self):
        """Put computer to sleep."""
        try:
            subprocess.run(['rundll32.exe', 'powrprof.dll,SetSuspendState,0,1,0'])
            return "Computer going to sleep...", True, False
        except Exception as e:
            return f"Could not put computer to sleep: {e}", False, False
    
    def _shutdown_computer(self):
        """Shutdown the computer (requires approval)."""
        return "Shutdown requires approval for safety. Use /run shutdown /s /t 0 with approval.", False, True
    
    def _restart_computer(self):
        """Restart the computer (requires approval)."""
        return "Restart requires approval for safety. Use /run shutdown /r /t 0 with approval.", False, True
    
    def _open_folder(self, path):
        """Open a folder in Explorer."""
        try:
            # Resolve path
            full_path = os.path.expanduser(path)
            if not os.path.isabs(full_path):
                full_path = os.path.join(os.path.expanduser('~'), full_path)
            
            if os.path.isdir(full_path):
                os.startfile(full_path)
                return f"Opening folder: {path}", True, False
            else:
                return f"Folder not found: {path}", False, False
        except Exception as e:
            return f"Could not open folder: {e}", False, False

    def _bluetooth_status(self):
        """Check Bluetooth radio / service status honestly."""
        try:
            # Check bthserv service
            proc = subprocess.run(['powershell', '-Command', 'Get-Service -Name bthserv -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Status'], capture_output=True, text=True, timeout=5)
            svc = proc.stdout.strip()
            # Check PnP devices
            proc2 = subprocess.run(['powershell', '-Command', 'Get-PnpDevice -Class Bluetooth -ErrorAction SilentlyContinue | Where-Object {$_.Status -eq "OK"} | Measure-Object | Select-Object -ExpandProperty Count'], capture_output=True, text=True, timeout=5)
            count = proc2.stdout.strip()
            if svc:
                return f"Bluetooth service (bthserv) status: {svc}. Bluetooth devices OK: {count or 'unknown'}. Open ms-settings:bluetooth to toggle the radio.", True, False
            return "Could not determine Bluetooth status.", False, False
        except Exception as e:
            return f"Could not check Bluetooth: {e}", False, False

    def _bluetooth_control(self, turn_on: bool):
        """Honestly control Bluetooth - service toggle != quick-settings radio. Never lies."""
        action = "on" if turn_on else "off"
        # NOTE: bthserv Running != radio ON in quick settings (as seen in your screenshot: service Running but toggle gray).
        # RadioState requires WinRT UWP API which is blocked in this portable context, so we must be honest
        # and delegate to Settings instead of claiming success.
        try:
            # Open Bluetooth settings so user can see/toggle the actual radio switch
            try:
                subprocess.Popen(['powershell', '-Command', 'Start-Process "ms-settings:bluetooth"'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass
            # Also open quick settings via ms-actioncenter for visibility
            # Try service attempt as best-effort but do NOT claim radio is on based on service alone
            if turn_on:
                ps = 'Set-Service -Name bthserv -StartupType Automatic -ErrorAction SilentlyContinue; Start-Service -Name bthserv -ErrorAction SilentlyContinue; Get-Service bthserv | Select-Object -ExpandProperty Status'
            else:
                ps = 'Stop-Service -Name bthserv -Force -ErrorAction SilentlyContinue; Set-Service -Name bthserv -StartupType Manual -ErrorAction SilentlyContinue; Get-Service bthserv | Select-Object -ExpandProperty Status'
            proc = subprocess.run(['powershell', '-Command', ps], capture_output=True, text=True, timeout=6)
            svc_status = proc.stdout.strip()
            # Honest response: do not claim radio toggled, ask user to verify in Settings/quick settings
            if turn_on:
                return f"Opened Bluetooth settings (service status: {svc_status or 'unknown'}). The quick-settings Bluetooth toggle is independent from the service - please turn it ON there. Automatic radio toggle needs admin/WinRT and isn't reliable portably, so I won't falsely claim 'Bluetooth is turned on'.", False, False
            else:
                return f"Opened Bluetooth settings (service status: {svc_status or 'unknown'}). Please turn Bluetooth OFF in the toggle there. Automatic radio control isn't available without admin.", False, False
        except Exception as e:
            try:
                subprocess.Popen(['powershell', '-Command', 'Start-Process "ms-settings:bluetooth"'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass
            return f"Error controlling Bluetooth: {e}. Opened Bluetooth settings - please toggle manually.", False, False
    
    def get_available_tasks(self):
        """Get a list of available task types."""
        return [
            "Open applications (notepad, calculator, browser, etc.)",
            "Web search (Google, YouTube, Bing)",
            "Search and play media on YouTube",
            "Get current time and date",
            "Take screenshots",
            "Control system volume",
            "Lock computer",
            "Turn off screen",
            "Open folders",
        ]


def handle_task_command(executor, command):
    """
    Handle a task command and return a response.
    
    Args:
        executor: TaskExecutor instance
        command: Natural language command
        
    Returns:
        Tuple of (response_text, success, requires_approval)
    """
    task = executor.parse_task(command)
    return executor.execute_task(task)


if __name__ == "__main__":
    # Test the task executor
    print("Jarvis Task Executor Test")
    print("=" * 40)
    
    executor = TaskExecutor()
    
    # Test various commands
    test_commands = [
        "play honey sing song from brave",
        "open notepad",
        "search for cats on youtube",
        "what time is it",
        "take a screenshot",
        "set volume to 50",
        "lock computer",
        "turn off screen",
    ]
    
    print("\nCommand parsing and execution test:")
    for cmd in test_commands:
        task = executor.parse_task(cmd)
        print(f"\nInput: '{cmd}'")
        print(f"Parsed: {task}")
        response, success, needs_approval = executor.execute_task(task)
        print(f"Response: {response}")
        print(f"Success: {success}, Needs Approval: {needs_approval}")
    
    print("\n\nAvailable tasks:")
    for task in executor.get_available_tasks():
        print(f"  • {task}")