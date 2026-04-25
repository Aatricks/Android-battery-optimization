import unittest
from unittest.mock import MagicMock, patch
from android_battery_optimizer.adb import AdbClient, CommandResult, DeviceInfo

class TestAdbStandbyBucket(unittest.TestCase):
    def setUp(self):
        self.runner_mock = MagicMock()
        # Mocking run method to return a valid command result by default
        self.runner_mock.run.return_value = CommandResult(0, "", "")
        self.client = AdbClient(runner=self.runner_mock, serial="test-serial")

    @patch.object(AdbClient, 'get_device_info_struct')
    def test_supports_standby_bucket_false_below_sdk_28(self, mock_info):
        mock_info.return_value = DeviceInfo(
            serial="test", brand="test", model="test", 
            android_release="8.0", sdk_int=27, fingerprint="test"
        )
        self.assertFalse(self.client.supports_standby_bucket())
        self.runner_mock.run.assert_not_called()

    @patch.object(AdbClient, 'get_device_info_struct')
    def test_supports_standby_bucket_true_when_get_command_succeeds(self, mock_info):
        mock_info.return_value = DeviceInfo(
            serial="test", brand="test", model="test", 
            android_release="9.0", sdk_int=28, fingerprint="test"
        )
        self.runner_mock.run.return_value = CommandResult(returncode=0, stdout="10\n", stderr="")
        
        self.assertTrue(self.client.supports_standby_bucket())
        
        self.runner_mock.run.assert_called_once()
        args, kwargs = self.runner_mock.run.call_args
        self.assertEqual(args[0], ["adb", "-s", "test-serial", "shell", "am", "get-standby-bucket", "android"])

    @patch.object(AdbClient, 'get_device_info_struct')
    def test_supports_standby_bucket_false_when_get_command_unknown(self, mock_info):
        mock_info.return_value = DeviceInfo(
            serial="test", brand="test", model="test", 
            android_release="9.0", sdk_int=28, fingerprint="test"
        )
        
        def mock_run(command, **kwargs):
            cmd_str = " ".join(command)
            if "get-standby-bucket" in cmd_str:
                return CommandResult(returncode=255, stdout="", stderr="Unknown command: get-standby-bucket")
            if "help" in cmd_str:
                return CommandResult(returncode=0, stdout="am set-inactive", stderr="")
            return CommandResult(returncode=1, stdout="", stderr="")
            
        self.runner_mock.run.side_effect = mock_run
        
        self.assertFalse(self.client.supports_standby_bucket())
        self.assertEqual(self.runner_mock.run.call_count, 2)

    @patch.object(AdbClient, 'get_device_info_struct')
    def test_supports_standby_bucket_does_not_call_set_standby_bucket(self, mock_info):
        mock_info.return_value = DeviceInfo(
            serial="test", brand="test", model="test", 
            android_release="9.0", sdk_int=28, fingerprint="test"
        )
        self.runner_mock.run.return_value = CommandResult(returncode=0, stdout="10\n", stderr="")
        
        self.client.supports_standby_bucket()
        
        for call in self.runner_mock.run.call_args_list:
            args, kwargs = call
            command = args[0]
            self.assertNotIn("set-standby-bucket", command)

    @patch.object(AdbClient, 'get_device_info_struct')
    def test_supports_standby_bucket_fallback_help(self, mock_info):
        mock_info.return_value = DeviceInfo(
            serial="test", brand="test", model="test", 
            android_release="9.0", sdk_int=28, fingerprint="test"
        )
        
        def mock_run(command, **kwargs):
            cmd_str = " ".join(command)
            if "get-standby-bucket" in cmd_str:
                return CommandResult(returncode=0, stdout="Not numeric output", stderr="")
            if "help" in cmd_str:
                return CommandResult(returncode=0, stdout="am get-standby-bucket\nam set-standby-bucket", stderr="")
            return CommandResult(returncode=1, stdout="", stderr="")
            
        self.runner_mock.run.side_effect = mock_run
        
        self.assertTrue(self.client.supports_standby_bucket())

if __name__ == '__main__':
    unittest.main()
