import unittest
from pathlib import Path
from unittest.mock import MagicMock
import tempfile

from android_battery_optimizer.adb import AdbClient, CommandError, CommandResult
from android_battery_optimizer.recorder import StateRecorder, VerificationError
from android_battery_optimizer.state import StateStore

class ReproRollbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.tmp_dir.name)
        self.mock_runner = MagicMock()
        self.client = AdbClient(runner=self.mock_runner, output=lambda _: None)
        self.client.serial = "test-device"
        self.store = StateStore(self.state_dir, self.client)
        self.recorder = StateRecorder(self.client, self.store)
        self.recorder.verify = True

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_batched_verification_failure_rolls_back_all_entries(self):
        # 1. Begin transaction.
        # 2. Queue two settings writes.
        # 3. Simulate batch shell success with SUCCESS_0 and SUCCESS_1.
        # 4. Simulate verification failure for the second setting.

        def side_effect(args, **kwargs):
            command = tuple(args)
            if command == ("adb", "-s", "test-device", "shell", "settings", "list", "global"):
                return CommandResult(returncode=0, stdout="s1=old1\ns2=old2\n", stderr="")
            if command == ("adb", "-s", "test-device", "shell") and "SUCCESS_0" in kwargs.get("input_data", ""):
                return CommandResult(returncode=0, stdout="SUCCESS_0\nSUCCESS_1\n", stderr="")
            if command == ("adb", "-s", "test-device", "shell", "settings", "get", "global", "s1"):
                return CommandResult(returncode=0, stdout="v1\n", stderr="")
            if command == ("adb", "-s", "test-device", "shell", "settings", "get", "global", "s2"):
                return CommandResult(returncode=0, stdout="old2\n", stderr="")
            if command == ("adb", "-s", "test-device", "shell", "settings", "put", "global", "s2", "old2"):
                return CommandResult(returncode=0, stdout="", stderr="")
            if command == ("adb", "-s", "test-device", "shell", "settings", "put", "global", "s1", "old1"):
                return CommandResult(returncode=0, stdout="", stderr="")
            return CommandResult(returncode=0, stdout="", stderr="")

        self.mock_runner.run.side_effect = side_effect

        with self.assertRaises(VerificationError):
            with self.recorder.transaction():
                self.recorder.put_setting("global", "s1", "v1")
                self.recorder.put_setting("global", "s2", "v2")

        # ASSERTIONS:
        # Both settings should be rolled back in reverse order.
        called_commands = [tuple(call.args[0]) for call in self.mock_runner.run.call_args_list]
        self.assertIn(
            ("adb", "-s", "test-device", "shell", "settings", "put", "global", "s2", "old2"),
            called_commands,
        )
        self.assertIn(
            ("adb", "-s", "test-device", "shell", "settings", "put", "global", "s1", "old1"),
            called_commands,
        )

        # Assert state.json remains clean after a successful rollback.
        self.assertFalse(self.store.has_entries(), "State should be empty after successful rollback")

    def test_batched_verification_failure_keeps_only_unresolved_state_when_rollback_fails(self):
        # 1. Same as above, but make rollback of one entry fail.

        def side_effect(args, **kwargs):
            command = tuple(args)
            if command == ("adb", "-s", "test-device", "shell", "settings", "list", "global"):
                return CommandResult(returncode=0, stdout="s1=old1\ns2=old2\n", stderr="")
            if command == ("adb", "-s", "test-device", "shell") and "SUCCESS_0" in kwargs.get("input_data", ""):
                return CommandResult(returncode=0, stdout="SUCCESS_0\nSUCCESS_1\n", stderr="")
            if command == ("adb", "-s", "test-device", "shell", "settings", "get", "global", "s1"):
                return CommandResult(returncode=0, stdout="v1\n", stderr="")
            if command == ("adb", "-s", "test-device", "shell", "settings", "get", "global", "s2"):
                return CommandResult(returncode=0, stdout="old2\n", stderr="")
            if command == ("adb", "-s", "test-device", "shell", "settings", "put", "global", "s2", "old2"):
                return CommandResult(returncode=1, stdout="", stderr="perm denied")
            if command == ("adb", "-s", "test-device", "shell", "settings", "put", "global", "s1", "old1"):
                return CommandResult(returncode=0, stdout="", stderr="")
            return CommandResult(returncode=0, stdout="", stderr="")

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
            command = tuple(args)
            if command == ("adb", "-s", "test-device", "shell", "settings", "list", "global"):
                return CommandResult(returncode=0, stdout="s1=old1\n", stderr="")
            if command == ("adb", "-s", "test-device", "shell", "settings", "put", "global", "s1", "v1"):
                return CommandResult(returncode=0, stdout="", stderr="")
            if command == ("adb", "-s", "test-device", "shell", "settings", "get", "global", "s1"):
                # Verification failure
                return CommandResult(returncode=0, stdout="old1\n", stderr="")
            if command == ("adb", "-s", "test-device", "shell", "settings", "put", "global", "s1", "old1"):
                return CommandResult(returncode=0, stdout="", stderr="")
            return CommandResult(returncode=0, stdout="", stderr="")

        self.mock_runner.run.side_effect = side_effect

        with self.assertRaises(VerificationError):
            self.recorder.put_setting("global", "s1", "v1")

        # ASSERTIONS:
        # Assert state has no entries.
        self.assertFalse(self.store.has_entries(), "State should be empty after successful rollback")

    def test_non_transactional_verification_failure_keeps_state_if_rollback_fails(self):
        # 1. Same as previous, but rollback command fails.

        def side_effect(args, **kwargs):
            command = tuple(args)
            if command == ("adb", "-s", "test-device", "shell", "settings", "list", "global"):
                return CommandResult(returncode=0, stdout="s1=old1\n", stderr="")
            if command == ("adb", "-s", "test-device", "shell", "settings", "put", "global", "s1", "v1"):
                return CommandResult(returncode=0, stdout="", stderr="")
            if command == ("adb", "-s", "test-device", "shell", "settings", "get", "global", "s1"):
                return CommandResult(returncode=0, stdout="old1\n", stderr="")
            if command == ("adb", "-s", "test-device", "shell", "settings", "put", "global", "s1", "old1"):
                # Rollback fails
                return CommandResult(returncode=1, stdout="", stderr="perm denied")
            return CommandResult(returncode=0, stdout="", stderr="")

        self.mock_runner.run.side_effect = side_effect

        with self.assertRaises(VerificationError):
            self.recorder.put_setting("global", "s1", "v1")

        # ASSERTIONS:
        # Assert state still contains the unresolved snapshot.
        self.assertTrue(self.store.has_entries())
        self.assertIn("global/s1", self.store.data["settings"])

if __name__ == "__main__":
    unittest.main()
