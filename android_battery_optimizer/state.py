import copy
import json
import os
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Optional, cast
from .adb import AdbClient, CommandError

SNAPSHOT_FILE = "state.json"

class StateStore:
    @staticmethod
    def sanitize_serial(serial: str) -> str:
        # Keep only A-Z, a-z, 0-9, dot, underscore, hyphen. Replace others with "_"
        return re.sub(r"[^A-Za-z0-9._-]", "_", serial)

    def __init__(self, base_state_dir: Path, client: AdbClient) -> None:
        self.base_state_dir = base_state_dir
        self.client = client
        self.path: Optional[Path] = None
        self.data: Dict[str, object] = self._empty_state()
        self._in_transaction = False
        self._pending_save = False
        self.rebind()

    def _empty_state(self) -> Dict[str, object]:
        return {
            "version": 2,
            "device": {},
            "settings": {},
            "device_config": {},
            "packages": {},
        }

    def rebind(self) -> None:
        serial = self.client.serial or "unknown-device"
        safe_serial = self.sanitize_serial(serial)
        device_dir = self.base_state_dir / "devices" / safe_serial
        self.path = device_dir / SNAPSHOT_FILE
        self.data = self._load()

    def _quarantine_state_file(self) -> None:
        if not self.path or not self.path.exists():
            return

        import time

        timestamp = int(time.time())
        corrupt_path = self.path.with_name(f"{SNAPSHOT_FILE}.corrupt.{timestamp}")
        try:
            os.replace(self.path, corrupt_path)
        except OSError:
            pass

    def _require_dict_section(self, state: dict[str, object], key: str) -> dict[str, object]:
        value = state.get(key)
        if not isinstance(value, dict):
            raise ValueError(f"State field '{key}' must be an object.")
        return cast(dict[str, object], value)

    def _validate_scalar_kv_section(
        self,
        section_name: str,
        section: dict[str, object],
    ) -> None:
        for snapshot_key, item in section.items():
            if not isinstance(item, dict):
                raise ValueError(
                    f"State section '{section_name}' entry '{snapshot_key}' must be an object."
                )
            item = cast(dict[str, object], item)
            for required_key in ("namespace", "key", "value"):
                if required_key not in item:
                    raise ValueError(
                        f"State section '{section_name}' entry '{snapshot_key}' is missing '{required_key}'."
                    )

            namespace = item["namespace"]
            key = item["key"]
            value = item["value"]
            if not isinstance(namespace, str) or not isinstance(key, str):
                raise ValueError(
                    f"State section '{section_name}' entry '{snapshot_key}' must use string namespace/key."
                )
            if value is not None and not isinstance(value, str):
                raise ValueError(
                    f"State section '{section_name}' entry '{snapshot_key}' has non-string value."
                )

    def _validate_packages_section(self, packages: dict[str, object]) -> None:
        for package, item in packages.items():
            if not isinstance(package, str):
                raise ValueError("State packages keys must be strings.")
            if not isinstance(item, dict):
                raise ValueError(f"State packages entry '{package}' must be an object.")
            item = cast(dict[str, object], item)

            for required_key in ("appops", "standby_bucket", "enabled"):
                if required_key not in item:
                    raise ValueError(
                        f"State packages entry '{package}' is missing '{required_key}'."
                    )

            appops = item["appops"]
            standby_bucket = item["standby_bucket"]
            enabled = item["enabled"]

            if not isinstance(appops, dict):
                raise ValueError(f"State packages entry '{package}' appops must be an object.")
            for op, mode in appops.items():
                if not isinstance(op, str):
                    raise ValueError(f"State packages entry '{package}' has non-string appop key.")
                if not isinstance(mode, str):
                    raise ValueError(
                        f"State packages entry '{package}' appop '{op}' must be a string."
                    )

            if standby_bucket is not None and not isinstance(standby_bucket, str):
                raise ValueError(
                    f"State packages entry '{package}' standby_bucket must be a string or null."
                )
            if enabled is not None and not isinstance(enabled, bool):
                raise ValueError(
                    f"State packages entry '{package}' enabled must be boolean or null."
                )

    def _normalize_state(self, raw: object) -> dict[str, object]:
        if not isinstance(raw, dict):
            raise ValueError("State root must be an object.")
        raw = cast(dict[str, object], raw)

        version = raw.get("version", 2)
        if not isinstance(version, int) or isinstance(version, bool):
            raise ValueError("State field 'version' must be an integer.")

        device = self._require_dict_section(raw, "device")
        settings = self._require_dict_section(raw, "settings")
        device_config = self._require_dict_section(raw, "device_config")
        packages = self._require_dict_section(raw, "packages")

        self._validate_scalar_kv_section("settings", settings)
        self._validate_scalar_kv_section("device_config", device_config)
        self._validate_packages_section(packages)

        return {
            "version": version,
            "device": dict(device),
            "settings": dict(settings),
            "device_config": dict(device_config),
            "packages": dict(packages),
        }

    def _load(self) -> Dict[str, object]:
        if not self.path or not self.path.exists():
            return self._empty_state()

        try:
            with self.path.open("r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except (json.JSONDecodeError, ValueError):
            self._quarantine_state_file()
            return self._empty_state()

        try:
            return self._normalize_state(raw)
        except ValueError:
            self._quarantine_state_file()
            return self._empty_state()

    @contextmanager
    def transaction(self):
        if getattr(self, "_in_transaction", False):
            yield
            return

        backup = copy.deepcopy(self.data)
        self._in_transaction = True
        self._pending_save = False
        success = False
        try:
            yield
            success = True
        finally:
            self._in_transaction = False
            if success:
                if self._pending_save:
                    self.save_or_clear()
            else:
                self.data = backup
                self._pending_save = False

    def save(self) -> None:
        if self.client.dry_run:
            return
        if self.client.serial is None:
            raise CommandError("Refusing to mutate device state without a selected ADB serial.")
        if getattr(self, "_in_transaction", False):
            self._pending_save = True
            return

        if not self.path:
            return

        self._ensure_device_metadata()

        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(".tmp")
        try:
            with tmp_path.open("w", encoding="utf-8") as handle:
                json.dump(self.data, handle, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self.path)
        except Exception:
            if tmp_path.exists():
                tmp_path.unlink()
            raise

    def _ensure_device_metadata(self) -> None:
        if self.data.get("device"):
            return

        self.data["device"] = self.client.get_device_metadata_with_fallback()

    def clear(self) -> None:
        self.data = self._empty_state()
        if self.path and self.path.exists():
            self.path.unlink()

    def has_entries(self) -> bool:
        return any(self.data.get(key) for key in ("settings", "device_config", "packages"))

    def save_or_clear(self) -> None:
        if self.client.dry_run:
            return
        if self.has_entries():
            self.save()
        else:
            self.clear()
