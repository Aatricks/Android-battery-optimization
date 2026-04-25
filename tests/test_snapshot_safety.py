import unittest
from pathlib import Path
from unittest.mock import MagicMock
import tempfile

from android_battery_optimizer.adb import AdbClient, CommandResult
from android_battery_optimizer.recorder import StateRecorder, SnapshotError
from android_battery_optimizer.state import StateStore

class TestSnapshotSafety(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.tmp_dir.name)
        self.mock_runner = MagicMock()
        self.client = AdbClient(runner=self.mock_runner, output=lambda _: None)
        self.client.serial = "test-device"
        self.store = StateStore(self.state_dir, self.client)
        self.recorder = StateRecorder(self.client, self.store)
        self.recorder.verify = False # Focus on snapshot safety, not verification

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_setting_snapshot_read_failure_blocks_mutation(self):
        # Simulate 'settings list global' returning nonzero (failure)
        self.mock_runner.run.return_value = CommandResult(returncode=1, stdout="", stderr="some error")

        with self.assertRaises(SnapshotError) as cm:
            self.recorder.put_setting("global", "test_key", "test_value")

        # Verify SnapshotError message contains namespace/key and error details
        self.assertIn("global", str(cm.exception))
        self.assertIn("test_key", str(cm.exception))
        # Depending on implementation, it might contain "some error"

        # Assert no 'settings put' command was executed
        for call in self.mock_runner.run.call_args_list:
            args = " ".join(call[0][0])
            self.assertNotIn("settings put", args)

        # Assert no state was saved in the store
        self.assertFalse(self.store.has_entries())

    def test_device_config_snapshot_read_failure_blocks_mutation(self):
        # Simulate 'device_config list namespace' returning nonzero
        self.mock_runner.run.return_value = CommandResult(returncode=1, stdout="", stderr="some error")

        with self.assertRaises(SnapshotError) as cm:
            self.recorder.put_device_config("test_ns", "test_key", "test_value")

        self.assertIn("test_ns", str(cm.exception))
        self.assertIn("test_key", str(cm.exception))

        for call in self.mock_runner.run.call_args_list:
            args = " ".join(call[0][0])
            self.assertNotIn("device_config put", args)

        self.assertFalse(self.store.has_entries())

    def test_missing_setting_after_successful_list_is_snapshot_as_none(self):
        # Simulate successful settings list without the target key
        def side_effect(args, **kwargs):
            cmd = " ".join(args)
            if "settings list global" in cmd:
                return CommandResult(returncode=0, stdout="other_key=value\n", stderr="")
            if "settings put global test_key test_value" in cmd:
                return CommandResult(returncode=0, stdout="", stderr="")
            return CommandResult(returncode=0, stdout="", stderr="")

        self.mock_runner.run.side_effect = side_effect

        self.recorder.put_setting("global", "test_key", "test_value")

        # Assert old value is stored as None in the state store
        snapshot_key = "global/test_key"
        self.assertIn(snapshot_key, self.store.data["settings"])
        self.assertIsNone(self.store.data["settings"][snapshot_key]["value"])

    def test_missing_device_config_after_successful_list_is_snapshot_as_none(self):
        # Simulate successful device_config list without the target key
        def side_effect(args, **kwargs):
            cmd = " ".join(args)
            if "device_config list test_ns" in cmd:
                return CommandResult(returncode=0, stdout="other_key=value\n", stderr="")
            if "device_config put test_ns test_key test_value" in cmd:
                return CommandResult(returncode=0, stdout="", stderr="")
            return CommandResult(returncode=0, stdout="", stderr="")

        self.mock_runner.run.side_effect = side_effect

        self.recorder.put_device_config("test_ns", "test_key", "test_value")

        # Assert old value is stored as None in the state store
        snapshot_key = "test_ns/test_key"
        self.assertIn(snapshot_key, self.store.data["device_config"])
        self.assertIsNone(self.store.data["device_config"][snapshot_key]["value"])

if __name__ == "__main__":
    unittest.main()
