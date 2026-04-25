import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
import tempfile
import json

from android_battery_optimizer.cli import main, BatteryOptimizerCLI, parse_args
from android_battery_optimizer.app import BatteryOptimizerApp
from android_battery_optimizer.adb import AdbClient, SubprocessRunner

class TestCLICommands(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    @patch("android_battery_optimizer.cli.BatteryOptimizerCLI.check_environment")
    @patch("android_battery_optimizer.app.BatteryOptimizerApp.get_device_info")
    @patch("android_battery_optimizer.app.BatteryOptimizerApp.apply_experimental_optimizations")
    def test_apply_experimental_requires_yes_noninteractive(self, mock_apply, mock_get_info, mock_check_env):
        mock_check_env.return_value = True
        mock_get_info.return_value = "Google Pixel 6"
        
        outputs = []
        app = BatteryOptimizerApp(client=AdbClient(runner=SubprocessRunner()), state_dir=self.state_dir)
        cli = BatteryOptimizerCLI(app=app, output=outputs.append)
        
        # Test without --yes
        args = parse_args(["apply-experimental"])
        result = cli.run_command(args)
        self.assertEqual(result, 1)
        self.assertIn("Error: --yes is required", outputs[0])
        mock_apply.assert_not_called()
        
        # Test with --yes
        outputs.clear()
        args = parse_args(["apply-experimental", "--yes"])
        result = cli.run_command(args)
        self.assertEqual(result, 0)
        self.assertIn("Experimental optimizations applied", outputs[-1])
        mock_apply.assert_called_once()

    @patch("android_battery_optimizer.cli.BatteryOptimizerCLI.check_environment")
    @patch("android_battery_optimizer.app.BatteryOptimizerApp.apply_documented_safe_optimizations")
    def test_apply_safe_subcommand_calls_app_method(self, mock_apply, mock_check_env):
        mock_check_env.return_value = True
        outputs = []
        app = BatteryOptimizerApp(client=AdbClient(runner=SubprocessRunner()), state_dir=self.state_dir)
        cli = BatteryOptimizerCLI(app=app, output=outputs.append)
        
        args = parse_args(["apply-safe"])
        result = cli.run_command(args)
        self.assertEqual(result, 0)
        mock_apply.assert_called_once()
        self.assertIn("Applying documented safe optimizations", outputs[0])

    @patch("android_battery_optimizer.cli.BatteryOptimizerCLI.check_environment")
    @patch("android_battery_optimizer.app.BatteryOptimizerApp.revert_saved_state")
    def test_revert_subcommand_calls_restore(self, mock_revert, mock_check_env):
        mock_check_env.return_value = True
        mock_revert.return_value = ["Restored something"]
        outputs = []
        app = BatteryOptimizerApp(client=AdbClient(runner=SubprocessRunner()), state_dir=self.state_dir)
        cli = BatteryOptimizerCLI(app=app, output=outputs.append)
        
        args = parse_args(["revert"])
        result = cli.run_command(args)
        self.assertEqual(result, 0)
        mock_revert.assert_called_once()
        self.assertIn("Restoring saved state", outputs[0])
        self.assertIn("Restored something", outputs[1])

    @patch("android_battery_optimizer.cli.BatteryOptimizerCLI.check_environment")
    @patch("android_battery_optimizer.app.BatteryOptimizerApp.get_device_info")
    @patch("android_battery_optimizer.state.StateStore.has_entries")
    def test_status_prints_device_info(self, mock_has_entries, mock_get_info, mock_check_env):
        mock_check_env.return_value = True
        mock_get_info.return_value = "Samsung S21"
        mock_has_entries.return_value = True
        
        outputs = []
        client = AdbClient(runner=SubprocessRunner(), serial="serial123")
        app = BatteryOptimizerApp(client=client, state_dir=self.state_dir)
        cli = BatteryOptimizerCLI(app=app, output=outputs.append)
        
        args = parse_args(["status"])
        result = cli.run_command(args)
        self.assertEqual(result, 0)
        
        output_str = "\n".join(outputs)
        self.assertIn("Selected device: serial123", output_str)
        self.assertIn("Device info: Samsung S21", output_str)
        self.assertIn("Rollback state exists: True", output_str)

    @patch("android_battery_optimizer.cli.BatteryOptimizerCLI.check_environment")
    @patch("android_battery_optimizer.cli.BatteryOptimizerCLI.run")
    def test_default_without_subcommand_still_runs_interactive_menu(self, mock_run, mock_check_env):
        # We need to mock main to avoid full initialization if possible, or just mock BatteryOptimizerCLI.run
        with patch("android_battery_optimizer.cli.BatteryOptimizerCLI.run") as mock_run:
            main([])
            mock_run.assert_called_once()

    @patch("android_battery_optimizer.cli.BatteryOptimizerCLI.check_environment")
    @patch("android_battery_optimizer.adb.subprocess.run")
    def test_dry_run_subcommand_does_not_create_state(self, mock_run, mock_check_env):
        mock_check_env.return_value = True
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        
        outputs = []
        client = AdbClient(runner=SubprocessRunner(), serial="serial1", dry_run=True)
        app = BatteryOptimizerApp(client=client, state_dir=self.state_dir)
        cli = BatteryOptimizerCLI(app=app, output=outputs.append)
        
        # Manually trigger something that would normally save state
        with app.recorder.transaction():
            app.recorder.put_setting("global", "test", "1")
        
        state_file = self.state_dir / "devices" / "serial1" / "state.json"
        self.assertFalse(state_file.exists())
        
        # Now test via cli command
        args = parse_args(["--dry-run", "apply-safe"])
        cli.client.dry_run = True # Ensure it's set
        result = cli.run_command(args)
        self.assertEqual(result, 0)
        self.assertFalse(state_file.exists())

if __name__ == "__main__":
    unittest.main()
