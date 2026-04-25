import unittest
from unittest.mock import patch
from android_battery_optimizer.cli import main

class TestCliErgonomics(unittest.TestCase):
    @patch("android_battery_optimizer.cli.BatteryOptimizerCLI.run_command", return_value=0)
    def test_global_dry_run_before_subcommand(self, _mock_run_command):
        with patch("sys.exit") as mock_exit:
            main(["--dry-run", "apply-safe"])
            assert mock_exit.call_count == 0

    @patch("android_battery_optimizer.cli.BatteryOptimizerCLI.run_command", return_value=0)
    def test_global_dry_run_after_subcommand(self, _mock_run_command):
        with patch("sys.exit") as mock_exit:
            main(["apply-safe", "--dry-run"])
            assert mock_exit.call_count == 0

    @patch("android_battery_optimizer.cli.BatteryOptimizerCLI.run_command", return_value=0)
    def test_global_serial_before_subcommand(self, _mock_run_command):
        with patch("sys.exit") as mock_exit:
            main(["--serial", "ABC", "status"])
            assert mock_exit.call_count == 0

    @patch("android_battery_optimizer.cli.BatteryOptimizerCLI.run_command", return_value=0)
    def test_global_serial_after_subcommand(self, _mock_run_command):
        with patch("sys.exit") as mock_exit:
            main(["status", "--serial", "ABC"])
            assert mock_exit.call_count == 0

    @patch("android_battery_optimizer.cli.BatteryOptimizerCLI.run_command", return_value=0)
    def test_restrict_apps_accepts_dry_run_after_subcommand(self, _mock_run_command):
        with patch("sys.exit") as mock_exit:
            main(["restrict-apps", "--level", "ignore", "--yes", "--dry-run"])
            assert mock_exit.call_count == 0

    @patch("android_battery_optimizer.cli.BatteryOptimizerCLI.run_command", return_value=0)
    def test_whitelist_accepts_state_dir_after_subcommand(self, _mock_run_command):
        with patch("sys.exit") as mock_exit:
            main(["whitelist", "list", "--state-dir", "/tmp/state"])
            assert mock_exit.call_count == 0
