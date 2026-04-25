import unittest
import tempfile
import json
import shutil
import os
from pathlib import Path
from unittest.mock import patch

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

        # Exact match only so malformed command shapes do not slip through.
        if cmd_str in self.responses:
            return self.responses[cmd_str]

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

    def set_response(self, command, returncode=0, stdout="", stderr=""):
        self.runner.responses[command] = CommandResult(returncode, stdout, stderr)

    def make_saved_state_app(self, state_data, serial="test-device"):
        client = self.get_client(serial=serial)
        app = self.get_app(client)
        app.store.data = json.loads(json.dumps(state_data))
        app.store.save()
        return app, state_data.get("device", {})

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
        client1 = self.get_client(serial="test-device")
        app1 = self.get_app(client1)
        app1.recorder.snapshot_setting("global", "test_key", "old_val")
        app1.store.save()

        state_file1 = self.test_dir / "devices" / "test-device" / "state.json"
        self.assertTrue(state_file1.exists())

        # Serial 2
        client2 = self.get_client(serial="test-device-2")
        app2 = self.get_app(client2)
        self.assertFalse(app2.store.has_entries())

        state_file2 = self.test_dir / "devices" / "test-device-2" / "state.json"
        self.assertFalse(state_file2.exists())

    def test_restore_refuses_mismatch(self):
        client = self.get_client(serial="test-device")
        app = self.get_app(client)

        # Setup state for test-device
        app.recorder.snapshot_setting("global", "test_key", "old_val")
        app.store.save()

        # Try to restore with a different serial
        client2 = self.get_client(serial="test-device-2")
        app2 = self.get_app(client2)
        # Force it to look at test-device's state but with test-device-2 client.
        app2.store.path = self.test_dir / "devices" / "test-device" / "state.json"
        app2.store.data = app2.store._load()

        with self.assertRaisesRegex(ValueError, "Device serial mismatch"):
            app2.recorder.restore()

    # 3. Transactions
    def test_transaction_pre_dispatch_exception(self):
        client = self.get_client(serial="test-device")
        app = self.get_app(client)

        with self.assertRaises(RuntimeError) as cm:
            with app.recorder.transaction():
                app.recorder.snapshot_setting("global", "k1", "v1")
                raise RuntimeError("Interrupt")

        self.assertIs(type(cm.exception), RuntimeError)
        self.assertEqual(str(cm.exception), "Interrupt")

        self.assertFalse(app.store.has_entries())
        state_file = self.test_dir / "devices" / "test-device" / "state.json"
        self.assertFalse(state_file.exists())

    def test_transaction_full_success(self):
        client = self.get_client(serial="test-device")
        app = self.get_app(client)

        # Mock success for batch dispatch
        self.runner.responses["adb -s test-device shell"] = CommandResult(0, "SUCCESS_0\nSUCCESS_1", "")
        # Mock verification success
        self.runner.responses["adb -s test-device shell settings get global k1"] = CommandResult(0, "v1_new", "")
        self.runner.responses["adb -s test-device shell settings get global k2"] = CommandResult(0, "v2_new", "")

        with app.recorder.transaction():
            app.recorder.put_setting("global", "k1", "v1_new")
            app.recorder.put_setting("global", "k2", "v2_new")

        self.assertTrue(app.store.has_entries())
        state_file = self.test_dir / "devices" / "test-device" / "state.json"
        self.assertTrue(state_file.exists())

    def test_transaction_partial_batch_failure_rollback(self):
        client = self.get_client(serial="test-device")
        app = self.get_app(client)

        # Ensure snapshotting succeeds
        self.runner.responses["adb -s test-device shell settings list"] = CommandResult(0, "", "")

        # First command succeeds, second fails in batch
        # AdbClient.shell with input_data (the script)
        # The script is: cmd1 && echo SUCCESS_0 || exit $? \n cmd2 && echo SUCCESS_1 || exit $?
        self.runner.responses["adb -s test-device shell"] = CommandResult(1, "SUCCESS_0\nError on cmd2", "")

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
    def test_partial_restore_removes_successful_setting_keeps_failed_setting(self):
        state_data = {
            "version": 2,
            "device": {
                "serial": "test-device",
                "brand": "Google",
                "model": "Pixel 6",
                "android_release": "13",
                "sdk": "33",
                "fingerprint": "fingerprint-1",
            },
            "settings": {
                "global/setting_a": {"namespace": "global", "key": "setting_a", "value": "1"},
                "global/setting_b": {"namespace": "global", "key": "setting_b", "value": "2"},
            },
            "device_config": {},
            "packages": {},
        }
        app, _ = self.make_saved_state_app(state_data)
        self.set_response("adb -s test-device shell settings put global setting_a 1")
        self.set_response("adb -s test-device shell settings get global setting_a", stdout="1")
        self.runner.responses["adb -s test-device shell settings put global setting_b 2"] = CommandResult(1, "", "Failed")

        app.recorder.restore()

        self.assertIn("global/setting_b", app.store.data["settings"])
        self.assertNotIn("global/setting_a", app.store.data["settings"])
        state_file = self.test_dir / "devices" / "test-device" / "state.json"
        with state_file.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertEqual(list(data["settings"].keys()), ["global/setting_b"])

    def test_partial_restore_removes_successful_device_config_keeps_failed_device_config(self):
        state_data = {
            "version": 2,
            "device": {
                "serial": "test-device",
                "brand": "Google",
                "model": "Pixel 6",
                "android_release": "13",
                "sdk": "33",
                "fingerprint": "fingerprint-1",
            },
            "settings": {},
            "device_config": {
                "namespace_a/key_a": {"namespace": "namespace_a", "key": "key_a", "value": "1"},
                "namespace_b/key_b": {"namespace": "namespace_b", "key": "key_b", "value": "2"},
            },
            "packages": {},
        }
        app, _ = self.make_saved_state_app(state_data)
        self.set_response("adb -s test-device shell device_config put namespace_a key_a 1")
        self.set_response("adb -s test-device shell device_config get namespace_a key_a", stdout="1")
        self.runner.responses["adb -s test-device shell device_config put namespace_b key_b 2"] = CommandResult(1, "", "Failed")

        app.recorder.restore()

        self.assertIn("namespace_b/key_b", app.store.data["device_config"])
        self.assertNotIn("namespace_a/key_a", app.store.data["device_config"])
        state_file = self.test_dir / "devices" / "test-device" / "state.json"
        with state_file.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertEqual(list(data["device_config"].keys()), ["namespace_b/key_b"])

    def test_partial_restore_removes_successful_package_appop_keeps_failed_appop(self):
        state_data = {
            "version": 2,
            "device": {
                "serial": "test-device",
                "brand": "Google",
                "model": "Pixel 6",
                "android_release": "13",
                "sdk": "33",
                "fingerprint": "fingerprint-1",
            },
            "settings": {},
            "device_config": {},
            "packages": {
                "com.example.app": {
                    "enabled": None,
                    "appops": {
                        "RUN_ANY_IN_BACKGROUND": "allow",
                        "SYSTEM_ALERT_WINDOW": "ignore",
                    },
                    "standby_bucket": None,
                }
            },
        }
        app, _ = self.make_saved_state_app(state_data)
        self.set_response(
            "adb -s test-device shell cmd appops set com.example.app RUN_ANY_IN_BACKGROUND allow"
        )
        self.set_response(
            "adb -s test-device shell cmd appops get com.example.app RUN_ANY_IN_BACKGROUND",
            stdout="RUN_ANY_IN_BACKGROUND: allow",
        )
        self.runner.responses[
            "adb -s test-device shell cmd appops set com.example.app SYSTEM_ALERT_WINDOW ignore"
        ] = CommandResult(1, "", "Failed")

        app.recorder.restore()

        self.assertIn("com.example.app", app.store.data["packages"])
        self.assertEqual(
            app.store.data["packages"]["com.example.app"]["appops"],
            {"SYSTEM_ALERT_WINDOW": "ignore"},
        )
        state_file = self.test_dir / "devices" / "test-device" / "state.json"
        with state_file.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertEqual(data["packages"]["com.example.app"]["appops"], {"SYSTEM_ALERT_WINDOW": "ignore"})

    def test_partial_restore_cleans_empty_package_entries(self):
        state_data = {
            "version": 2,
            "device": {
                "serial": "test-device",
                "brand": "Google",
                "model": "Pixel 6",
                "android_release": "13",
                "sdk": "33",
                "fingerprint": "fingerprint-1",
            },
            "settings": {},
            "device_config": {},
            "packages": {
                "com.example.app": {
                    "enabled": None,
                    "appops": {"RUN_ANY_IN_BACKGROUND": "allow"},
                    "standby_bucket": None,
                }
            },
        }
        app, _ = self.make_saved_state_app(state_data)
        self.set_response(
            "adb -s test-device shell cmd appops set com.example.app RUN_ANY_IN_BACKGROUND allow"
        )
        self.set_response(
            "adb -s test-device shell cmd appops get com.example.app RUN_ANY_IN_BACKGROUND",
            stdout="RUN_ANY_IN_BACKGROUND: allow",
        )

        app.recorder.restore()

        self.assertFalse(app.store.has_entries())
        state_file = self.test_dir / "devices" / "test-device" / "state.json"
        self.assertFalse(state_file.exists())

    def test_successful_restore_still_clears_state_file(self):
        state_data = {
            "version": 2,
            "device": {
                "serial": "test-device",
                "brand": "Google",
                "model": "Pixel 6",
                "android_release": "13",
                "sdk": "33",
                "fingerprint": "fingerprint-1",
            },
            "settings": {
                "global/test_key": {"namespace": "global", "key": "test_key", "value": "old_val"}
            },
            "device_config": {},
            "packages": {},
        }
        app, _ = self.make_saved_state_app(state_data)
        self.set_response("adb -s test-device shell settings put global test_key old_val")
        self.set_response("adb -s test-device shell settings get global test_key", stdout="old_val")

        app.recorder.restore()

        self.assertFalse(app.store.has_entries())
        state_file = self.test_dir / "devices" / "test-device" / "state.json"
        self.assertFalse(state_file.exists())

    def test_restore_metadata_getprop_failure_falls_back_to_serial_check(self):
        state_data = {
            "version": 2,
            "device": {
                "serial": "test-device",
                "brand": "Google",
                "model": "Pixel 6",
                "android_release": "13",
                "sdk": "33",
                "fingerprint": "fingerprint-1",
            },
            "settings": {
                "global/test_key": {"namespace": "global", "key": "test_key", "value": "old_val"}
            },
            "device_config": {},
            "packages": {},
        }
        app, _ = self.make_saved_state_app(state_data)
        self.runner.responses["adb -s test-device shell getprop ro.product.brand"] = CommandResult(
            1,
            "",
            "property lookup failed",
        )
        self.set_response("adb -s test-device shell settings put global test_key old_val")
        self.set_response("adb -s test-device shell settings get global test_key", stdout="old_val")

        messages = app.recorder.restore()

        self.assertTrue(any("Restored setting global/test_key" in m for m in messages))
        self.assertFalse(app.store.has_entries())

    def test_restore_still_refuses_serial_mismatch_with_minimal_metadata(self):
        state_data = {
            "version": 2,
            "device": {
                "serial": "test-device",
                "brand": "Google",
                "model": "Pixel 6",
                "android_release": "13",
                "sdk": "33",
                "fingerprint": "fingerprint-1",
            },
            "settings": {
                "global/test_key": {"namespace": "global", "key": "test_key", "value": "old_val"}
            },
            "device_config": {},
            "packages": {},
        }
        app, _ = self.make_saved_state_app(state_data)
        self.runner.responses["adb -s test-device-2 shell getprop ro.product.brand"] = CommandResult(
            1,
            "",
            "property lookup failed",
        )
        app.client.serial = "test-device-2"

        with self.assertRaisesRegex(ValueError, "Device serial mismatch"):
            app.recorder.restore()

        self.assertIn("global/test_key", app.store.data["settings"])

    def test_restore_warns_when_fingerprint_cannot_be_verified(self):
        state_data = {
            "version": 2,
            "device": {
                "serial": "test-device",
                "brand": "Google",
                "model": "Pixel 6",
                "android_release": "13",
                "sdk": "33",
                "fingerprint": "fingerprint-1",
            },
            "settings": {
                "global/test_key": {"namespace": "global", "key": "test_key", "value": "old_val"}
            },
            "device_config": {},
            "packages": {},
        }
        output_messages = []
        client = self.get_client(serial="test-device")
        client.output = output_messages.append
        app = self.get_app(client)
        app.store.data = json.loads(json.dumps(state_data))
        app.store.save()

        self.runner.responses["adb -s test-device shell getprop ro.build.fingerprint"] = CommandResult(
            1,
            "",
            "property lookup failed",
        )
        self.set_response("adb -s test-device shell settings put global test_key old_val")
        self.set_response("adb -s test-device shell settings get global test_key", stdout="old_val")

        messages = app.recorder.restore()

        self.assertTrue(
            any("could not verify device fingerprint" in message.lower() for message in output_messages)
        )
        self.assertTrue(
            any("could not verify device fingerprint" in message.lower() for message in messages)
        )
        self.assertFalse(app.store.has_entries())

    def test_restore_refuses_when_full_fingerprint_mismatches(self):
        state_data = {
            "version": 2,
            "device": {
                "serial": "test-device",
                "brand": "Google",
                "model": "Pixel 6",
                "android_release": "13",
                "sdk": "33",
                "fingerprint": "fingerprint-1",
            },
            "settings": {
                "global/test_key": {"namespace": "global", "key": "test_key", "value": "old_val"}
            },
            "device_config": {},
            "packages": {},
        }
        app, _ = self.make_saved_state_app(state_data)
        self.runner.responses["adb -s test-device shell getprop ro.build.fingerprint"] = CommandResult(
            0,
            "fingerprint-2",
            "",
        )

        with self.assertRaisesRegex(ValueError, "Device fingerprint mismatch"):
            app.recorder.restore()

        self.assertIn("global/test_key", app.store.data["settings"])

    def test_failed_restore_still_keeps_state_file(self):
        state_data = {
            "version": 2,
            "device": {
                "serial": "test-device",
                "brand": "Google",
                "model": "Pixel 6",
                "android_release": "13",
                "sdk": "33",
                "fingerprint": "fingerprint-1",
            },
            "settings": {
                "global/test_key": {"namespace": "global", "key": "test_key", "value": "old_val"}
            },
            "device_config": {},
            "packages": {},
        }
        app, _ = self.make_saved_state_app(state_data)
        self.runner.responses["adb -s test-device shell settings put global test_key old_val"] = CommandResult(1, "", "Failed")

        app.recorder.restore()

        self.assertTrue(app.store.has_entries())
        self.assertEqual(list(app.store.data["settings"].keys()), ["global/test_key"])
        state_file = self.test_dir / "devices" / "test-device" / "state.json"
        self.assertTrue(state_file.exists())

    def test_restore_setting_success_but_verification_failure_keeps_entry(self):
        state_data = {
            "version": 2,
            "device": {
                "serial": "test-device",
                "brand": "Google",
                "model": "Pixel 6",
                "android_release": "13",
                "sdk": "33",
                "fingerprint": "fingerprint-1",
            },
            "settings": {
                "global/test_key": {"namespace": "global", "key": "test_key", "value": "old_val"}
            },
            "device_config": {},
            "packages": {},
        }
        app, _ = self.make_saved_state_app(state_data)
        self.set_response("adb -s test-device shell settings put global test_key old_val")
        self.set_response("adb -s test-device shell settings get global test_key", stdout="not_old_val")

        messages = app.recorder.restore()

        self.assertTrue(any("Failed to restore setting global/test_key" in m for m in messages))
        self.assertIn("global/test_key", app.store.data["settings"])
        state_file = self.test_dir / "devices" / "test-device" / "state.json"
        self.assertTrue(state_file.exists())

    def test_restore_device_config_success_but_verification_failure_keeps_entry(self):
        state_data = {
            "version": 2,
            "device": {
                "serial": "test-device",
                "brand": "Google",
                "model": "Pixel 6",
                "android_release": "13",
                "sdk": "33",
                "fingerprint": "fingerprint-1",
            },
            "settings": {},
            "device_config": {
                "namespace_a/key_a": {"namespace": "namespace_a", "key": "key_a", "value": "1"}
            },
            "packages": {},
        }
        app, _ = self.make_saved_state_app(state_data)
        self.set_response("adb -s test-device shell device_config put namespace_a key_a 1")
        self.set_response("adb -s test-device shell device_config get namespace_a key_a", stdout="2")

        messages = app.recorder.restore()

        self.assertTrue(any("Failed to restore device_config namespace_a/key_a" in m for m in messages))
        self.assertIn("namespace_a/key_a", app.store.data["device_config"])
        state_file = self.test_dir / "devices" / "test-device" / "state.json"
        self.assertTrue(state_file.exists())

    def test_restore_appop_success_but_verification_failure_keeps_entry(self):
        state_data = {
            "version": 2,
            "device": {
                "serial": "test-device",
                "brand": "Google",
                "model": "Pixel 6",
                "android_release": "13",
                "sdk": "33",
                "fingerprint": "fingerprint-1",
            },
            "settings": {},
            "device_config": {},
            "packages": {
                "com.example.app": {
                    "enabled": None,
                    "appops": {"RUN_ANY_IN_BACKGROUND": "allow"},
                    "standby_bucket": None,
                }
            },
        }
        app, _ = self.make_saved_state_app(state_data)
        self.set_response(
            "adb -s test-device shell cmd appops set com.example.app RUN_ANY_IN_BACKGROUND allow"
        )
        self.set_response(
            "adb -s test-device shell cmd appops get com.example.app RUN_ANY_IN_BACKGROUND",
            stdout="RUN_ANY_IN_BACKGROUND: ignore",
        )

        messages = app.recorder.restore()

        self.assertTrue(any("Failed to restore com.example.app appop RUN_ANY_IN_BACKGROUND" in m for m in messages))
        self.assertIn("com.example.app", app.store.data["packages"])
        self.assertEqual(
            app.store.data["packages"]["com.example.app"]["appops"],
            {"RUN_ANY_IN_BACKGROUND": "allow"},
        )
        state_file = self.test_dir / "devices" / "test-device" / "state.json"
        self.assertTrue(state_file.exists())

    def test_restore_standby_bucket_success_but_verification_failure_keeps_entry(self):
        state_data = {
            "version": 2,
            "device": {
                "serial": "test-device",
                "brand": "Google",
                "model": "Pixel 6",
                "android_release": "13",
                "sdk": "33",
                "fingerprint": "fingerprint-1",
            },
            "settings": {},
            "device_config": {},
            "packages": {
                "com.example.app": {
                    "enabled": None,
                    "appops": {},
                    "standby_bucket": "rare",
                }
            },
        }
        app, _ = self.make_saved_state_app(state_data)
        self.set_response("adb -s test-device shell am set-standby-bucket com.example.app rare")
        self.set_response("adb -s test-device shell am get-standby-bucket com.example.app", stdout="30")

        messages = app.recorder.restore()

        self.assertTrue(any("Failed to restore com.example.app standby bucket" in m for m in messages))
        self.assertIn("com.example.app", app.store.data["packages"])
        self.assertEqual(app.store.data["packages"]["com.example.app"]["standby_bucket"], "rare")
        state_file = self.test_dir / "devices" / "test-device" / "state.json"
        self.assertTrue(state_file.exists())

    def test_restore_package_enabled_success_but_verification_failure_keeps_entry(self):
        state_data = {
            "version": 2,
            "device": {
                "serial": "test-device",
                "brand": "Google",
                "model": "Pixel 6",
                "android_release": "13",
                "sdk": "33",
                "fingerprint": "fingerprint-1",
            },
            "settings": {},
            "device_config": {},
            "packages": {
                "com.example.app": {
                    "enabled": True,
                    "appops": {},
                    "standby_bucket": None,
                }
            },
        }
        app, _ = self.make_saved_state_app(state_data)
        self.set_response("adb -s test-device shell pm enable --user 0 com.example.app")
        self.set_response("adb -s test-device shell pm list packages -e com.example.app", stdout="")

        messages = app.recorder.restore()

        self.assertTrue(any("Failed to restore com.example.app enabled state" in m for m in messages))
        self.assertIn("com.example.app", app.store.data["packages"])
        self.assertTrue(app.store.data["packages"]["com.example.app"]["enabled"])
        state_file = self.test_dir / "devices" / "test-device" / "state.json"
        self.assertTrue(state_file.exists())

    def test_restore_successful_verified_entry_is_removed(self):
        state_data = {
            "version": 2,
            "device": {
                "serial": "test-device",
                "brand": "Google",
                "model": "Pixel 6",
                "android_release": "13",
                "sdk": "33",
                "fingerprint": "fingerprint-1",
            },
            "settings": {
                "global/test_key": {"namespace": "global", "key": "test_key", "value": "old_val"}
            },
            "device_config": {},
            "packages": {},
        }
        app, _ = self.make_saved_state_app(state_data)
        self.set_response("adb -s test-device shell settings put global test_key old_val")
        self.set_response("adb -s test-device shell settings get global test_key", stdout="old_val")

        messages = app.recorder.restore()

        self.assertTrue(any("Restored setting global/test_key" in m for m in messages))
        self.assertFalse(app.store.has_entries())
        state_file = self.test_dir / "devices" / "test-device" / "state.json"
        self.assertFalse(state_file.exists())

    def test_restore_mixed_verified_success_and_failure_keeps_only_failed_entries(self):
        state_data = {
            "version": 2,
            "device": {
                "serial": "test-device",
                "brand": "Google",
                "model": "Pixel 6",
                "android_release": "13",
                "sdk": "33",
                "fingerprint": "fingerprint-1",
            },
            "settings": {
                "global/setting_ok": {"namespace": "global", "key": "setting_ok", "value": "1"},
                "global/setting_bad": {"namespace": "global", "key": "setting_bad", "value": "2"},
            },
            "device_config": {},
            "packages": {},
        }
        app, _ = self.make_saved_state_app(state_data)
        self.set_response("adb -s test-device shell settings put global setting_ok 1")
        self.set_response("adb -s test-device shell settings get global setting_ok", stdout="1")
        self.set_response("adb -s test-device shell settings put global setting_bad 2")
        self.set_response("adb -s test-device shell settings get global setting_bad", stdout="not_2")

        messages = app.recorder.restore()

        self.assertTrue(any("Restored setting global/setting_ok" in m for m in messages))
        self.assertTrue(any("Failed to restore setting global/setting_bad" in m for m in messages))
        self.assertEqual(list(app.store.data["settings"].keys()), ["global/setting_bad"])
        state_file = self.test_dir / "devices" / "test-device" / "state.json"
        with state_file.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertEqual(list(data["settings"].keys()), ["global/setting_bad"])

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

    def test_mutating_adb_command_requires_serial(self):
        client = self.get_client()

        with self.assertRaisesRegex(CommandError, "Refusing to mutate device state without a selected ADB serial"):
            client.shell(["settings", "put", "global", "test", "1"], mutate=True)

    def test_dry_run_mutating_command_allows_missing_serial(self):
        client = self.get_client(dry_run=True)

        result = client.shell(["settings", "put", "global", "test", "1"], mutate=True)

        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.runner.commands, [])

    def test_non_mutating_adb_command_allows_missing_serial(self):
        client = self.get_client()

        result = client.shell(["getprop", "ro.product.model"], mutate=False)

        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.runner.commands[0]["args"], ["adb", "shell", "getprop", "ro.product.model"])

    # 7. Whitelist
    def test_whitelist_apps_not_mutated(self):
        client = self.get_client(serial="test-device")
        app = self.get_app(client)

        # Mock support
        self.runner.responses["adb -s test-device shell cmd appops help"] = CommandResult(0, "", "")
        self.runner.responses["adb -s test-device shell getprop ro.build.version.sdk"] = CommandResult(0, "30", "")
        self.runner.responses["adb -s test-device shell am get-standby-bucket android"] = CommandResult(0, "10\n", "")

        # Mock packages
        self.runner.responses["adb -s test-device shell pm list packages -3"] = CommandResult(0, "package:com.app.mutated\npackage:com.app.whitelisted", "")
        self.runner.responses["adb -s test-device shell pm list packages"] = CommandResult(0, "package:com.app.mutated\npackage:com.app.whitelisted", "")

        # Mock prefetch
        self.runner.responses["adb -s test-device shell dumpsys appops"] = CommandResult(0, "Package com.app.mutated:\n  RUN_ANY_IN_BACKGROUND: allow\nPackage com.app.whitelisted:\n  RUN_ANY_IN_BACKGROUND: allow", "")
        self.runner.responses["adb -s test-device shell dumpsys usagestats"] = CommandResult(0, "package=com.app.mutated bucket=active\npackage=com.app.whitelisted bucket=active", "")

        app.save_whitelist(["com.app.whitelisted"])

        # Mock verification responses for the mutated app
        self.runner.responses["adb -s test-device shell cmd appops get com.app.mutated RUN_ANY_IN_BACKGROUND"] = CommandResult(0, "mode: ignore", "")
        self.runner.responses["adb -s test-device shell am get-standby-bucket com.app.mutated"] = CommandResult(0, "40", "") # 40 is rare

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
