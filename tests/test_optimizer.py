import tempfile
import unittest
from pathlib import Path
from unittest.mock import ANY, MagicMock, patch

from optimizer import (
    AdbClient,
    BatteryOptimizerApp,
    BatteryOptimizerCLI,
    CommandError,
    CommandResult,
    StateRecorder,
    StateStore,
    parse_adb_devices,
    resolve_package_choice,
    SubprocessRunner,
)


class OptimizerTests(unittest.TestCase):
    def make_app_and_cli(self, state_dir, user_inputs=None):
        outputs = []
        input_values = list(user_inputs or [])

        def fake_input(prompt):
            if not input_values:
                raise AssertionError(f"Unexpected prompt: {prompt}")
            return input_values.pop(0)

        runner = SubprocessRunner()
        client = AdbClient(runner=runner, output=outputs.append)
        app = BatteryOptimizerApp(client=client, state_dir=state_dir)
        cli = BatteryOptimizerCLI(
            app=app,
            output=outputs.append,
            input_fn=fake_input,
        )
        return app, cli, outputs

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

    @patch("optimizer.shutil.which")
    @patch("optimizer.subprocess.run")
    def test_missing_adb_is_reported(self, mock_run, mock_which):
        mock_which.return_value = None
        with tempfile.TemporaryDirectory() as tmp:
            _, cli, outputs = self.make_app_and_cli(Path(tmp))
            self.assertFalse(cli.check_environment())
            self.assertIn("ADB was not found in PATH.", outputs[0])

    @patch("optimizer.shutil.which")
    @patch("optimizer.subprocess.run")
    def test_no_devices_is_reported(self, mock_run, mock_which):
        mock_which.return_value = "/usr/bin/adb"
        mock_run.return_value = MagicMock(returncode=0, stdout="List of devices attached\n\n", stderr="")
        with tempfile.TemporaryDirectory() as tmp:
            _, cli, outputs = self.make_app_and_cli(Path(tmp))
            self.assertFalse(cli.check_environment())
            self.assertIn("No ADB devices detected.", outputs[0])

    @patch("optimizer.shutil.which")
    @patch("optimizer.subprocess.run")
    def test_multiple_devices_requires_selection(self, mock_run, mock_which):
        mock_which.return_value = "/usr/bin/adb"
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="List of devices attached\nserial-1\tdevice\nserial-2\tdevice\n",
            stderr=""
        )
        with tempfile.TemporaryDirectory() as tmp:
            _, cli, _ = self.make_app_and_cli(Path(tmp), user_inputs=["2"])
            self.assertTrue(cli.check_environment())
            self.assertEqual(cli.client.serial, "serial-2")

    @patch("optimizer.shutil.which")
    @patch("optimizer.subprocess.run")
    def test_unauthorized_device_is_blocked(self, mock_run, mock_which):
        mock_which.return_value = "/usr/bin/adb"
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="List of devices attached\nserial-1\tunauthorized\n",
            stderr=""
        )
        with tempfile.TemporaryDirectory() as tmp:
            _, cli, outputs = self.make_app_and_cli(Path(tmp))
            self.assertFalse(cli.check_environment())
            self.assertIn("No authorized online device is available.", outputs[-1])

    @patch("optimizer.shutil.which")
    @patch("optimizer.subprocess.run")
    def test_experimental_confirmation_blocks_mutation(self, mock_run, mock_which):
        mock_which.return_value = "/usr/bin/adb"
        def side_effect(args, **kwargs):
            cmd = " ".join(args)
            if "devices" in cmd:
                return MagicMock(returncode=0, stdout="List of devices attached\nserial-1\tdevice\n", stderr="")
            if "getprop ro.product.brand" in cmd:
                return MagicMock(returncode=0, stdout="google\n", stderr="")
            if "getprop ro.product.model" in cmd:
                return MagicMock(returncode=0, stdout="Pixel\n", stderr="")
            if "getprop ro.build.version.release" in cmd:
                return MagicMock(returncode=0, stdout="14\n", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")
        mock_run.side_effect = side_effect

        with tempfile.TemporaryDirectory() as tmp:
            app, cli, outputs = self.make_app_and_cli(Path(tmp), user_inputs=["3", "n", "9"])
            cli.client.serial = "serial-1"
            
            with patch.object(app, 'apply_experimental_optimizations') as mock_apply:
                cli.run()
                mock_apply.assert_not_called()
                
            self.assertIn("Skipped experimental optimizations.", outputs)

    @patch("optimizer.subprocess.run")
    def test_snapshot_restore_for_unset_setting_uses_delete(self, mock_run):
        def side_effect(args, **kwargs):
            cmd = " ".join(args)
            if "settings list global" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")
        mock_run.side_effect = side_effect

        with tempfile.TemporaryDirectory() as tmp:
            app, _, _ = self.make_app_and_cli(Path(tmp))
            app.client.serial = "serial-1"

            with app.recorder.transaction():
                app.recorder.put_setting("global", "wifi_scan_throttle_enabled", "1")

            messages = app.revert_saved_state()
            self.assertTrue(any("Restored setting global/wifi_scan_throttle_enabled" in m for m in messages))

            mock_run.assert_any_call(
                ["adb", "-s", "serial-1", "shell", "settings", "delete", "global", "wifi_scan_throttle_enabled"],
                capture_output=True,
                text=True,
                input=None
            )

    @patch("optimizer.subprocess.run")
    def test_package_state_restore_covers_appops_bucket_and_enabled_state(self, mock_run):
        def side_effect(args, **kwargs):
            cmd = " ".join(args)
            if "dumpsys appops" in cmd:
                return MagicMock(returncode=0, stdout="  Package com.example.app:\n    RUN_ANY_IN_BACKGROUND: allow\n", stderr="")
            if "dumpsys usagestats" in cmd:
                return MagicMock(returncode=0, stdout="package=com.example.app u=0 bucket=active reason=...\n", stderr="")
            if "list packages -d" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            if "list packages -e" in cmd:
                return MagicMock(returncode=0, stdout="package:com.example.app\n", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")
        mock_run.side_effect = side_effect

        with tempfile.TemporaryDirectory() as tmp:
            app, _, _ = self.make_app_and_cli(Path(tmp))
            app.client.serial = "serial-1"

            with app.recorder.transaction():
                app.recorder.prefetch_package_states()
                app.recorder.set_appop("com.example.app", "RUN_ANY_IN_BACKGROUND", "ignore")
                app.recorder.set_standby_bucket("com.example.app", "rare")
                app.recorder.set_package_enabled("com.example.app", enabled=False)

            app.revert_saved_state()

            mock_run.assert_any_call(
                ["adb", "-s", "serial-1", "shell", "cmd", "appops", "set", "com.example.app", "RUN_ANY_IN_BACKGROUND", "allow"],
                capture_output=True, text=True, input=None
            )
            mock_run.assert_any_call(
                ["adb", "-s", "serial-1", "shell", "am", "set-standby-bucket", "com.example.app", "active"],
                capture_output=True, text=True, input=None
            )
            mock_run.assert_any_call(
                ["adb", "-s", "serial-1", "shell", "pm", "enable", "--user", "0", "com.example.app"],
                capture_output=True, text=True, input=None
            )

    @patch("optimizer.subprocess.run")
    def test_restore_reports_failures(self, mock_run):
        def side_effect(args, **kwargs):
            cmd = " ".join(args)
            if "settings list global" in cmd:
                return MagicMock(returncode=0, stdout="window_animation_scale=1.0\n", stderr="")
            if "settings put global window_animation_scale 1.0" in cmd:
                return MagicMock(returncode=1, stdout="", stderr="permission denied")
            return MagicMock(returncode=0, stdout="", stderr="")
        mock_run.side_effect = side_effect

        with tempfile.TemporaryDirectory() as tmp:
            app, _, outputs = self.make_app_and_cli(Path(tmp))
            app.client.serial = "serial-1"

            with app.recorder.transaction():
                app.recorder.put_setting("global", "window_animation_scale", "0.5")

            messages = app.revert_saved_state()
            self.assertTrue(any("Failed to restore setting global/window_animation_scale" in m for m in messages))
            self.assertTrue((Path(tmp) / "state.json").exists())
            self.assertTrue(any("Partial state corruption" in out for out in outputs))

    @patch("optimizer.subprocess.run")
    def test_validate_package_blocks_unknown_package(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="package:com.example.safe\n", stderr="")
        with tempfile.TemporaryDirectory() as tmp:
            app, _, _ = self.make_app_and_cli(Path(tmp))
            app.client.serial = "serial-1"
            with self.assertRaises(ValueError):
                app.validate_package("com.bad.actor;rm -rf /")

    @patch("optimizer.subprocess.run")
    def test_partial_rollback_on_batch_failure(self, mock_run):
        def side_effect(args, **kwargs):
            input_data = kwargs.get('input')
            if input_data and "SUCCESS_0" in input_data:
                # First command succeeds, second fails
                return MagicMock(returncode=1, stdout="SUCCESS_0\n", stderr="simulated failure")
            if "settings list global" in " ".join(args):
                return MagicMock(returncode=0, stdout="some_setting=old_value\nother_setting=old_value\n", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")
        mock_run.side_effect = side_effect

        with tempfile.TemporaryDirectory() as tmp:
            app, _, _ = self.make_app_and_cli(Path(tmp))
            app.client.serial = "serial-1"

            try:
                with app.recorder.transaction():
                    app.recorder.put_setting("global", "some_setting", "new_value")
                    app.recorder.put_setting("global", "other_setting", "new_value")
            except CommandError:
                pass

            # Only some_setting (index 0) should be reverted because SUCCESS_0 was in stdout
            # other_setting (index 1) should NOT be reverted
            mock_run.assert_any_call(
                ["adb", "-s", "serial-1", "shell", "settings", "put", "global", "some_setting", "old_value"],
                capture_output=True, text=True, input=None
            )
            
            # Verify other_setting was NOT reverted
            for call in mock_run.call_args_list:
                args = call[0][0]
                cmd_str = " ".join(args)
                if "other_setting" in cmd_str and "put" in cmd_str:
                    self.assertFalse("old_value" in cmd_str)

    @patch("optimizer.subprocess.run")
    def test_no_rollback_if_not_dispatched(self, mock_run):
        with tempfile.TemporaryDirectory() as tmp:
            app, _, _ = self.make_app_and_cli(Path(tmp))
            app.client.serial = "serial-1"

            try:
                with app.recorder.transaction():
                    app.recorder.put_setting("global", "some_setting", "value")
                    raise RuntimeError("Pre-dispatch error")
            except RuntimeError as e:
                if str(e) != "Pre-dispatch error":
                    raise

            # Revert should NOT be called because batch_dispatched was False
            for call in mock_run.call_args_list:
                args = call[0][0]
                cmd_str = " ".join(args)
                self.assertFalse("settings put" in cmd_str and "old_value" in cmd_str)


if __name__ == "__main__":
    unittest.main()
