import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Set


APP_NAME = "android-battery-optimizer"
DEFAULT_STATE_DIR = (
    Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / APP_NAME
)
WHITELIST_FILE = "whitelist.txt"
SNAPSHOT_FILE = "state.json"


class CommandError(RuntimeError):
    pass


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class CommandRunner:
    def run(self, args: Sequence[str]) -> CommandResult:
        raise NotImplementedError

    def which(self, name: str) -> Optional[str]:
        raise NotImplementedError


class SubprocessRunner(CommandRunner):
    def run(self, args: Sequence[str]) -> CommandResult:
        completed = subprocess.run(args, capture_output=True, text=True)
        return CommandResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )

    def which(self, name: str) -> Optional[str]:
        return shutil.which(name)


class AdbClient:
    def __init__(
        self,
        runner: CommandRunner,
        serial: Optional[str] = None,
        dry_run: bool = False,
        output: Callable[[str], None] = print,
    ) -> None:
        self.runner = runner
        self.serial = serial
        self.dry_run = dry_run
        self.output = output

    def adb_exists(self) -> bool:
        return self.runner.which("adb") is not None

    def _base_command(self) -> List[str]:
        command = ["adb"]
        if self.serial:
            command.extend(["-s", self.serial])
        return command

    def _stringify(self, args: Sequence[object]) -> List[str]:
        return [str(arg) for arg in args]

    def _format(self, args: Sequence[str]) -> str:
        return " ".join(shlex.quote(arg) for arg in args)

    def run_adb(
        self, args: Sequence[object], *, mutate: bool = False, check: bool = True
    ) -> CommandResult:
        command = self._base_command() + self._stringify(args)
        if mutate and self.dry_run:
            self.output(f"[dry-run] {self._format(command)}")
            return CommandResult(returncode=0, stdout="", stderr="")

        result = self.runner.run(command)
        if check and result.returncode != 0:
            stderr = result.stderr.strip()
            stdout = result.stdout.strip()
            details = stderr or stdout or "unknown error"
            raise CommandError(f"{self._format(command)} failed: {details}")
        return result

    def shell(
        self, args: Sequence[object], *, mutate: bool = False, check: bool = True
    ) -> CommandResult:
        return self.run_adb(["shell", *args], mutate=mutate, check=check)

    def shell_text(
        self, args: Sequence[object], *, mutate: bool = False, check: bool = True
    ) -> str:
        return self.shell(args, mutate=mutate, check=check).stdout.strip()

    def local_text(self, args: Sequence[object], *, check: bool = True) -> str:
        result = self.runner.run(self._stringify(args))
        if check and result.returncode != 0:
            details = result.stderr.strip() or result.stdout.strip() or "unknown error"
            raise CommandError(f"{self._format(self._stringify(args))} failed: {details}")
        return result.stdout.strip()


class StateStore:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.path = state_dir / SNAPSHOT_FILE
        self.data = self._load()

    def _load(self) -> Dict[str, object]:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            return {
                "version": 1,
                "settings": {},
                "device_config": {},
                "packages": {},
            }
        with self.path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def save(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as handle:
            json.dump(self.data, handle, indent=2, sort_keys=True)

    def clear(self) -> None:
        self.data = {
            "version": 1,
            "settings": {},
            "device_config": {},
            "packages": {},
        }
        if self.path.exists():
            self.path.unlink()

    def has_entries(self) -> bool:
        return any(self.data[key] for key in ("settings", "device_config", "packages"))


class StateRecorder:
    def __init__(self, client: AdbClient, store: StateStore) -> None:
        self.client = client
        self.store = store

    @staticmethod
    def _normalize_value(value: str) -> Optional[str]:
        stripped = value.strip()
        if stripped in {"", "null", "None", "undefined"}:
            return None
        return stripped

    def snapshot_setting(self, namespace: str, key: str) -> None:
        store = self.store.data["settings"]
        snapshot_key = f"{namespace}/{key}"
        if snapshot_key in store:
            return
        value = self._normalize_value(
            self.client.shell_text(["settings", "get", namespace, key], check=False)
        )
        store[snapshot_key] = {
            "namespace": namespace,
            "key": key,
            "value": value,
        }
        self.store.save()

    def put_setting(self, namespace: str, key: str, value: object) -> None:
        self.snapshot_setting(namespace, key)
        self.client.shell(["settings", "put", namespace, key, value], mutate=True)

    def delete_setting(self, namespace: str, key: str) -> None:
        self.snapshot_setting(namespace, key)
        self.client.shell(["settings", "delete", namespace, key], mutate=True)

    def snapshot_device_config(self, namespace: str, key: str) -> None:
        store = self.store.data["device_config"]
        snapshot_key = f"{namespace}/{key}"
        if snapshot_key in store:
            return
        value = self._normalize_value(
            self.client.shell_text(["device_config", "get", namespace, key], check=False)
        )
        store[snapshot_key] = {
            "namespace": namespace,
            "key": key,
            "value": value,
        }
        self.store.save()

    def put_device_config(self, namespace: str, key: str, value: object) -> None:
        self.snapshot_device_config(namespace, key)
        self.client.shell(
            ["device_config", "put", namespace, key, value],
            mutate=True,
        )

    def delete_device_config(self, namespace: str, key: str) -> None:
        self.snapshot_device_config(namespace, key)
        self.client.shell(
            ["device_config", "delete", namespace, key],
            mutate=True,
        )

    def _package_entry(self, package: str) -> Dict[str, object]:
        packages = self.store.data["packages"]
        entry = packages.setdefault(
            package,
            {
                "enabled": None,
                "appops": {},
                "standby_bucket": None,
            },
        )
        return entry

    def snapshot_package_enabled(self, package: str) -> None:
        entry = self._package_entry(package)
        if entry["enabled"] is not None:
            return
        disabled = self.client.shell_text(
            ["pm", "list", "packages", "-d", package],
            check=False,
        )
        enabled = self.client.shell_text(
            ["pm", "list", "packages", "-e", package],
            check=False,
        )
        entry["enabled"] = package in enabled.splitlines() or f"package:{package}" in enabled
        if package in disabled.splitlines() or f"package:{package}" in disabled:
            entry["enabled"] = False
        self.store.save()

    def snapshot_appop(self, package: str, op: str) -> None:
        entry = self._package_entry(package)
        appops = entry["appops"]
        if op in appops:
            return
        output = self.client.shell_text(["cmd", "appops", "get", package, op], check=False)
        value = "default"
        for line in output.splitlines():
            if f"{op}:" in line:
                value = line.split(":", 1)[1].strip().split()[0]
                break
        appops[op] = value
        self.store.save()

    def snapshot_standby_bucket(self, package: str) -> None:
        entry = self._package_entry(package)
        if entry["standby_bucket"] is not None:
            return
        output = self.client.shell_text(
            ["am", "get-standby-bucket", package],
            check=False,
        )
        bucket = output.split(":")[-1].strip() if output else "active"
        entry["standby_bucket"] = bucket or "active"
        self.store.save()

    def set_package_enabled(self, package: str, enabled: bool) -> None:
        self.snapshot_package_enabled(package)
        command = ["pm", "enable", "--user", "0", package]
        if not enabled:
            command = ["pm", "disable-user", "--user", "0", package]
        self.client.shell(command, mutate=True)

    def set_appop(self, package: str, op: str, value: str) -> None:
        self.snapshot_appop(package, op)
        self.client.shell(["cmd", "appops", "set", package, op, value], mutate=True)

    def set_standby_bucket(self, package: str, bucket: str) -> None:
        self.snapshot_standby_bucket(package)
        self.client.shell(["am", "set-standby-bucket", package, bucket], mutate=True)

    def restore(self) -> List[str]:
        messages: List[str] = []
        had_failures = False
        for item in self.store.data["settings"].values():
            namespace = item["namespace"]
            key = item["key"]
            value = item["value"]
            try:
                if value is None:
                    self.client.shell(["settings", "delete", namespace, key], mutate=True)
                else:
                    self.client.shell(
                        ["settings", "put", namespace, key, value],
                        mutate=True,
                    )
                messages.append(f"Restored setting {namespace}/{key}")
            except CommandError as exc:
                had_failures = True
                messages.append(f"Failed to restore setting {namespace}/{key}: {exc}")

        for item in self.store.data["device_config"].values():
            namespace = item["namespace"]
            key = item["key"]
            value = item["value"]
            try:
                if value is None:
                    self.client.shell(
                        ["device_config", "delete", namespace, key],
                        mutate=True,
                    )
                else:
                    self.client.shell(
                        ["device_config", "put", namespace, key, value],
                        mutate=True,
                    )
                messages.append(f"Restored device_config {namespace}/{key}")
            except CommandError as exc:
                had_failures = True
                messages.append(f"Failed to restore device_config {namespace}/{key}: {exc}")

        for package, item in self.store.data["packages"].items():
            for op, value in item["appops"].items():
                try:
                    if value == "default":
                        self.client.shell(
                            ["cmd", "appops", "reset", package, op],
                            mutate=True,
                        )
                    else:
                        self.client.shell(
                            ["cmd", "appops", "set", package, op, value],
                            mutate=True,
                        )
                    messages.append(f"Restored {package} appop {op}")
                except CommandError as exc:
                    had_failures = True
                    messages.append(f"Failed to restore {package} appop {op}: {exc}")

            bucket = item.get("standby_bucket")
            if bucket:
                try:
                    self.client.shell(
                        ["am", "set-standby-bucket", package, bucket],
                        mutate=True,
                    )
                    messages.append(f"Restored {package} standby bucket")
                except CommandError as exc:
                    had_failures = True
                    messages.append(f"Failed to restore {package} standby bucket: {exc}")

            enabled = item.get("enabled")
            if enabled is not None:
                try:
                    command = ["pm", "enable", "--user", "0", package]
                    if not enabled:
                        command = ["pm", "disable-user", "--user", "0", package]
                    self.client.shell(command, mutate=True)
                    messages.append(f"Restored {package} enabled state")
                except CommandError as exc:
                    had_failures = True
                    messages.append(f"Failed to restore {package} enabled state: {exc}")

        if had_failures:
            self.store.save()
        else:
            self.store.clear()
        return messages


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Android battery optimizer")
    parser.add_argument("--serial", help="ADB device serial to use")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print mutating adb commands instead of executing them",
    )
    parser.add_argument(
        "--state-dir",
        default=str(DEFAULT_STATE_DIR),
        help="Directory for whitelist and saved rollback state",
    )
    return parser.parse_args(argv)


def parse_adb_devices(output: str) -> List[Dict[str, str]]:
    devices: List[Dict[str, str]] = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("List of devices attached"):
            continue
        parts = stripped.split()
        if len(parts) >= 2:
            devices.append({"serial": parts[0], "status": parts[1]})
    return devices


def resolve_package_choice(query: str, packages: Sequence[str]) -> List[str]:
    normalized = query.strip()
    if not normalized:
        return []
    if "." in normalized:
        return [pkg for pkg in packages if pkg == normalized]
    lowered = normalized.lower()
    return [pkg for pkg in packages if lowered in pkg.lower()]


class BatteryOptimizerApp:
    def __init__(
        self,
        client: AdbClient,
        state_dir: Path,
        runner: Optional[CommandRunner] = None,
        output: Callable[[str], None] = print,
        input_fn: Callable[[str], str] = input,
    ) -> None:
        self.client = client
        self.runner = runner or client.runner
        self.state_dir = state_dir
        self.output = output
        self.input = input_fn
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.whitelist_path = self.state_dir / WHITELIST_FILE
        self.store = StateStore(self.state_dir)
        self.recorder = StateRecorder(client, self.store)

    def load_whitelist(self) -> List[str]:
        if not self.whitelist_path.exists():
            return []
        with self.whitelist_path.open("r", encoding="utf-8") as handle:
            return [line.strip() for line in handle if line.strip()]

    def save_whitelist(self, packages: Sequence[str]) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with self.whitelist_path.open("w", encoding="utf-8") as handle:
            for package in packages:
                handle.write(f"{package}\n")

    def check_environment(self) -> bool:
        if not self.client.adb_exists():
            self.output("ADB was not found in PATH. Install Android Platform Tools first.")
            return False

        devices = parse_adb_devices(self.client.local_text(["adb", "devices"], check=False))
        if not devices:
            self.output("No ADB devices detected. Connect a device and authorize USB debugging.")
            return False

        if self.client.serial:
            matching = [device for device in devices if device["serial"] == self.client.serial]
            if not matching:
                self.output(f"Device {self.client.serial} was not found in `adb devices` output.")
                return False
            if matching[0]["status"] != "device":
                self.output(
                    f"Device {self.client.serial} is {matching[0]['status']}. Resolve that before continuing."
                )
                return False
            return True

        ready = [device for device in devices if device["status"] == "device"]
        blocked = [device for device in devices if device["status"] != "device"]
        for device in blocked:
            self.output(
                f"Skipping device {device['serial']} because it is {device['status']}."
            )

        if not ready:
            self.output("No authorized online device is available.")
            return False

        if len(ready) == 1:
            self.client.serial = ready[0]["serial"]
            return True

        self.output("Multiple devices detected:")
        for index, device in enumerate(ready, start=1):
            self.output(f"  {index}. {device['serial']}")
        choice = self.input("Select device number: ").strip()
        if not choice.isdigit():
            self.output("Invalid device selection.")
            return False
        selected = int(choice)
        if selected < 1 or selected > len(ready):
            self.output("Invalid device selection.")
            return False
        self.client.serial = ready[selected - 1]["serial"]
        return True

    def require_connected_device(self) -> bool:
        if not self.client.serial:
            return self.check_environment()
        return True

    def get_device_info(self) -> str:
        brand = self.client.shell_text(["getprop", "ro.product.brand"], check=False)
        model = self.client.shell_text(["getprop", "ro.product.model"], check=False)
        version = self.client.shell_text(["getprop", "ro.build.version.release"], check=False)
        return f"{brand} {model} (Android {version})".strip()

    def get_packages(self, third_party: bool = True) -> List[str]:
        args: List[object] = ["pm", "list", "packages"]
        if third_party:
            args.append("-3")
        output = self.client.shell_text(args, check=False)
        packages = []
        for line in output.splitlines():
            if ":" in line:
                packages.append(line.split(":", 1)[1].strip())
        return sorted(packages)

    def get_installed_packages_set(self) -> Set[str]:
        return set(self.get_packages(third_party=False))

    def validate_package(self, package: str) -> None:
        if package not in self.get_installed_packages_set():
            raise ValueError(f"Package `{package}` is not installed on the connected device.")

    def confirm(self, prompt: str) -> bool:
        answer = self.input(f"{prompt} [y/N]: ").strip().lower()
        return answer in {"y", "yes"}

    def confirm_experimental(self, label: str) -> bool:
        return self.confirm(
            f"{label} may affect notifications, sync, or device stability. Continue?"
        )

    def check_battery(self) -> None:
        self.output("\n--- Battery Status ---")
        self.output(self.client.shell_text(["dumpsys", "battery"], check=False))
        self.output("\n--- BatteryStats Summary (Since Charged) ---")
        output = self.client.shell_text(["dumpsys", "batterystats", "--charged"], check=False)
        found = False
        for line in output.splitlines():
            stripped = line.strip()
            if any(token in stripped for token in ("Estimated power use", "Capacity:", "Computed drain:")):
                found = True
                self.output(stripped)
                continue
            if found:
                if stripped and ("mAh" in stripped or ":" in stripped):
                    self.output(stripped)
                elif stripped and not line.startswith("  "):
                    break

    def apply_documented_safe_optimizations(self) -> None:
        self.output("Applying documented safe optimizations...")
        self.recorder.put_device_config("activity_manager", "bg_auto_restrict_abusive_apps", 1)
        self.recorder.put_device_config(
            "activity_manager",
            "bg_current_drain_auto_restrict_abusive_apps_enabled",
            1,
        )
        self.output("Applied abusive-app auto restriction tracking from AOSP documentation.")

    def apply_experimental_optimizations(self) -> None:
        if not self.confirm_experimental("Experimental optimizations"):
            self.output("Skipped experimental optimizations.")
            return

        self.output("Applying experimental optimizations...")
        doze_settings = {
            "light_after_inactive_to": "0",
            "light_pre_idle_to": "15000",
            "light_idle_to": "10000",
            "light_idle_factor": "2",
            "light_max_idle_to": "30000",
            "inactive_to": "15000",
            "sensing_to": "0",
            "locating_to": "0",
            "motion_inactive_to": "0",
            "idle_after_inactive_to": "0",
            "quick_doze_delay_to": "5000",
        }
        for key, value in doze_settings.items():
            self.recorder.put_device_config("device_idle", key, value)

        settings_to_apply = [
            ("global", "window_animation_scale", "0.5"),
            ("global", "transition_animation_scale", "0.5"),
            ("global", "animator_duration_scale", "0.5"),
            ("global", "ble_scan_always_enabled", "0"),
            ("system", "nearby_scanning_enabled", "0"),
            ("global", "wifi_scan_throttle_enabled", "1"),
            ("global", "mobile_data_always_on", "0"),
            ("global", "cached_apps_freezer", "enabled"),
            ("global", "adaptive_battery_management_enabled", "1"),
            ("global", "low_power", "1"),
        ]
        for namespace, key, value in settings_to_apply:
            self.recorder.put_setting(namespace, key, value)

        constants = (
            "advertise_is_enabled=true,"
            "datasaver_disabled=false,"
            "enable_night_mode=true,"
            "launch_boost_disabled=true,"
            "vibration_disabled=true,"
            "animation_disabled=true,"
            "soundtrigger_disabled=true,"
            "fullbackup_deferred=true,"
            "keyvaluebackup_deferred=true,"
            "firewall_disabled=true,"
            "gps_mode=0,"
            "adjust_brightness_disabled=false,"
            "adjust_brightness_factor=2,"
            "force_all_apps_standby=true,"
            "force_background_check=true,"
            "optional_sensors_disabled=true,"
            "aod_disabled=false,"
            "quick_doze_enabled=true"
        )
        self.recorder.put_setting("global", "battery_saver_constants", constants)
        self.apply_documented_safe_optimizations()
        self.output("Experimental optimizations applied.")

    def apply_samsung_experimental_optimizations(self) -> None:
        brand = self.client.shell_text(["getprop", "ro.product.brand"], check=False)
        if brand.lower() != "samsung":
            self.output("Connected device is not Samsung.")
            return
        if not self.confirm_experimental("Samsung experimental optimizations"):
            self.output("Skipped Samsung experimental optimizations.")
            return

        self.output("Applying Samsung experimental optimizations...")
        samsung_settings = {
            "system": {
                "master_motion": "0",
                "motion_engine": "0",
                "air_motion_engine": "0",
                "air_motion_wake_up": "0",
                "mcf_continuity": "0",
                "intelligent_sleep_mode": "0",
                "nearby_scanning_enabled": "0",
                "nearby_scanning_permission_allowed": "0",
            },
            "global": {
                "ram_expand_size": "0",
                "enhanced_processing": "0",
            },
            "secure": {
                "vibration_on": "0",
                "adaptive_sleep": "0",
                "game_auto_temperature_control": "0",
                "game_bixby_block": "1",
            },
        }
        for namespace, values in samsung_settings.items():
            for key, value in values.items():
                self.recorder.put_setting(namespace, key, value)

        for package in (
            "com.samsung.android.game.gos",
            "com.samsung.android.game.gamelab",
        ):
            if package in self.get_installed_packages_set():
                self.recorder.set_package_enabled(package, enabled=False)

        self.output("Samsung experimental optimizations applied.")

    def restrict_background_apps(self, level: str = "ignore") -> None:
        if not self.confirm_experimental("Third-party app background restrictions"):
            self.output("Skipped third-party app restrictions.")
            return

        whitelist = set(self.load_whitelist())
        packages = self.get_packages(third_party=True)
        installed = self.get_installed_packages_set()
        self.output(f"Setting RUN_ANY_IN_BACKGROUND={level} for third-party apps...")
        for package in packages:
            if package not in installed:
                continue
            if package in whitelist:
                self.output(f"  Skipping whitelisted app: {package}")
                self.recorder.set_appop(package, "RUN_ANY_IN_BACKGROUND", "allow")
                self.recorder.set_standby_bucket(package, "active")
                continue
            self.recorder.set_appop(package, "RUN_ANY_IN_BACKGROUND", level)
            bucket = "rare" if level == "ignore" else "active"
            self.recorder.set_standby_bucket(package, bucket)
        self.output("Background restrictions updated.")

    def manage_whitelist(self) -> None:
        whitelist = self.load_whitelist()
        installed = self.get_packages(third_party=True)
        while True:
            self.output("\n--- Whitelist Management ---")
            if whitelist:
                for index, package in enumerate(whitelist, start=1):
                    self.output(f"  {index}. {package}")
            else:
                self.output("  (empty)")

            self.output("\n1. Add App to Whitelist")
            self.output("2. Remove App from Whitelist")
            self.output("3. Back")
            choice = self.input("Select an option: ").strip()
            if choice == "1":
                query = self.input("Enter package name or a search term: ").strip()
                matches = resolve_package_choice(query, installed)
                if not matches:
                    self.output("No installed packages matched that query.")
                    continue
                if len(matches) == 1:
                    package = matches[0]
                else:
                    for index, package in enumerate(matches, start=1):
                        self.output(f"  {index}. {package}")
                    selected = self.input("Select number to add (or 0 to cancel): ").strip()
                    if not selected.isdigit():
                        self.output("Invalid selection.")
                        continue
                    item = int(selected)
                    if item == 0:
                        continue
                    if item < 1 or item > len(matches):
                        self.output("Invalid selection.")
                        continue
                    package = matches[item - 1]
                if package not in whitelist:
                    whitelist.append(package)
                    whitelist.sort()
                    self.save_whitelist(whitelist)
                    self.output(f"Added {package}.")
                else:
                    self.output(f"{package} is already whitelisted.")
            elif choice == "2":
                if not whitelist:
                    self.output("Whitelist is empty.")
                    continue
                selected = self.input("Enter number to remove: ").strip()
                if not selected.isdigit():
                    self.output("Invalid selection.")
                    continue
                item = int(selected)
                if item < 1 or item > len(whitelist):
                    self.output("Invalid selection.")
                    continue
                removed = whitelist.pop(item - 1)
                self.save_whitelist(whitelist)
                self.output(f"Removed {removed}.")
            elif choice == "3":
                return
            else:
                self.output("Invalid selection.")

    def run_bg_dexopt(self) -> None:
        if not self.confirm_experimental("Background dexopt job"):
            self.output("Skipped dexopt.")
            return
        self.output("Triggering background package optimization (dexopt)...")
        self.client.shell(["cmd", "package", "bg-dexopt-job"], mutate=True)
        self.output("Dexopt job triggered.")

    def revert_saved_state(self) -> None:
        if not self.store.has_entries():
            self.output("No saved state found to restore.")
            return
        self.output("Restoring saved state...")
        for message in self.recorder.restore():
            self.output(f"  {message}")
        self.output("Restore finished.")

    def run(self) -> int:
        if not self.check_environment():
            return 1

        device = self.get_device_info()
        self.output(f"Connected to: {device}")
        while True:
            self.output("\n--- Android Battery Optimizer ---")
            self.output("1. Check Battery Status")
            self.output("2. Apply Documented Safe Optimizations")
            self.output("3. Apply Experimental Optimizations")
            self.output("4. Apply Samsung Experimental Optimizations")
            self.output("5. Restrict 3rd Party Apps (Experimental, with Whitelist)")
            self.output("6. Manage Whitelist")
            self.output("7. Run Background Optimization (Dexopt, Experimental)")
            self.output("8. Revert Saved State")
            self.output("9. Exit")
            choice = self.input("\nSelect an option: ").strip()
            try:
                if choice == "1":
                    self.check_battery()
                elif choice == "2":
                    self.apply_documented_safe_optimizations()
                elif choice == "3":
                    self.apply_experimental_optimizations()
                elif choice == "4":
                    self.apply_samsung_experimental_optimizations()
                elif choice == "5":
                    self.restrict_background_apps(level="ignore")
                elif choice == "6":
                    self.manage_whitelist()
                elif choice == "7":
                    self.run_bg_dexopt()
                elif choice == "8":
                    self.revert_saved_state()
                elif choice == "9":
                    return 0
                else:
                    self.output("Invalid selection.")
            except (CommandError, ValueError) as exc:
                self.output(f"Error: {exc}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    state_dir = Path(args.state_dir).expanduser()
    client = AdbClient(
        runner=SubprocessRunner(),
        serial=args.serial,
        dry_run=args.dry_run,
    )
    app = BatteryOptimizerApp(client=client, state_dir=state_dir)
    return app.run()


if __name__ == "__main__":
    sys.exit(main())
