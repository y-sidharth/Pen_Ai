#!/usr/bin/env python3
"""
Voice assistant module for Jarvis - provides speech-to-text and text-to-speech capabilities.
Designed to work offline on Windows using local TTS and optional offline STT.
"""
import os
import sys
import threading
import queue
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Default configuration
DEFAULT_VOICE_CONFIG = {
    "voice_enabled": False,
    "tts_enabled": True,
    "stt_enabled": True,
    "stt_engine": "whisper",  # Options: "whisper", "sphinx", "google" (requires internet)
    "tts_engine": "sapi5",    # Options: "sapi5" (Windows), "dummy" (no-op)
    "tts_rate": 150,
    "tts_volume": 0.9,
    "tts_voice": None,  # None = default system voice
    "wake_word": None,  # Optional wake word for continuous listening
    "energy_threshold": 300,  # Energy threshold for microphone
    "pause_threshold": 0.8,  # Seconds of silence before phrase is considered complete
    "operation_timeout": 5,  # Seconds to wait for operation to complete
}


class VoiceAssistant:
    """Voice input/output handler for Jarvis."""
    
    def __init__(self, config=None):
        """
        Initialize voice assistant.
        
        Args:
            config: Dictionary with voice configuration options
        """
        self.config = DEFAULT_VOICE_CONFIG.copy()
        if config:
            self.config.update(config)
        
        self.speech_recognizer = None
        self.microphone = None
        self.tts_engine = None
        self._initialized = False
        self._listening = False
        self._audio_queue = queue.Queue()
        self._listen_thread = None
        
    def initialize(self):
        """Initialize speech recognition and TTS engines."""
        if self._initialized:
            return True
            
        try:
            # Initialize TTS
            if self.config.get("tts_enabled"):
                self._init_tts()
            
            # Initialize STT
            if self.config.get("stt_enabled"):
                self._init_stt()
            
            self._initialized = True
            return True
        except Exception as e:
            print(f"Voice assistant initialization failed: {e}")
            return False
    
    def _init_tts(self):
        """Initialize text-to-speech engine."""
        engine_type = self.config.get("tts_engine", "sapi5")
        
        if engine_type == "sapi5":
            try:
                import pyttsx3
                self.tts_engine = pyttsx3.init()
                self.tts_engine.setProperty('rate', self.config.get('tts_rate', 150))
                self.tts_engine.setProperty('volume', self.config.get('tts_volume', 0.9))
                
                # Set specific voice if configured
                voice_id = self.config.get('tts_voice')
                if voice_id:
                    self.tts_engine.setProperty('voice', voice_id)
                    
            except ImportError:
                print("pyttsx3 not installed. TTS will be disabled.")
                self.config["tts_enabled"] = False
        elif engine_type == "dummy":
            self.tts_engine = None
        else:
            raise ValueError(f"Unknown TTS engine: {engine_type}")
    
    def _init_stt(self):
        """Initialize speech-to-text engine."""
        try:
            import speech_recognition as sr
            self.speech_recognizer = sr.Recognizer()
            self.speech_recognizer.energy_threshold = self.config.get('energy_threshold', 300)
            self.speech_recognizer.pause_threshold = self.config.get('pause_threshold', 0.8)
            self.speech_recognizer.operation_timeout = self.config.get('operation_timeout', 5)
            
            # Initialize microphone
            try:
                self.microphone = sr.Microphone()
                # Adjust for ambient noise
                with self.microphone as source:
                    self.speech_recognizer.adjust_for_ambient_noise(source, duration=0.5)
            except Exception as e:
                print(f"Microphone not available: {e}")
                self.config["stt_enabled"] = False
                
        except ImportError:
            print("speech_recognition not installed. STT will be disabled.")
            self.config["stt_enabled"] = False
    
    def speak(self, text):
        """
        Convert text to speech.
        
        Args:
            text: Text to speak
            
        Returns:
            True if successful, False otherwise
        """
        if not self.config.get("tts_enabled") or not self.tts_engine:
            return False
        
        try:
            if self.config.get("tts_engine") == "sapi5":
                self.tts_engine.say(text)
                self.tts_engine.runAndWait()
                return True
        except Exception as e:
            print(f"TTS error: {e}")
            return False
        
        return False
    
    def listen(self, timeout=None):
        """
        Listen for speech and convert to text.
        
        Args:
            timeout: Maximum seconds to wait for speech
            
        Returns:
            Recognized text, or None if recognition failed
        """
        if not self.config.get("stt_enabled") or not self.speech_recognizer or not self.microphone:
            return None
        
        try:
            with self.microphone as source:
                # Adjust threshold dynamically if needed
                audio = self.speech_recognizer.listen(source, timeout=timeout or self.config.get('operation_timeout', 5))
            
            return self._recognize(audio)

        except sr.WaitTimeoutError:
            return None
        except Exception as e:
            print(f"STT error: {e}")
            return None
    
    def _recognize(self, audio):
        """
        Recognize speech from audio data.
        
        Args:
            audio: Audio data from speech_recognition
            
        Returns:
            Recognized text or None
        """
        engine = self.config.get("stt_engine", "whisper")
        
        try:
            if engine == "whisper":
                # Try to use Whisper offline
                try:
                    text = self.speech_recognizer.recognize_whisper(audio, model="base", language="en")
                    return text
                except Exception as e:
                    # Fallback to other engines if Whisper fails
                    print(f"Whisper recognition failed: {e}")
                    return self._recognize_fallback(audio)
                    
            elif engine == "sphinx":
                # CMU Sphinx (completely offline, but less accurate)
                text = self.speech_recognizer.recognize_sphinx(audio)
                return text
                
            elif engine == "google":
                # Google (requires internet)
                if self.config.get("internet_allowed", False):
                    text = self.speech_recognizer.recognize_google(audio)
                    return text
                else:
                    print("Google STT requires internet access")
                    return None
            else:
                print(f"Unknown STT engine: {engine}")
                return None
                
        except sr.UnknownValueError:
            return None
        except sr.RequestError as e:
            print(f"STT service error: {e}")
            return None
    
    def _recognize_fallback(self, audio):
        """Fallback recognition methods."""
        # Try Sphinx as fallback
        if self.config.get("stt_engine") != "sphinx":
            try:
                return self.speech_recognizer.recognize_sphinx(audio)
            except:
                pass
        return None
    
    def start_continuous_listening(self, callback=None):
        """
        Start listening in a background thread.
        
        Args:
            callback: Function to call with recognized text
        """
        if not self.config.get("stt_enabled"):
            return
        
        self._listening = True
        self._listen_thread = threading.Thread(
            target=self._continuous_listen_loop,
            args=(callback,),
            daemon=True
        )
        self._listen_thread.start()
    
    def stop_continuous_listening(self):
        """Stop the continuous listening thread."""
        self._listening = False
        if self._listen_thread:
            self._listen_thread.join(timeout=5)
            self._listen_thread = None
    
    def _continuous_listen_loop(self, callback):
        """Background listening loop."""
        while self._listening:
            text = self.listen(timeout=1)
            if text and callback:
                callback(text)
            time.sleep(0.1)
    
    def list_voices(self):
        """List available TTS voices."""
        if not self.tts_engine or self.config.get("tts_engine") != "sapi5":
            return []
        
        try:
            return self.tts_engine.getProperty('voices')
        except:
            return []
    
    def is_available(self):
        """Check if voice capabilities are available."""
        return self.config.get("tts_enabled") or self.config.get("stt_enabled")
    
    def get_status(self):
        """Get current voice assistant status."""
        return {
            "initialized": self._initialized,
            "tts_available": self.tts_engine is not None,
            "stt_available": self.speech_recognizer is not None and self.microphone is not None,
            "listening": self._listening,
            "config": self.config
        }


# Import speech_recognition at module level for error handling
try:
    import speech_recognition as sr
except ImportError:
    sr = None

try:
    import pyttsx3
except ImportError:
    pyttsx3 = None


def check_dependencies():
    """Check if required dependencies are installed."""
    deps = {
        "speech_recognition": sr is not None,
        "pyttsx3": pyttsx3 is not None,
    }
    return deps


def install_dependencies():
    """Print instructions for installing voice dependencies."""
    print("\nVoice assistant dependencies:")
    print("  pip install SpeechRecognition pyttsx3")
    print("\nFor offline Whisper support:")
    print("  pip install openai-whisper")
    print("  (Requires FFmpeg: choco install ffmpeg or download from ffmpeg.org)")
    print("\nFor CMU Sphinx (alternative offline STT):")
    print("  pip install pocketsphinx")


if __name__ == "__main__":
    # Test the voice assistant
    print("Jarvis Voice Assistant Test")
    print("=" * 40)
    
    deps = check_dependencies()
    print("Dependencies:")
    for name, installed in deps.items():
        status = "✓" if installed else "✗"
        print(f"  {status} {name}")
    
    if not all(deps.values()):
        print("\nSome dependencies are missing. Install with:")
        install_dependencies()
        sys.exit(1)
    
    # Initialize and test
    va = VoiceAssistant({
        "tts_enabled": True,
        "stt_enabled": True,
        "stt_engine": "whisper",
    })
    
    if va.initialize():
        print("\nVoice assistant initialized!")
        print("Status:", va.get_status())
        
        # List available voices
        voices = va.list_voices()
        if voices:
            print(f"\nAvailable voices ({len(voices)}):")
            for i, v in enumerate(voices[:5]):  # Show first 5
                print(f"  {i}: {v.name} ({v.id})")
        
        # Test TTS
        print("\nTesting TTS...")
        va.speak("Hello, I am Jarvis, your voice-enabled assistant.")
        
        # Test STT
        print("\nTesting STT - Please say something...")
        text = va.listen(timeout=5)
        if text:
            print(f"You said: {text}")
        else:
            print("No speech detected.")
    else:
        print("Failed to initialize voice assistant.")