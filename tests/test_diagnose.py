import unittest
from unittest.mock import MagicMock
from android_battery_optimizer.adb import AdbClient, CommandRunner, CommandResult, CommandError
from android_battery_optimizer.app import BatteryOptimizerApp
from android_battery_optimizer.diagnose import Diagnoser

class FakeRunner(CommandRunner):
    def __init__(self):
        self.commands = []
        self.responses = {}

    def run(self, args, input_data=None, timeout=None):
        cmd_str = " ".join(map(str, args))
        self.commands.append(cmd_str)
        if cmd_str in self.responses:
            res = self.responses[cmd_str]
            if isinstance(res, Exception):
                raise res
            return res
        return CommandResult(0, "", "")

    def which(self, name):
        return "/usr/bin/" + name

class TestDiagnose(unittest.TestCase):
    def setUp(self):
        self.runner = FakeRunner()
        self.client = AdbClient(self.runner, serial="test_device", output=lambda x: None)
        
        # Setup basic responses
        self.runner.responses["adb -s test_device shell getprop ro.product.brand"] = CommandResult(0, "Google", "")
        self.runner.responses["adb -s test_device shell pm list packages -3"] = CommandResult(0, "package:com.example.app\npackage:com.example.other", "")
        
        self.runner.responses["adb -s test_device shell am get-standby-bucket com.example.app"] = CommandResult(0, "active", "")
        self.runner.responses["adb -s test_device shell cmd appops get com.example.app RUN_ANY_IN_BACKGROUND"] = CommandResult(0, "Uid mode: RUN_ANY_IN_BACKGROUND: allow", "")
        
        self.runner.responses["adb -s test_device shell am get-standby-bucket com.example.other"] = CommandResult(0, "frequent", "")
        self.runner.responses["adb -s test_device shell cmd appops get com.example.other RUN_ANY_IN_BACKGROUND"] = CommandResult(0, "Uid mode: RUN_ANY_IN_BACKGROUND: ignore", "")

        self.runner.responses["adb -s test_device shell dumpsys batterystats --charged"] = CommandResult(0, "com.example.app", "")
        self.runner.responses["adb -s test_device shell dumpsys deviceidle"] = CommandResult(0, "", "")
        self.runner.responses["adb -s test_device shell dumpsys usagestats"] = CommandResult(0, "lastTimeUsed=\"100\"", "")
        self.runner.responses["adb -s test_device shell dumpsys alarm"] = CommandResult(0, "com.example.app", "")
        self.runner.responses["adb -s test_device shell dumpsys jobscheduler"] = CommandResult(0, "com.example.app", "")

    def test_diagnose_does_not_mutate(self):
        diagnoser = Diagnoser(self.client)
        diagnoser.run(third_party_only=True)
        
        # Check no mutate commands
        for cmd in self.runner.commands:
            self.assertNotIn("put", cmd)
            self.assertNotIn("set", cmd)
            self.assertNotIn("disable", cmd)

    def test_diagnose_continues_on_failure(self):
        # Make alarm dumpsys fail
        self.runner.responses["adb -s test_device shell dumpsys alarm"] = CommandError("timeout", CommandResult(-1, "", ""))
        
        diagnoser = Diagnoser(self.client)
        report = diagnoser.run(third_party_only=True)
        
        # Should continue and complete
        self.assertEqual(len(report["packages"]), 2)
        
        # Emit warning
        self.assertTrue(any("alarm failed" in w for w in report["warnings"]))

    def test_diagnose_emits_json_with_warnings(self):
        # Make one command fail
        self.runner.responses["adb -s test_device shell dumpsys alarm"] = CommandError("timeout", CommandResult(-1, "", ""))
        
        diagnoser = Diagnoser(self.client)
        report = diagnoser.run(third_party_only=True)
        
        self.assertIn("device", report)
        self.assertIn("warnings", report)
        self.assertIn("packages", report)
        
        self.assertTrue(len(report["warnings"]) > 0)
        
        pkg = report["packages"][0]
        self.assertIn("recommendation", pkg)
        self.assertIn("signals", pkg)

    def test_diagnose_package_boundary_no_false_positive(self):
        diagnoser = Diagnoser(self.client)
        # Check that 'com.foo' does not match 'com.foobar' or 'com.foo.bar'
        self.assertFalse(diagnoser._has_package_signal("com.foo", "com.foobar"))
        self.assertFalse(diagnoser._has_package_signal("com.foo", "com.foo.bar"))
        self.assertFalse(diagnoser._has_package_signal("com.foo", "a.com.foo"))
        self.assertFalse(diagnoser._has_package_signal("com.foo", "com_foo"))
        self.assertFalse(diagnoser._has_package_signal("com.foo", "com.foo1"))
        
    def test_diagnose_exact_package_signal_detected(self):
        diagnoser = Diagnoser(self.client)
        # Check valid boundaries
        self.assertTrue(diagnoser._has_package_signal("com.foo", "com.foo"))
        self.assertTrue(diagnoser._has_package_signal("com.foo", " com.foo "))
        self.assertTrue(diagnoser._has_package_signal("com.foo", "\ncom.foo\n"))
        self.assertTrue(diagnoser._has_package_signal("com.foo", "uid:com.foo,"))
        self.assertTrue(diagnoser._has_package_signal("com.foo", "package=com.foo "))
        self.assertTrue(diagnoser._has_package_signal("com.foo", "package:com.foo"))
        self.assertTrue(diagnoser._has_package_signal("com.foo", '"com.foo"'))
