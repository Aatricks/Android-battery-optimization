import unittest
import tempfile
import json
import shutil
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

from android_battery_optimizer.adb import (
    AdbClient,
    CommandRunner,
    CommandResult,
    CommandError,
)
from android_battery_optimizer.state import StateStore
from android_battery_optimizer.recorder import (
    StateRecorder,
    SnapshotError,
    VerificationError,
)
from android_battery_optimizer.app import BatteryOptimizerApp

class FakeRunner(CommandRunner):
    def __init__(self):
        self.commands = []
        self.responses = {}
        self.which_responses = {"adb": "/usr/bin/adb"}

    def run(self, args, input_data=None, timeout=None):
        cmd_str = " ".join(map(str, args))
        self.commands.append({
            "args": args,
            "input_data": input_data,
            "timeout": timeout,
            "cmd_str": cmd_str
        })
        
        # Exact match or prefix match for responses
        if cmd_str in self.responses:
            return self.responses[cmd_str]
        
        for pattern, response in self.responses.items():
            if cmd_str.startswith(pattern):
                return response
                
        return CommandResult(0, "", "")

    def which(self, name):
        return self.which_responses.get(name)

class TestSafetyRegressions(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())
        self.runner = FakeRunner()
        # Default device info responses
        self.runner.responses["adb shell getprop ro.product.brand"] = CommandResult(0, "Google", "")
        self.runner.responses["adb shell getprop ro.product.model"] = CommandResult(0, "Pixel 6", "")
        self.runner.responses["adb shell getprop ro.build.version.release"] = CommandResult(0, "13", "")
        self.runner.responses["adb shell getprop ro.build.version.sdk"] = CommandResult(0, "33", "")
        self.runner.responses["adb shell getprop ro.build.fingerprint"] = CommandResult(0, "google/oriole/oriole:13/TP1A.220624.021/8877034:user/release-keys", "")

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def get_client(self, serial=None, dry_run=False):
        return AdbClient(self.runner, serial=serial, dry_run=dry_run, output=lambda x: None)

    def get_app(self, client):
        return BatteryOptimizerApp(client, self.test_dir)

    # 1. Dry-run safety
    def test_dry_run_safety(self):
        client = self.get_client(dry_run=True)
        app = self.get_app(client)
        
        # Mocking support checks
        self.runner.responses["adb shell device_config list"] = CommandResult(0, "namespace/key=value", "")
        
        app.apply_documented_safe_optimizations()
        
        # Check no ADB mutation
        for cmd in self.runner.commands:
            if "device_config put" in cmd["cmd_str"]:
                self.fail(f"Mutation command run in dry-run: {cmd['cmd_str']}")
        
        # Check no state file written
        state_file = self.test_dir / "devices" / "unknown-device" / "state.json"
        self.assertFalse(state_file.exists())

    # 2. Device scoping
    def test_device_scoping(self):
        # Serial 1
        client1 = self.get_client(serial="serial-1")
        app1 = self.get_app(client1)
        app1.recorder.snapshot_setting("global", "test_key", "old_val")
        app1.store.save()
        
        state_file1 = self.test_dir / "devices" / "serial-1" / "state.json"
        self.assertTrue(state_file1.exists())
        
        # Serial 2
        client2 = self.get_client(serial="serial-2")
        app2 = self.get_app(client2)
        self.assertFalse(app2.store.has_entries())
        
        state_file2 = self.test_dir / "devices" / "serial-2" / "state.json"
        self.assertFalse(state_file2.exists())

    def test_restore_refuses_mismatch(self):
        client = self.get_client(serial="serial-1")
        app = self.get_app(client)
        
        # Setup state for serial-1
        app.recorder.snapshot_setting("global", "test_key", "old_val")
        app.store.save()
        
        # Try to restore with serial-2
        client2 = self.get_client(serial="serial-2")
        app2 = self.get_app(client2)
        # We need to manually point app2 to the same state dir but different serial will rebind
        # Actually app2 will look at devices/serial-2 which is empty.
        # Let's force it to look at serial-1's state but with serial-2 client.
        app2.store.path = self.test_dir / "devices" / "serial-1" / "state.json"
        app2.store.data = app2.store._load()
        
        with self.assertRaisesRegex(ValueError, "Device serial mismatch"):
            app2.recorder.restore()

    # 3. Transactions
    def test_transaction_pre_dispatch_exception(self):
        client = self.get_client()
        app = self.get_app(client)
        
        try:
            with app.recorder.transaction():
                app.recorder.snapshot_setting("global", "k1", "v1")
                raise RuntimeError("Interrupt")
        except RuntimeError:
            pass
            
        self.assertFalse(app.store.has_entries())
        state_file = self.test_dir / "devices" / "unknown-device" / "state.json"
        self.assertFalse(state_file.exists())

    def test_transaction_full_success(self):
        client = self.get_client()
        app = self.get_app(client)
        
        # Mock success for batch dispatch
        self.runner.responses["adb shell"] = CommandResult(0, "SUCCESS_0\nSUCCESS_1", "")
        # Mock verification success
        self.runner.responses["adb shell settings get global k1"] = CommandResult(0, "v1_new", "")
        self.runner.responses["adb shell settings get global k2"] = CommandResult(0, "v2_new", "")

        with app.recorder.transaction():
            app.recorder.put_setting("global", "k1", "v1_new")
            app.recorder.put_setting("global", "k2", "v2_new")
            
        self.assertTrue(app.store.has_entries())
        state_file = self.test_dir / "devices" / "unknown-device" / "state.json"
        self.assertTrue(state_file.exists())

    def test_transaction_partial_batch_failure_rollback(self):
        client = self.get_client()
        app = self.get_app(client)
        
        # Ensure snapshotting succeeds
        self.runner.responses["adb shell settings list"] = CommandResult(0, "", "")
        
        # First command succeeds, second fails in batch
        # AdbClient.shell with input_data (the script)
        # The script is: cmd1 && echo SUCCESS_0 || exit $? \n cmd2 && echo SUCCESS_1 || exit $?
        self.runner.responses["adb shell"] = CommandResult(1, "SUCCESS_0\nError on cmd2", "")
        
        # Mock verification calls (though it might not even get there if shell fails)
        # Actually if shell fails, it raises CommandError and transaction() catches it.
        
        with self.assertRaises(CommandError):
            with app.recorder.transaction():
                app.recorder.put_setting("global", "k1", "v1_new") # index 0
                app.recorder.put_setting("global", "k2", "v2_new") # index 1
        
        # Should have attempted rollback for k1 but not k2
        rollback_cmds = [c["cmd_str"] for c in self.runner.commands if "settings put global k1" in c["cmd_str"] or "settings delete global k1" in c["cmd_str"]]
        # One for putting v1_new (in batch), one for rolling back to original (snapshot)
        self.assertTrue(any("settings" in cmd and "k1" in cmd for cmd in rollback_cmds))

    # 4. Snapshot correctness
    def test_snapshot_unknown_package_blocks_mutation(self):
        client = self.get_client()
        app = self.get_app(client)
        # No prefetch or empty prefetch
        with self.assertRaises(SnapshotError):
            app.recorder.set_package_enabled("com.unknown.app", False)

    def test_snapshot_unknown_appop_blocks_mutation(self):
        client = self.get_client()
        app = self.get_app(client)
        # Mock failed appops prefetch
        app.recorder._prefetch_appops_success = False
        with self.assertRaises(SnapshotError):
            app.recorder.set_appop("com.app", "RUN_ANY_IN_BACKGROUND", "ignore")

    def test_snapshot_unknown_standby_bucket_blocks_mutation(self):
        client = self.get_client()
        app = self.get_app(client)
        app.recorder._prefetch_standby_bucket_success = False
        with self.assertRaises(SnapshotError):
            app.recorder.set_standby_bucket("com.app", "rare")

    # 5. Restore
    def test_successful_restore_clears_state(self):
        client = self.get_client()
        app = self.get_app(client)
        
        # Setup some state
        app.recorder.snapshot_setting("global", "test_key", "old_val")
        app.store.save()
        self.assertTrue(app.store.has_entries())
        
        app.recorder.restore()
        self.assertFalse(app.store.has_entries())
        state_file = self.test_dir / "devices" / "unknown-device" / "state.json"
        self.assertFalse(state_file.exists())

    def test_failed_restore_keeps_state(self):
        client = self.get_client()
        app = self.get_app(client)
        
        # Mock initial value so snapshot records it
        self.runner.responses["adb shell settings list global"] = CommandResult(0, "test_key=old_val", "")
        
        # Setup state
        app.recorder.snapshot_setting("global", "test_key")
        app.store.save()
        self.assertTrue(app.store.has_entries())
        
        # Mock failure for the restore command
        self.runner.responses["adb shell settings put global test_key old_val"] = CommandResult(1, "", "Failed")
        
        app.recorder.restore()
        self.assertTrue(app.store.has_entries(), f"State entries should persist after failed restore. Data: {app.store.data}")
        state_file = self.test_dir / "devices" / "unknown-device" / "state.json"
        self.assertTrue(state_file.exists())

    # 6. Command execution
    def test_command_timeout_raises_error(self):
        # We need a runner that actually enforces timeout or we mock it
        with patch("subprocess.run") as mock_run:
            import subprocess
            mock_run.side_effect = subprocess.TimeoutExpired(["adb", "shell"], 5)
            
            from android_battery_optimizer.adb import SubprocessRunner
            real_runner = SubprocessRunner()
            with self.assertRaises(CommandError) as cm:
                real_runner.run(["adb", "shell"], timeout=5)
            self.assertIn("Command timed out", str(cm.exception))

    def test_dry_run_never_invokes_subprocess_for_mutation(self):
        client = self.get_client(dry_run=True)
        # We'll use a real-ish scenario
        client.shell(["settings", "put", "global", "test", "1"], mutate=True)
        
        # runner.commands should contain NO "settings put"
        for cmd in self.runner.commands:
            if "settings put" in cmd["cmd_str"]:
                self.fail("Mutation command invoked subprocess in dry-run")

    # 7. Whitelist
    def test_whitelist_apps_not_mutated(self):
        client = self.get_client()
        app = self.get_app(client)
        
        # Mock support
        self.runner.responses["adb shell cmd appops help"] = CommandResult(0, "", "")
        self.runner.responses["adb shell am set-standby-bucket"] = CommandResult(0, "", "")
        
        # Mock packages
        self.runner.responses["adb shell pm list packages -3"] = CommandResult(0, "package:com.app.mutated\npackage:com.app.whitelisted", "")
        self.runner.responses["adb shell pm list packages"] = CommandResult(0, "package:com.app.mutated\npackage:com.app.whitelisted", "")
        
        # Mock prefetch
        self.runner.responses["adb shell dumpsys appops"] = CommandResult(0, "Package com.app.mutated:\n  RUN_ANY_IN_BACKGROUND: allow\nPackage com.app.whitelisted:\n  RUN_ANY_IN_BACKGROUND: allow", "")
        self.runner.responses["adb shell dumpsys usagestats"] = CommandResult(0, "package=com.app.mutated bucket=active\npackage=com.app.whitelisted bucket=active", "")
        
        app.save_whitelist(["com.app.whitelisted"])
        
        # Mock verification responses for the mutated app
        self.runner.responses["adb shell cmd appops get com.app.mutated RUN_ANY_IN_BACKGROUND"] = CommandResult(0, "mode: ignore", "")
        self.runner.responses["adb shell am get-standby-bucket com.app.mutated"] = CommandResult(0, "40", "") # 40 is rare
        
        app.restrict_background_apps()
        
        # Check that whitelisted app was NOT mutated (no appops set or standby bucket set)
        for cmd in self.runner.commands:
            if "com.app.whitelisted" in cmd["cmd_str"] and ("appops set" in cmd["cmd_str"] or "am set-standby-bucket" in cmd["cmd_str"]):
                self.fail(f"Whitelisted app was mutated: {cmd['cmd_str']}")
                
        # Check that mutated app WAS mutated (should be in batched commands)
        batched = [c["input_data"] for c in self.runner.commands if c["input_data"] and "com.app.mutated" in c["input_data"]]
        self.assertTrue(len(batched) > 0)

if __name__ == "__main__":
    unittest.main()
