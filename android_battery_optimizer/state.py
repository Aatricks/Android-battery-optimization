import copy
import json
import os
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Optional
from .adb import AdbClient

SNAPSHOT_FILE = "state.json"

class StateStore:
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

    def _sanitize_serial(self, serial: str) -> str:
        # Keep only A-Z, a-z, 0-9, dot, underscore, hyphen. Replace others with "_"
        return re.sub(r"[^A-Za-z0-9._-]", "_", serial)

    def rebind(self) -> None:
        serial = self.client.serial or "unknown-device"
        safe_serial = self._sanitize_serial(serial)
        device_dir = self.base_state_dir / "devices" / safe_serial
        self.path = device_dir / SNAPSHOT_FILE
        self.data = self._load()

    def _load(self) -> Dict[str, object]:
        if not self.path or not self.path.exists():
            return self._empty_state()

        try:
            with self.path.open("r", encoding="utf-8") as handle:
                return json.load(handle)
        except (json.JSONDecodeError, ValueError):
            import time
            timestamp = int(time.time())
            corrupt_path = self.path.with_name(f"{SNAPSHOT_FILE}.corrupt.{timestamp}")
            try:
                os.replace(self.path, corrupt_path)
            except OSError:
                pass
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
                    self.save()
            else:
                self.data = backup
                self._pending_save = False

    def save(self) -> None:
        if self.client.dry_run:
            return
        if getattr(self, "_in_transaction", False):
            self._pending_save = True
            return
        
        if not self.path:
            return

        # Ensure metadata is present if we are saving state for the first time or if it's empty
        if not self.data.get("device"):
            self.data["device"] = self.client.get_device_metadata()

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

    def clear(self) -> None:
        self.data = self._empty_state()
        if self.path and self.path.exists():
            self.path.unlink()

    def has_entries(self) -> bool:
        return any(self.data.get(key) for key in ("settings", "device_config", "packages"))
