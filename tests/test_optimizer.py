import tempfile
import unittest
from pathlib import Path

from optimizer import (
    AdbClient,
    BatteryOptimizerApp,
    CommandError,
    CommandResult,
    StateRecorder,
    StateStore,
    parse_adb_devices,
    resolve_package_choice,
)


class FakeRunner:
    def __init__(self, responses=None, which_map=None):
        self.responses = responses or {}
        self.which_map = {"adb": "/usr/bin/adb"} if which_map is None else which_map
        self.calls = []

    def run(self, args):
        key = tuple(args)
        self.calls.append(list(args))
        response = self.responses.get(key)
        if callable(response):
            return response(args)
        if response is None:
            return CommandResult(0, "", "")
        return response

    def which(self, name):
        return self.which_map.get(name)


class OptimizerTests(unittest.TestCase):
    def make_app(self, runner, state_dir, responses=None, user_inputs=None):
        outputs = []
        input_values = list(user_inputs or [])

        def fake_input(prompt):
            if not input_values:
                raise AssertionError(f"Unexpected prompt: {prompt}")
            return input_values.pop(0)

        client = AdbClient(runner=runner, output=outputs.append)
        app = BatteryOptimizerApp(
            client=client,
            state_dir=state_dir,
            output=outputs.append,
            input_fn=fake_input,
        )
        return app, outputs

    def test_parse_adb_devices(self):
        devices = parse_adb_devices(
            "List of devices attached\nserial-1\tdevice\nserial-2\tunauthorized\n"
        )
        self.assertEqual(
            devices,
            [
                {"serial": "serial-1", "status": "device"},
                {"serial": "serial-2", "status": "unauthorized"},
            ],
        )

    def test_resolve_package_choice_partial_match(self):
        packages = ["com.example.chat", "com.example.music", "org.sample"]
        self.assertEqual(
            resolve_package_choice("music", packages),
            ["com.example.music"],
        )

    def test_missing_adb_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = FakeRunner(which_map={})
            app, outputs = self.make_app(runner, Path(tmp))
            self.assertFalse(app.check_environment())
            self.assertIn("ADB was not found in PATH.", outputs[0])

    def test_no_devices_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = FakeRunner(
                responses={
                    ("adb", "devices"): CommandResult(0, "List of devices attached\n\n", "")
                }
            )
            app, outputs = self.make_app(runner, Path(tmp))
            self.assertFalse(app.check_environment())
            self.assertIn("No ADB devices detected.", outputs[0])

    def test_multiple_devices_requires_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = FakeRunner(
                responses={
                    (
                        "adb",
                        "devices",
                    ): CommandResult(
                        0,
                        "List of devices attached\nserial-1\tdevice\nserial-2\tdevice\n",
                        "",
                    )
                }
            )
            app, _ = self.make_app(runner, Path(tmp), user_inputs=["2"])
            self.assertTrue(app.check_environment())
            self.assertEqual(app.client.serial, "serial-2")

    def test_unauthorized_device_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = FakeRunner(
                responses={
                    (
                        "adb",
                        "devices",
                    ): CommandResult(
                        0,
                        "List of devices attached\nserial-1\tunauthorized\n",
                        "",
                    )
                }
            )
            app, outputs = self.make_app(runner, Path(tmp))
            self.assertFalse(app.check_environment())
            self.assertIn("No authorized online device is available.", outputs[-1])

    def test_experimental_confirmation_blocks_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = FakeRunner(
                responses={
                    ("adb", "devices"): CommandResult(
                        0,
                        "List of devices attached\nserial-1\tdevice\n",
                        "",
                    ),
                    ("adb", "-s", "serial-1", "shell", "getprop", "ro.product.brand"): CommandResult(
                        0, "google\n", ""
                    ),
                    ("adb", "-s", "serial-1", "shell", "getprop", "ro.product.model"): CommandResult(
                        0, "Pixel\n", ""
                    ),
                    (
                        "adb",
                        "-s",
                        "serial-1",
                        "shell",
                        "getprop",
                        "ro.build.version.release",
                    ): CommandResult(0, "14\n", ""),
                }
            )
            app, outputs = self.make_app(runner, Path(tmp), user_inputs=["n"])
            app.client.serial = "serial-1"
            app.apply_experimental_optimizations()
            self.assertIn("Skipped experimental optimizations.", outputs[-1])
            self.assertEqual(
                [call for call in runner.calls if "device_config" in call or "settings" in call],
                [],
            )

    def test_snapshot_restore_for_unset_setting_uses_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = FakeRunner(
                responses={
                    (
                        "adb",
                        "-s",
                        "serial-1",
                        "shell",
                        "settings",
                        "get",
                        "global",
                        "wifi_scan_throttle_enabled",
                    ): CommandResult(0, "null\n", ""),
                }
            )
            client = AdbClient(runner=runner, serial="serial-1")
            store = StateStore(Path(tmp))
            recorder = StateRecorder(client, store)
            recorder.put_setting("global", "wifi_scan_throttle_enabled", "1")
            messages = recorder.restore()
            self.assertTrue(any("Restored setting global/wifi_scan_throttle_enabled" in m for m in messages))
            self.assertIn(
                [
                    "adb",
                    "-s",
                    "serial-1",
                    "shell",
                    "settings",
                    "delete",
                    "global",
                    "wifi_scan_throttle_enabled",
                ],
                runner.calls,
            )

    def test_package_state_restore_covers_appops_bucket_and_enabled_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = FakeRunner(
                responses={
                    (
                        "adb",
                        "-s",
                        "serial-1",
                        "shell",
                        "cmd",
                        "appops",
                        "get",
                        "com.example.app",
                        "RUN_ANY_IN_BACKGROUND",
                    ): CommandResult(0, "RUN_ANY_IN_BACKGROUND: allow\n", ""),
                    (
                        "adb",
                        "-s",
                        "serial-1",
                        "shell",
                        "am",
                        "get-standby-bucket",
                        "com.example.app",
                    ): CommandResult(0, "com.example.app: active\n", ""),
                    (
                        "adb",
                        "-s",
                        "serial-1",
                        "shell",
                        "pm",
                        "list",
                        "packages",
                        "-d",
                        "com.example.app",
                    ): CommandResult(0, "", ""),
                    (
                        "adb",
                        "-s",
                        "serial-1",
                        "shell",
                        "pm",
                        "list",
                        "packages",
                        "-e",
                        "com.example.app",
                    ): CommandResult(0, "package:com.example.app\n", ""),
                }
            )
            client = AdbClient(runner=runner, serial="serial-1")
            store = StateStore(Path(tmp))
            recorder = StateRecorder(client, store)
            recorder.set_appop("com.example.app", "RUN_ANY_IN_BACKGROUND", "ignore")
            recorder.set_standby_bucket("com.example.app", "rare")
            recorder.set_package_enabled("com.example.app", enabled=False)
            recorder.restore()
            self.assertIn(
                [
                    "adb",
                    "-s",
                    "serial-1",
                    "shell",
                    "cmd",
                    "appops",
                    "set",
                    "com.example.app",
                    "RUN_ANY_IN_BACKGROUND",
                    "allow",
                ],
                runner.calls,
            )
            self.assertIn(
                [
                    "adb",
                    "-s",
                    "serial-1",
                    "shell",
                    "am",
                    "set-standby-bucket",
                    "com.example.app",
                    "active",
                ],
                runner.calls,
            )
            self.assertIn(
                [
                    "adb",
                    "-s",
                    "serial-1",
                    "shell",
                    "pm",
                    "enable",
                    "--user",
                    "0",
                    "com.example.app",
                ],
                runner.calls,
            )

    def test_restore_keeps_snapshot_when_a_step_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            runner = FakeRunner(
                responses={
                    (
                        "adb",
                        "-s",
                        "serial-1",
                        "shell",
                        "settings",
                        "get",
                        "global",
                        "window_animation_scale",
                    ): CommandResult(0, "1.0\n", ""),
                    (
                        "adb",
                        "-s",
                        "serial-1",
                        "shell",
                        "settings",
                        "put",
                        "global",
                        "window_animation_scale",
                        "0.5",
                    ): CommandResult(0, "", ""),
                    (
                        "adb",
                        "-s",
                        "serial-1",
                        "shell",
                        "settings",
                        "put",
                        "global",
                        "window_animation_scale",
                        "1.0",
                    ): CommandResult(1, "", "permission denied"),
                }
            )
            client = AdbClient(runner=runner, serial="serial-1")
            store = StateStore(state_dir)
            recorder = StateRecorder(client, store)
            recorder.put_setting("global", "window_animation_scale", "0.5")
            messages = recorder.restore()
            self.assertTrue(any("Failed to restore setting global/window_animation_scale" in m for m in messages))
            self.assertTrue((state_dir / "state.json").exists())

    def test_validate_package_blocks_unknown_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = FakeRunner(
                responses={
                    (
                        "adb",
                        "-s",
                        "serial-1",
                        "shell",
                        "pm",
                        "list",
                        "packages",
                    ): CommandResult(0, "package:com.example.safe\n", "")
                }
            )
            app, _ = self.make_app(runner, Path(tmp))
            app.client.serial = "serial-1"
            with self.assertRaises(ValueError):
                app.validate_package("com.bad.actor;rm -rf /")


if __name__ == "__main__":
    unittest.main()
