import shutil
from pathlib import Path
from typing import List, Optional, Sequence, Set
from .adb import AdbClient, CommandError
from .state import StateStore
from .recorder import PACKAGE_USER_ID, StateRecorder

WHITELIST_FILE = "whitelist.txt"

class BatteryOptimizerApp:
    def __init__(self, client: AdbClient, state_dir: Path) -> None:
        self.client = client
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._whitelist_migration_announced = False
        self.store = StateStore(self.state_dir, client)
        self.recorder = StateRecorder(client, self.store)

    def rebind_device(self) -> None:
        self.store.rebind()

    @property
    def whitelist_path(self) -> Path:
        serial = self.client.serial or "unknown-device"
        safe_serial = StateStore.sanitize_serial(serial)
        return self.state_dir / "devices" / safe_serial / WHITELIST_FILE

    @property
    def legacy_whitelist_path(self) -> Path:
        return self.state_dir / WHITELIST_FILE

    def _migrate_legacy_whitelist_if_needed(self) -> None:
        current_path = self.whitelist_path
        if current_path.exists():
            return

        legacy_path = self.legacy_whitelist_path
        if not legacy_path.exists():
            return

        current_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(legacy_path, current_path)
        if not self._whitelist_migration_announced:
            self.client.output(
                f"Migrated legacy whitelist.txt to {current_path}."
            )
            self._whitelist_migration_announced = True

    def load_whitelist(self) -> List[str]:
        self._migrate_legacy_whitelist_if_needed()
        if not self.whitelist_path.exists():
            return []
        with self.whitelist_path.open("r", encoding="utf-8") as handle:
            return [line.strip() for line in handle if line.strip()]

    def save_whitelist(self, packages: Sequence[str]) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._migrate_legacy_whitelist_if_needed()
        self.whitelist_path.parent.mkdir(parents=True, exist_ok=True)
        with self.whitelist_path.open("w", encoding="utf-8") as handle:
            for package in packages:
                handle.write(f"{package}\n")


    def get_device_info(self) -> str:
        info = self.client.get_device_info_struct()
        return f"{info.brand} {info.model} (Android {info.android_release})".strip()

    def get_packages(self, third_party: bool = True, user_id: Optional[str] = None) -> List[str]:
        args: List[object] = ["pm", "list", "packages"]
        if user_id is not None:
            args.extend(["--user", user_id])
        if third_party:
            args.append("-3")
        result = self.client.shell(args, check=False)
        if result.returncode != 0:
            stderr = result.stderr.strip()
            stdout = result.stdout.strip()
            details = stderr or stdout or "unknown error"
            raise CommandError(
                f"Failed to list packages with `{' '.join(str(arg) for arg in args)}`: {details}",
                result=result,
            )

        output = result.stdout.strip()
        packages = []
        for line in output.splitlines():
            if ":" in line:
                packages.append(line.split(":", 1)[1].strip())
        return sorted(packages)

    def get_installed_packages_set(self, user_id: Optional[str] = None) -> Set[str]:
        return set(self.get_packages(third_party=False, user_id=user_id))

    def validate_package(self, package: str) -> None:
        if package not in self.get_installed_packages_set():
            raise ValueError(f"Package `{package}` is not installed on the connected device.")

    def apply_documented_safe_optimizations(self) -> None:
        if not self.client.supports_device_config():
            raise ValueError("Device does not support `device_config` command. Optimization aborted.")

        with self.recorder.transaction():
            self.recorder.put_device_config("activity_manager", "bg_auto_restrict_abusive_apps", 1)
            self.recorder.put_device_config(
                "activity_manager",
                "bg_current_drain_auto_restrict_abusive_apps_enabled",
                1,
            )

    def apply_experimental_optimizations(self) -> None:
        info = self.client.get_device_info_struct()
        # SDK 26 (Android 8.0) is a conservative minimum for device_config and stable settings behavior
        if info.sdk_int < 26:
             raise ValueError(f"Device SDK {info.sdk_int} is too old for experimental optimizations (min SDK 26 required).")

        if not self.client.supports_device_config():
            raise ValueError("Device does not support `device_config` command. Experimental optimization aborted.")

        for namespace in ("global", "system", "secure"):
            if not self.client.supports_settings_namespace(namespace):
                raise ValueError(f"Device does not support `settings` namespace `{namespace}`. Experimental optimization aborted.")

        with self.recorder.transaction():
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

    def apply_samsung_experimental_optimizations(self) -> None:
        info = self.client.get_device_info_struct()
        if info.brand.lower() != "samsung":
            raise ValueError("Connected device is not Samsung.")

        for namespace in ("system", "global", "secure"):
            if not self.client.supports_settings_namespace(namespace):
                raise ValueError(f"Device does not support `settings` namespace `{namespace}`. Samsung optimization aborted.")

        with self.recorder.transaction():
            self.recorder.prefetch_package_states()
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

            installed = self.get_installed_packages_set(user_id=PACKAGE_USER_ID)
            for package in (
                "com.samsung.android.game.gos",
                "com.samsung.android.game.gamelab",
            ):
                if package in installed:
                    self.recorder.set_package_enabled(package, enabled=False)

    def restrict_background_apps(self, level: str = "ignore") -> List[str]:
        if not self.client.supports_appops():
            raise ValueError("Device does not support `appops` command via `cmd`. Background restriction aborted.")
        if not self.client.supports_standby_bucket():
            raise ValueError("Device does not support `am set-standby-bucket`. Background restriction aborted.")

        whitelist = set(self.load_whitelist())
        packages = self.get_packages(third_party=True)
        installed = self.get_installed_packages_set()
        skipped = []

        with self.recorder.transaction():
            self.recorder.prefetch_package_states()
            for package in packages:
                if package not in installed:
                    continue
                if package in whitelist:
                    skipped.append(package)
                    continue
                self.recorder.set_appop(package, "RUN_ANY_IN_BACKGROUND", level)
                bucket = "rare" if level == "ignore" else "active"
                self.recorder.set_standby_bucket(package, bucket)
        return skipped

    def run_bg_dexopt(self) -> None:
        self.client.shell(
            ["cmd", "package", "bg-dexopt-job"],
            mutate=True,
            timeout=self.client.LONG_TIMEOUT_SECONDS,
        )

    def revert_saved_state(self) -> List[str]:
        if not self.store.has_entries():
            return []
        return self.recorder.restore()
