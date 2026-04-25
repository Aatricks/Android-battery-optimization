import unittest
from unittest.mock import MagicMock, patch
from pathlib import Path
import tempfile
from android_battery_optimizer.adb import AdbClient, CommandResult
from android_battery_optimizer.app import BatteryOptimizerApp
from android_battery_optimizer.cli import BatteryOptimizerCLI
from android_battery_optimizer.state import StateStore
from android_battery_optimizer.recorder import StateRecorder

class TestTimeoutCoverage(unittest.TestCase):
    def setUp(self):
        self.runner = MagicMock()
        self.client = AdbClient(self.runner)

    def test_non_mutating_adb_shell_uses_default_timeout(self):
        self.runner.run.return_value = CommandResult(0, "", "")
        self.client.shell(["settings", "list", "global"], mutate=False)
        
        args, kwargs = self.runner.run.call_args
        self.assertEqual(kwargs.get("timeout"), AdbClient.DEFAULT_TIMEOUT_SECONDS)

    def test_getprop_uses_default_timeout(self):
        self.runner.run.return_value = CommandResult(0, "val", "")
        self.client.get_device_info_struct()
        
        # Verify that all calls (getprop) used the default timeout
        for call in self.runner.run.call_args_list:
            args, kwargs = call
            self.assertEqual(kwargs.get("timeout"), AdbClient.DEFAULT_TIMEOUT_SECONDS)

    def test_adb_devices_uses_default_timeout(self):
        self.runner.run.return_value = CommandResult(0, "List of devices attached\nserial\tdevice\n", "")
        self.runner.which.return_value = "/usr/bin/adb"
        
        with tempfile.TemporaryDirectory() as tmp:
            app = BatteryOptimizerApp(self.client, Path(tmp))
            cli = BatteryOptimizerCLI(app)
            
            cli.check_environment()
            
            # Find the "adb devices" call
            adb_devices_call = None
            for call in self.runner.run.call_args_list:
                args, kwargs = call
                if args[0] == ["adb", "devices"]:
                    adb_devices_call = call
                    break
            
            self.assertIsNotNone(adb_devices_call)
            self.assertEqual(adb_devices_call[1].get("timeout"), AdbClient.DEFAULT_TIMEOUT_SECONDS)

    def test_bg_dexopt_still_uses_long_timeout(self):
        self.runner.run.return_value = CommandResult(0, "", "")
        
        with tempfile.TemporaryDirectory() as tmp:
            app = BatteryOptimizerApp(self.client, Path(tmp))
            
            app.run_bg_dexopt()
            
            # Find the bg-dexopt-job call
            bg_dexopt_call = None
            for call in self.runner.run.call_args_list:
                args, kwargs = call
                if "bg-dexopt-job" in args[0]:
                    bg_dexopt_call = call
                    break
            
            self.assertIsNotNone(bg_dexopt_call)
            self.assertEqual(bg_dexopt_call[1].get("timeout"), AdbClient.LONG_TIMEOUT_SECONDS)

    def test_explicit_timeout_overrides_default(self):
        self.runner.run.return_value = CommandResult(0, "", "")
        self.client.shell(["some", "command"], timeout=5)
        
        args, kwargs = self.runner.run.call_args
        self.assertEqual(kwargs.get("timeout"), 5)

    def test_support_checks_use_default_timeout(self):
        self.runner.run.return_value = CommandResult(0, "", "")
        
        # Test a few support checks
        self.client.supports_device_config()
        self.client.supports_appops()
        self.client.supports_standby_bucket()
        self.client.supports_settings_namespace("global")
        
        for call in self.runner.run.call_args_list:
            args, kwargs = call
            # All these should be ADB commands (either shell or getprop in supports_standby_bucket)
            self.assertEqual(kwargs.get("timeout"), AdbClient.DEFAULT_TIMEOUT_SECONDS)

    def test_local_non_adb_command_no_default_timeout(self):
        # We should NOT apply ADB timeout to arbitrary local commands
        self.runner.run.return_value = CommandResult(0, "", "")
        self.client.local_text(["ls", "-l"])
        
        args, kwargs = self.runner.run.call_args
        self.assertIsNone(kwargs.get("timeout"))

if __name__ == "__main__":
    unittest.main()
