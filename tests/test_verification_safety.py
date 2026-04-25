import unittest
from pathlib import Path
from unittest.mock import MagicMock
import tempfile

from android_battery_optimizer.adb import AdbClient, CommandResult
from android_battery_optimizer.recorder import StateRecorder, VerificationError
from android_battery_optimizer.state import StateStore

class TestVerificationSafety(unittest.TestCase):
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

    def test_verify_setting_read_failure_raises_verification_error(self):
        self.mock_runner.run.return_value = CommandResult(returncode=1, stdout="", stderr="error")
        with self.assertRaises(VerificationError):
            self.recorder.verify_setting("global", "key", "value")

    def test_verify_device_config_read_failure_raises_verification_error(self):
        self.mock_runner.run.return_value = CommandResult(returncode=1, stdout="", stderr="error")
        with self.assertRaises(VerificationError):
            self.recorder.verify_device_config("ns", "key", "value")

    def test_verify_appop_read_failure_raises_verification_error(self):
        self.mock_runner.run.return_value = CommandResult(returncode=1, stdout="", stderr="error")
        with self.assertRaises(VerificationError):
            self.recorder.verify_appop("pkg", "op", "allow")

    def test_verify_appop_unparseable_output_raises_verification_error(self):
        self.mock_runner.run.return_value = CommandResult(returncode=0, stdout="garbage", stderr="")
        with self.assertRaises(VerificationError):
            self.recorder.verify_appop("pkg", "op", "allow")

    def test_verify_standby_bucket_read_failure_raises_verification_error(self):
        self.mock_runner.run.return_value = CommandResult(returncode=1, stdout="", stderr="error")
        with self.assertRaises(VerificationError):
            self.recorder.verify_standby_bucket("pkg", "active")

    def test_delete_setting_does_not_pass_verification_when_readback_fails(self):
        self.mock_runner.run.return_value = CommandResult(returncode=1, stdout="", stderr="device offline")
        with self.assertRaises(VerificationError):
            self.recorder.verify_setting("global", "key", None)

    def test_delete_device_config_does_not_pass_verification_when_readback_fails(self):
        self.mock_runner.run.return_value = CommandResult(returncode=1, stdout="", stderr="device offline")
        with self.assertRaises(VerificationError):
            self.recorder.verify_device_config("ns", "key", None)

if __name__ == "__main__":
    unittest.main()
