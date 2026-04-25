import unittest
import tempfile
import shutil
import argparse
from unittest.mock import MagicMock
from pathlib import Path
import time
from android_battery_optimizer.adb import AdbClient, CommandRunner, CommandResult
from android_battery_optimizer.app import BatteryOptimizerApp
from android_battery_optimizer.cli import BatteryOptimizerCLI
from android_battery_optimizer.diagnose import Diagnoser

class FakeRunner(CommandRunner):
    def __init__(self):
        self.commands = []
        self.responses = {}

    def run(self, args, input_data=None, timeout=None):
        cmd_str = " ".join(map(str, args))
        if input_data:
            for line in input_data.splitlines():
                if line.strip():
                    self.commands.append(line.strip())
        self.commands.append(cmd_str)
        if cmd_str in self.responses:
            res = self.responses[cmd_str]
            if isinstance(res, Exception):
                raise res
            return res
        return CommandResult(0, "", "")

    def which(self, name):
        return "/usr/bin/" + name

class TestNewRequirements(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())
        self.runner = FakeRunner()
        self.client = AdbClient(self.runner, serial="test_device", output=lambda x: None)
        self.app = BatteryOptimizerApp(self.client, self.test_dir)
        self.app.recorder.verify = False
        
        self.cli_outputs = []
        def capture_output(msg):
            self.cli_outputs.append(msg)
        self.cli = BatteryOptimizerCLI(self.app, output=capture_output)

        self.setup_default_responses()

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def setup_default_responses(self):
        self.runner.responses["adb -s test_device shell getprop ro.product.brand"] = CommandResult(0, "Google", "")
        self.runner.responses["adb -s test_device shell getprop ro.build.version.sdk"] = CommandResult(0, "30", "")
        
        self.runner.responses["adb -s test_device shell pm list packages"] = CommandResult(0, "package:com.example.app\npackage:com.example.recent\npackage:com.example.kept", "")
        self.runner.responses["adb -s test_device shell pm list packages -3"] = CommandResult(0, "package:com.example.app\npackage:com.example.recent\npackage:com.example.kept", "")
        
        self.runner.responses["adb -s test_device shell pm list packages --user 0 -d"] = CommandResult(0, "", "")
        self.runner.responses["adb -s test_device shell pm list packages --user 0 -e"] = CommandResult(0, "package:com.example.app\npackage:com.example.recent\npackage:com.example.kept", "")
        
        # com.example.app is aggressive restrict, com.example.recent is restrict, com.example.kept is keep
        self.runner.responses["adb devices"] = CommandResult(0, "List of devices attached\ntest_device\tdevice\n", "")
        self.runner.responses["adb -s test_device shell dumpsys appops"] = CommandResult(0, 
            "Package com.example.app:\n  RUN_ANY_IN_BACKGROUND: allow\n"
            "Package com.example.recent:\n  RUN_ANY_IN_BACKGROUND: allow\n"
            "Package com.example.kept:\n  RUN_ANY_IN_BACKGROUND: allow", "")
        self.runner.responses["adb -s test_device shell cmd appops help"] = CommandResult(0, "help", "")
        self.runner.responses["adb -s test_device shell am get-standby-bucket android"] = CommandResult(0, "10", "")
        
        self.runner.responses["adb -s test_device shell am get-standby-bucket com.example.app"] = CommandResult(0, "active", "")
        self.runner.responses["adb -s test_device shell cmd appops get com.example.app RUN_ANY_IN_BACKGROUND"] = CommandResult(0, "allow", "")
        
        self.runner.responses["adb -s test_device shell am get-standby-bucket com.example.recent"] = CommandResult(0, "active", "")
        self.runner.responses["adb -s test_device shell cmd appops get com.example.recent RUN_ANY_IN_BACKGROUND"] = CommandResult(0, "allow", "")
        
        self.runner.responses["adb -s test_device shell am get-standby-bucket com.example.kept"] = CommandResult(0, "active", "")
        self.runner.responses["adb -s test_device shell cmd appops get com.example.kept RUN_ANY_IN_BACKGROUND"] = CommandResult(0, "allow", "")

        self.runner.responses["adb -s test_device shell dumpsys alarm"] = CommandResult(0, "com.example.app\ncom.example.recent", "")
        self.runner.responses["adb -s test_device shell dumpsys jobscheduler"] = CommandResult(0, "com.example.app", "")
        self.runner.responses["adb -s test_device shell dumpsys batterystats --charged"] = CommandResult(0, "com.example.app", "")
        
        self.runner.responses["adb -s test_device shell dumpsys deviceidle"] = CommandResult(0, "", "")
        
        now_ms = int(time.time() * 1000)
        recent_ms = now_ms - (2 * 86400 * 1000) # 2 days ago
        self.runner.responses["adb -s test_device shell dumpsys usagestats"] = CommandResult(0, 
            f"package=com.example.recent lastTimeUsed=\"{recent_ms}\" bucket=10\n"
            "package=com.example.app lastTimeUsed=\"unparsed_string\" bucket=10\n"
            "package=com.example.kept bucket=10", "")
            
        self.runner.responses["adb -s test_device shell cmd package resolve-activity -a android.intent.action.MAIN -c android.intent.category.HOME"] = CommandResult(0, "", "")
        self.runner.responses["adb -s test_device shell telecom get-default-dialer"] = CommandResult(0, "", "")

    def test_smart_restrict_returns_applied_skipped_kept_warnings(self):
        # Trigger some warning
        self.runner.responses["adb -s test_device shell dumpsys deviceidle"] = Exception("failed")
        
        res = self.app.smart_restrict(aggressive=False)
        
        self.assertIn("applied", res)
        self.assertIn("skipped", res)
        self.assertIn("kept", res)
        self.assertIn("warnings", res)
        
        self.assertTrue(any("dumpsys deviceidle failed" in w for w in res["warnings"]))
        
        applied_pkgs = [r["package"] for r in res["applied"]]
        self.assertIn("com.example.app", applied_pkgs)
        self.assertIn("com.example.recent", applied_pkgs)
        
        kept_pkgs = [r["package"] for r in res["kept"]]
        self.assertIn("com.example.kept", kept_pkgs)

    def test_smart_restrict_cli_prints_applied_packages(self):
        args = argparse.Namespace(command="smart-restrict", yes=True, dry_run=False, aggressive=False, min_last_used_days=None)
        self.cli.run_command(args)
        
        out = "\n".join(self.cli_outputs)
        self.assertIn("Restricted: 2", out)
        self.assertIn("Kept: 1", out)
        self.assertIn("com.example.app -> RUN_ANY_IN_BACKGROUND=ignore, bucket=rare", out)

    def test_smart_restrict_cli_prints_diagnostics_warnings(self):
        self.runner.responses["adb -s test_device shell dumpsys deviceidle"] = Exception("failed")
        args = argparse.Namespace(command="smart-restrict", yes=True, dry_run=False, aggressive=False, min_last_used_days=None)
        self.cli.run_command(args)
        
        out = "\n".join(self.cli_outputs)
        self.assertIn("Warnings:", out)
        self.assertIn("dumpsys deviceidle failed", out)

    def test_min_last_used_days_skips_recently_used_package_with_reason(self):
        res = self.app.smart_restrict(aggressive=False, min_last_used_days=7)
        
        skipped = {s["package"]: s["reason"] for s in res["skipped"]}
        self.assertIn("com.example.recent", skipped)
        self.assertEqual(skipped["com.example.recent"], "recently_used")
        
        applied_pkgs = [r["package"] for r in res["applied"]]
        self.assertNotIn("com.example.recent", applied_pkgs)

    def test_min_last_used_days_skips_unknown_last_used_with_reason(self):
        res = self.app.smart_restrict(aggressive=False, min_last_used_days=7)
        
        skipped = {s["package"]: s["reason"] for s in res["skipped"]}
        self.assertIn("com.example.app", skipped)
        self.assertEqual(skipped["com.example.app"], "last_used_unknown")

    def test_diagnose_last_used_parses_epoch_ms(self):
        diag = Diagnoser(self.client)
        res = diag.run(third_party_only=True)
        
        for pkg in res["packages"]:
            if pkg["package"] == "com.example.recent":
                last_used = pkg["signals"]["last_used"]
                self.assertTrue(last_used["parsed"])
                self.assertIsNotNone(last_used["epoch_ms"])

    def test_diagnose_last_used_reports_unparsed_human_time(self):
        diag = Diagnoser(self.client)
        res = diag.run(third_party_only=True)
        
        for pkg in res["packages"]:
            if pkg["package"] == "com.example.app":
                last_used = pkg["signals"]["last_used"]
                self.assertFalse(last_used["parsed"])
                self.assertEqual(last_used["raw"], "unparsed_string")
                self.assertIsNone(last_used["epoch_ms"])

    def test_diagnose_reuses_appops_parser_for_colon_format(self):
        self.runner.responses["adb -s test_device shell cmd appops get com.example.kept RUN_ANY_IN_BACKGROUND"] = CommandResult(0, "RUN_ANY_IN_BACKGROUND: mode=ignore; time=+1m", "")
        diag = Diagnoser(self.client)
        res = diag.run(third_party_only=True)
        
        for pkg in res["packages"]:
            if pkg["package"] == "com.example.kept":
                self.assertEqual(pkg["run_any_in_background"], "ignore")

    def test_smart_restrict_dry_run_reports_would_apply_but_does_not_write_state(self):
        args = argparse.Namespace(command="smart-restrict", yes=False, dry_run=True, aggressive=False, min_last_used_days=None)
        
        self.cli.client.dry_run = True
        self.cli.run_command(args)
        
        out = "\n".join(self.cli_outputs)
        self.assertIn("Would restrict (dry-run):", out)
        self.assertIn("com.example.app ->", out)
        self.assertNotIn("Smart restrict applied successfully.", out)
        
        state_file = self.test_dir / "devices" / "test_device" / "state.json"
        self.assertFalse(state_file.exists())