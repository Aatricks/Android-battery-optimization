import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
import tempfile

from android_battery_optimizer.adb import AdbClient, CommandError, CommandResult
from android_battery_optimizer.recorder import StateRecorder, VerificationError
from android_battery_optimizer.state import StateStore

class ReproRollbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.tmp_dir.name)
        self.mock_runner = MagicMock()
        self.client = AdbClient(runner=self.mock_runner)
        self.client.serial = "test-device"
        self.store = StateStore(self.state_dir, self.client)
        self.recorder = StateRecorder(self.client, self.store)
        self.recorder.verify = True

    def tearDown(self):
        self.tmp_dir.cleanup()

    @patch("android_battery_optimizer.recorder.re.finditer")
    def test_batched_verification_failure_rolls_back_all_entries(self, mock_finditer):
        # 1. Begin transaction.
        # 2. Queue two settings writes.
        # 3. Simulate batch shell success with SUCCESS_0 and SUCCESS_1.
        # 4. Simulate verification failure for the second setting.
        
        def side_effect(args, **kwargs):
            cmd = " ".join(args) if args else ""
            if "settings list global" in cmd:
                return MagicMock(returncode=0, stdout="s1=old1\ns2=old2\n", stderr="")
            if kwargs.get('input_data') and "SUCCESS_0" in kwargs.get('input_data'):
                # Batch success
                return MagicMock(returncode=0, stdout="SUCCESS_0\nSUCCESS_1\n", stderr="")
            if "settings get global s1" in cmd:
                return MagicMock(returncode=0, stdout="v1\n", stderr="")
            if "settings get global s2" in cmd:
                # Verification failure for second setting
                return MagicMock(returncode=0, stdout="old2\n", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")
        
        self.mock_runner.run.side_effect = side_effect
        # Mock re.finditer to return empty if called on VerificationError result (which doesn't exist)
        mock_finditer.return_value = []

        with self.assertRaises(VerificationError):
            with self.recorder.transaction():
                self.recorder.put_setting("global", "s1", "v1")
                self.recorder.put_setting("global", "s2", "v2")

        # ASSERTIONS:
        # Both settings should be rolled back in reverse order.
        calls = self.mock_runner.run.call_args_list
        rollback_s2 = any("settings put global s2 old2" in " ".join(c[0][0]) for c in calls)
        rollback_s1 = any("settings put global s1 old1" in " ".join(c[0][0]) for c in calls)
        
        self.assertTrue(rollback_s2, "s2 should be rolled back")
        self.assertTrue(rollback_s1, "s1 should be rolled back")
        
        # Assert no state.json remains or state has no entries.
        self.assertFalse(self.store.has_entries(), "State should be empty after successful rollback")

    def test_batched_verification_failure_keeps_only_unresolved_state_when_rollback_fails(self):
        # 1. Same as above, but make rollback of one entry fail.
        
        def side_effect(args, **kwargs):
            cmd = " ".join(args) if args else ""
            if "settings list global" in cmd:
                return MagicMock(returncode=0, stdout="s1=old1\ns2=old2\n", stderr="")
            if kwargs.get('input_data') and "SUCCESS_0" in kwargs.get('input_data'):
                return MagicMock(returncode=0, stdout="SUCCESS_0\nSUCCESS_1\n", stderr="")
            if "settings get global s1" in cmd:
                return MagicMock(returncode=0, stdout="v1\n", stderr="")
            if "settings get global s2" in cmd:
                return MagicMock(returncode=0, stdout="old2\n", stderr="")
            if "settings put global s2 old2" in cmd:
                # Rollback of s2 fails
                raise CommandError(["settings", "put", "global", "s2", "old2"], 
                                   CommandResult(1, "", "perm denied"))
            return MagicMock(returncode=0, stdout="", stderr="")
        
        self.mock_runner.run.side_effect = side_effect

        with self.assertRaises(VerificationError):
            with self.recorder.transaction():
                self.recorder.put_setting("global", "s1", "v1")
                self.recorder.put_setting("global", "s2", "v2")

        # ASSERTIONS:
        # State file should contain only the failed rollback entry (s2).
        self.assertTrue(self.store.has_entries())
        self.assertIn("global/s2", self.store.data["settings"])
        self.assertNotIn("global/s1", self.store.data["settings"])

    def test_non_transactional_verification_failure_clears_state_after_successful_rollback(self):
        # 1. Call put_setting outside transaction.
        # 2. Simulate write success.
        # 3. Simulate verification failure.
        # 4. Simulate rollback success.
        
        def side_effect(args, **kwargs):
            cmd = " ".join(args) if args else ""
            if "settings list global" in cmd:
                return MagicMock(returncode=0, stdout="s1=old1\n", stderr="")
            if "settings put global s1 v1" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            if "settings get global s1" in cmd:
                # Verification failure
                return MagicMock(returncode=0, stdout="old1\n", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")
        
        self.mock_runner.run.side_effect = side_effect

        with self.assertRaises(VerificationError):
            self.recorder.put_setting("global", "s1", "v1")

        # ASSERTIONS:
        # Assert state has no entries.
        self.assertFalse(self.store.has_entries(), "State should be empty after successful rollback")

    def test_non_transactional_verification_failure_keeps_state_if_rollback_fails(self):
        # 1. Same as previous, but rollback command fails.
        
        def side_effect(args, **kwargs):
            cmd = " ".join(args) if args else ""
            if "settings list global" in cmd:
                return MagicMock(returncode=0, stdout="s1=old1\n", stderr="")
            if "settings put global s1 v1" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            if "settings get global s1" in cmd:
                return MagicMock(returncode=0, stdout="old1\n", stderr="")
            if "settings put global s1 old1" in cmd:
                # Rollback fails
                raise CommandError(["settings", "put", "global", "s1", "old1"], 
                                   CommandResult(1, "", "perm denied"))
            return MagicMock(returncode=0, stdout="", stderr="")
        
        self.mock_runner.run.side_effect = side_effect

        with self.assertRaises(VerificationError):
            self.recorder.put_setting("global", "s1", "v1")

        # ASSERTIONS:
        # Assert state still contains the unresolved snapshot.
        self.assertTrue(self.store.has_entries())
        self.assertIn("global/s1", self.store.data["settings"])

if __name__ == "__main__":
    unittest.main()
