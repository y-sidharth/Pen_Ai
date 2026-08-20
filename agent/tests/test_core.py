import os
import sys
import unittest

AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, AGENT_DIR)

from command_policy import validate_command
from security import make_verifier, verify_verifier
from run_agent import build_media_config, _pid_is_running


class CredentialTests(unittest.TestCase):
    def test_verifier_accepts_only_the_original_secret(self):
        verifier = make_verifier("correct horse battery staple")
        self.assertTrue(verify_verifier("correct horse battery staple", verifier))
        self.assertFalse(verify_verifier("incorrect", verifier))

    def test_empty_secret_is_rejected(self):
        with self.assertRaises(ValueError):
            make_verifier("")


class MediaConfigTests(unittest.TestCase):
    def test_missing_music_dirs_are_omitted_so_defaults_apply(self):
        # Regression test: passing an explicit empty list used to shadow
        # MediaController's own sensible defaults (~/Music, etc.) because
        # dict.get('music_dirs', DEFAULT) returns [] when the key is present
        # but empty, rather than falling through to DEFAULT.
        cfg = {}
        media_config = build_media_config(cfg)
        self.assertNotIn('music_dirs', media_config)

    def test_configured_music_dirs_are_forwarded(self):
        cfg = {'music_dirs': ['D:/MyMusic']}
        media_config = build_media_config(cfg)
        self.assertEqual(media_config['music_dirs'], ['D:/MyMusic'])

    def test_media_player_defaults_to_vlc(self):
        media_config = build_media_config({})
        self.assertEqual(media_config['media_player'], 'vlc')


class PidLivenessTests(unittest.TestCase):
    def test_current_process_is_reported_running(self):
        import os
        self.assertTrue(_pid_is_running(os.getpid()))

    def test_implausible_pid_is_not_running(self):
        # PID 0 is reserved (System Idle Process) and OpenProcess with our
        # limited query rights should not report it as a live, queryable
        # process for this check.
        self.assertFalse(_pid_is_running(999999999))


class CommandPolicyTests(unittest.TestCase):
    def test_safe_diagnostic_command_is_accepted(self):
        argv, reason = validate_command("whoami /all")
        self.assertEqual(argv, ["whoami", "/all"])
        self.assertIsNone(reason)

    def test_shell_chaining_is_rejected(self):
        argv, reason = validate_command("whoami & del C:\\important.txt")
        self.assertIsNone(argv)
        self.assertIn("not allowed", reason)

    def test_unknown_program_is_rejected(self):
        argv, reason = validate_command("powershell -Command Get-ChildItem")
        self.assertIsNone(argv)
        self.assertIn("allowlist", reason)

    def test_unapproved_arguments_are_rejected(self):
        argv, reason = validate_command("hostname /all")
        self.assertIsNone(argv)
        self.assertIn("does not allow arguments", reason)


if __name__ == "__main__":
    unittest.main()
