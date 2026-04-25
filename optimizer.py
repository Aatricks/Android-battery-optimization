import argparse
import copy
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from contextlib import contextmanager
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
    def __init__(self, message: str, result: Optional["CommandResult"] = None) -> None:
        super().__init__(message)
        self.result = result


class SnapshotError(RuntimeError):
    pass


class VerificationError(RuntimeError):
    pass


STANDBY_BUCKET_MAP = {
    "active": "10",
    "working_set": "20",
    "frequent": "30",
    "rare": "40",
    "restricted": "45",
    "never": "50",
}


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class CommandRunner:
    def run(
        self,
        args: Sequence[str],
        input_data: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> CommandResult:
        raise NotImplementedError

    def which(self, name: str) -> Optional[str]:
        raise NotImplementedError


class SubprocessRunner(CommandRunner):
    def run(
        self,
        args: Sequence[str],
        input_data: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> CommandResult:
        try:
            completed = subprocess.run(
                args,
                capture_output=True,
                text=True,
                input=input_data,
                timeout=timeout,
            )
            return CommandResult(
                returncode=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            result = CommandResult(returncode=-1, stdout=stdout, stderr=stderr)
            raise CommandError(
                f"Command timed out after {timeout}s: {' '.join(args)}",
                result=result,
            ) from exc

    def which(self, name: str) -> Optional[str]:
        return shutil.which(name)


@dataclass
class DeviceInfo:
    serial: str
    brand: str
    model: str
    android_release: str
    sdk_int: int
    fingerprint: str


class AdbClient:
    DEFAULT_TIMEOUT_SECONDS = 30
    LONG_TIMEOUT_SECONDS = 300

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

    def get_device_info_struct(self) -> DeviceInfo:
        serial = self.serial or "unknown-device"
        brand = self.shell_text(["getprop", "ro.product.brand"], check=False)
        model = self.shell_text(["getprop", "ro.product.model"], check=False)
        release = self.shell_text(["getprop", "ro.build.version.release"], check=False)
        sdk_str = self.shell_text(["getprop", "ro.build.version.sdk"], check=False)
        fingerprint = self.shell_text(["getprop", "ro.build.fingerprint"], check=False)

        try:
            sdk_int = int(sdk_str)
        except (ValueError, TypeError):
            sdk_int = 0

        return DeviceInfo(
            serial=serial,
            brand=brand,
            model=model,
            android_release=release,
            sdk_int=sdk_int,
            fingerprint=fingerprint,
        )

    def get_device_metadata(self) -> Dict[str, str]:
        info = self.get_device_info_struct()
        return {
            "serial": info.serial,
            "brand": info.brand,
            "model": info.model,
            "android_release": info.android_release,
            "sdk": str(info.sdk_int),
            "fingerprint": info.fingerprint,
        }

    def supports_device_config(self) -> bool:
        try:
            # device_config list will return exit code 0 if supported
            # some devices might return help text, some might just work.
            # a safe check is running 'device_config list' which should not fail.
            result = self.shell(["device_config", "list"], check=False)
            return result.returncode == 0
        except Exception:
            return False

    def supports_appops(self) -> bool:
        try:
            # 'cmd appops' was introduced in Android 6.0 (SDK 23)
            # but 'appops' command itself might vary.
            # 'cmd appops help' is a safe way to check support.
            result = self.shell(["cmd", "appops", "help"], check=False)
            return result.returncode == 0
        except Exception:
            return False

    def supports_standby_bucket(self) -> bool:
        try:
            # Standby buckets were introduced in Android 9 (SDK 28)
            # 'am set-standby-bucket' should be available.
            # We can check if 'am' help mentions it or just check if the command exists.
            # A more conservative check is SDK >= 28.
            info = self.get_device_info_struct()
            if info.sdk_int < 28:
                return False
            # Also check if command actually runs (without arguments it should show error/help)
            result = self.shell(["am", "set-standby-bucket"], check=False)
            # am set-standby-bucket usually returns error code if no args, but if command is missing
            # it will return 'not found' or similar with non-zero exit code.
            # However, some 'am' versions might return 0 even for errors.
            # 'cmd' based approach is better if available.
            return result.returncode != 127 # 127 is usually command not found
        except Exception:
            return False

    def supports_settings_namespace(self, namespace: str) -> bool:
        try:
            result = self.shell(["settings", "list", namespace], check=False)
            return result.returncode == 0
        except Exception:
            return False

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
        self,
        args: Sequence[object],
        *,
        mutate: bool = False,
        check: bool = True,
        input_data: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> CommandResult:
        command = self._base_command() + self._stringify(args)
        if mutate and self.dry_run:
            self.output(f"[dry-run] {self._format(command)}")
            if input_data:
                self.output(f"[dry-run-input]\n{input_data}")
            return CommandResult(returncode=0, stdout="", stderr="")

        if timeout is None and mutate:
            timeout = self.DEFAULT_TIMEOUT_SECONDS

        result = self.runner.run(command, input_data=input_data, timeout=timeout)
        if check and result.returncode != 0:
            stderr = result.stderr.strip()
            stdout = result.stdout.strip()
            details = stderr or stdout or "unknown error"
            raise CommandError(f"{self._format(command)} failed: {details}", result=result)
        return result

    def shell(
        self,
        args: Sequence[object],
        *,
        mutate: bool = False,
        check: bool = True,
        input_data: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> CommandResult:
        return self.run_adb(
            ["shell", *args],
            mutate=mutate,
            check=check,
            input_data=input_data,
            timeout=timeout,
        )

    def shell_text(
        self,
        args: Sequence[object],
        *,
        mutate: bool = False,
        check: bool = True,
        input_data: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> str:
        return self.shell(
            args, mutate=mutate, check=check, input_data=input_data, timeout=timeout
        ).stdout.strip()

    def local_text(
        self,
        args: Sequence[object],
        *,
        check: bool = True,
        input_data: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> str:
        result = self.runner.run(
            self._stringify(args), input_data=input_data, timeout=timeout
        )
        if check and result.returncode != 0:
            details = result.stderr.strip() or result.stdout.strip() or "unknown error"
            raise CommandError(
                f"{self._format(self._stringify(args))} failed: {details}", result=result
            )
        return result.stdout.strip()


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


class StateRecorder:
    def __init__(self, client: AdbClient, store: StateStore) -> None:
        self.client = client
        self.store = store
        self.verify = True
        self._in_transaction = False
        self._batched_commands: List[str] = []
        self._settings_cache: Dict[str, Dict[str, str]] = {}
        self._device_config_cache: Dict[str, Dict[str, str]] = {}
        self._appops_cache: Dict[str, Dict[str, str]] = {}
        self._standby_bucket_cache: Dict[str, str] = {}
        self._package_enabled_cache: Dict[str, bool] = {}
        self._ledger: List[Dict[str, object]] = []
        self._prefetch_package_enabled_success = False
        self._prefetch_appops_success = False
        self._prefetch_standby_bucket_success = False

    @staticmethod
    def _normalize_value(value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        stripped = value.strip()
        if stripped in {"", "null", "None", "undefined"}:
            return None
        return stripped

    @contextmanager
    def transaction(self):
        if self._in_transaction:
            yield
            return

        self._in_transaction = True
        self._batched_commands = []
        self._settings_cache.clear()
        self._device_config_cache.clear()
        self._appops_cache.clear()
        self._standby_bucket_cache.clear()
        self._package_enabled_cache.clear()
        self._ledger = []
        batch_dispatched = False

        try:
            with self.store.transaction():
                yield
                if self._batched_commands:
                    script_lines = []
                    for i, cmd in enumerate(self._batched_commands):
                        script_lines.append(f"{cmd} && echo \"SUCCESS_{i}\" || exit $?")
                    script = "\n".join(script_lines)
                    batch_dispatched = True
                    self.client.shell([], mutate=True, input_data=script)
                    # Verify all entries after batch success
                    if self.verify:
                        for entry in self._ledger:
                            self._verify_entry(entry)
        except Exception as exc:
            if batch_dispatched:
                stdout = getattr(getattr(exc, "result", None), "stdout", "")
                successful_indices = [
                    int(m.group(1)) for m in re.finditer(r"SUCCESS_(\d+)", stdout)
                ]
                # If VerificationError, we want to revert ALL commands that were sent in the batch
                # because we don't know for sure which one failed and we want to keep it atomic.
                # SUCCESS_i indicates it was successfully executed, so it should be reverted.
                if isinstance(exc, VerificationError):
                    # For verification error, all successfully dispatched commands should be rolled back.
                    self._revert_ledger(successful_indices)
                else:
                    self._revert_ledger(successful_indices)
            raise
        finally:
            self._in_transaction = False
            self._batched_commands = []
            self._settings_cache.clear()
            self._device_config_cache.clear()
            self._appops_cache.clear()
            self._standby_bucket_cache.clear()
            self._package_enabled_cache.clear()
            self._ledger = []

    def _queue_or_run(self, args: Sequence[object]) -> None:
        if self._in_transaction:
            cmd = self.client._format(self.client._stringify(args))
            self._batched_commands.append(cmd)
        else:
            self.client.shell(args, mutate=True)

    def _revert_ledger(self, successful_indices: Optional[List[int]] = None) -> None:
        failures = []
        entries_to_revert = []
        if successful_indices is not None:
            # Revert in reverse order of indices to be safe
            for idx in sorted(successful_indices, reverse=True):
                if idx < len(self._ledger):
                    entries_to_revert.append(self._ledger[idx])
        else:
            entries_to_revert = list(reversed(self._ledger))

        for entry in entries_to_revert:
            type_ = entry["type"]
            try:
                if type_ == "setting":
                    namespace = str(entry["namespace"])
                    key = str(entry["key"])
                    prior_value = entry["prior_value"]
                    if prior_value is None:
                        self.client.shell(["settings", "delete", namespace, key], mutate=True)
                    else:
                        self.client.shell(["settings", "put", namespace, key, prior_value], mutate=True)
                elif type_ == "device_config":
                    namespace = str(entry["namespace"])
                    key = str(entry["key"])
                    prior_value = entry["prior_value"]
                    if prior_value is None:
                        self.client.shell(["device_config", "delete", namespace, key], mutate=True)
                    else:
                        self.client.shell(["device_config", "put", namespace, key, prior_value], mutate=True)
                elif type_ == "appop":
                    package = str(entry["package"])
                    op = str(entry["op"])
                    prior_value = entry["prior_value"]
                    if prior_value == "default" or prior_value is None:
                        self.client.shell(["cmd", "appops", "reset", package, op], mutate=True)
                    else:
                        self.client.shell(["cmd", "appops", "set", package, op, prior_value], mutate=True)
                elif type_ == "standby_bucket":
                    package = str(entry["package"])
                    prior_value = entry["prior_value"]
                    if prior_value:
                        self.client.shell(["am", "set-standby-bucket", package, str(prior_value)], mutate=True)
                elif type_ == "package_enabled":
                    package = str(entry["package"])
                    prior_value = bool(entry["prior_value"])
                    command = ["pm", "enable", "--user", "0", package]
                    if not prior_value:
                        command = ["pm", "disable-user", "--user", "0", package]
                    self.client.shell(command, mutate=True)
            except CommandError as exc:
                msg = f"Rollback failed for {type_} {entry}: {exc}"
                failures.append(msg)
                self.client.output(msg)
                self._persist_failed_rollback(entry)
                
        if failures:
            self.client.output("Warning: Partial state corruption due to rollback failures.")
            self.store.save()

    def _persist_failed_rollback(self, entry: Dict[str, object]) -> None:
        type_ = entry["type"]
        if type_ == "setting":
            store = self.store.data["settings"]
            snapshot_key = f"{entry['namespace']}/{entry['key']}"
            if snapshot_key not in store:
                store[snapshot_key] = {
                    "namespace": entry["namespace"],
                    "key": entry["key"],
                    "value": entry["prior_value"],
                }
        elif type_ == "device_config":
            store = self.store.data["device_config"]
            snapshot_key = f"{entry['namespace']}/{entry['key']}"
            if snapshot_key not in store:
                store[snapshot_key] = {
                    "namespace": entry["namespace"],
                    "key": entry["key"],
                    "value": entry["prior_value"],
                }
        elif type_ == "appop":
            package = str(entry["package"])
            op = str(entry["op"])
            pkg_entry = self._package_entry(package)
            if op not in pkg_entry["appops"]:
                pkg_entry["appops"][op] = entry["prior_value"]
        elif type_ == "standby_bucket":
            package = str(entry["package"])
            pkg_entry = self._package_entry(package)
            if pkg_entry["standby_bucket"] is None:
                pkg_entry["standby_bucket"] = entry["prior_value"]
        elif type_ == "package_enabled":
            package = str(entry["package"])
            pkg_entry = self._package_entry(package)
            if pkg_entry["enabled"] is None:
                pkg_entry["enabled"] = entry["prior_value"]

    def prefetch_package_states(self) -> None:
        try:
            disabled = self.client.shell_text(["pm", "list", "packages", "-d"])
            enabled = self.client.shell_text(["pm", "list", "packages", "-e"])
            for line in disabled.splitlines():
                if ":" in line:
                    self._package_enabled_cache[line.split(":", 1)[1].strip()] = False
            for line in enabled.splitlines():
                if ":" in line:
                    self._package_enabled_cache[line.split(":", 1)[1].strip()] = True
            self._prefetch_package_enabled_success = True
        except CommandError:
            self._prefetch_package_enabled_success = False

        try:
            appops = self.client.shell_text(["dumpsys", "appops"])
            current_pkg = None
            for line in appops.splitlines():
                pkg_match = re.search(r"Package\s+([a-zA-Z0-9_\.]+):", line)
                if pkg_match:
                    current_pkg = pkg_match.group(1)
                    self._appops_cache.setdefault(current_pkg, {})
                    continue
                if current_pkg:
                    op_match = re.search(r"\s+([A-Z_a-z0-9]+):\s*([a-zA-Z0-9_]+)", line)
                    if op_match:
                        self._appops_cache.setdefault(current_pkg, {})[op_match.group(1)] = op_match.group(2)
            self._prefetch_appops_success = True
        except CommandError:
            self._prefetch_appops_success = False

        try:
            usagestats = self.client.shell_text(["dumpsys", "usagestats"])
            for line in usagestats.splitlines():
                if "package=" in line and "bucket=" in line:
                    pkg_match = re.search(r"package=([a-zA-Z0-9_\.]+)", line)
                    bucket_match = re.search(r"bucket=(\d+|[a-zA-Z_]+)", line)
                    if pkg_match and bucket_match:
                        self._standby_bucket_cache[pkg_match.group(1)] = bucket_match.group(1)
            self._prefetch_standby_bucket_success = True
        except CommandError:
            self._prefetch_standby_bucket_success = False

    def _get_setting(self, namespace: str, key: str) -> Optional[str]:
        if namespace not in self._settings_cache:
            try:
                output = self.client.shell_text(["settings", "list", namespace], check=False)
                cache = {}
                for line in output.splitlines():
                    if "=" in line:
                        k, v = line.split("=", 1)
                        cache[k.strip()] = v.strip()
                self._settings_cache[namespace] = cache
            except CommandError:
                self._settings_cache[namespace] = {}
        return self._settings_cache[namespace].get(key)

    def snapshot_setting(self, namespace: str, key: str, new_value: Optional[str] = None) -> None:
        store = self.store.data["settings"]
        snapshot_key = f"{namespace}/{key}"
        value = self._get_setting(namespace, key)
        value = self._normalize_value(value)
        if snapshot_key not in store:
            store[snapshot_key] = {
                "namespace": namespace,
                "key": key,
                "value": value,
            }
            self.store.save()
        self._ledger.append({
            "type": "setting",
            "namespace": namespace,
            "key": key,
            "prior_value": value,
            "new_value": self._normalize_value(new_value),
        })

    def put_setting(self, namespace: str, key: str, value: object, verify: bool = True) -> None:
        self.snapshot_setting(namespace, key, new_value=str(value))
        self._queue_or_run(["settings", "put", namespace, key, value])
        if not self._in_transaction and self.verify and verify:
            try:
                self.verify_setting(namespace, key, str(value))
            except VerificationError:
                self._revert_ledger()
                raise

    def delete_setting(self, namespace: str, key: str, verify: bool = True) -> None:
        self.snapshot_setting(namespace, key, new_value=None)
        self._queue_or_run(["settings", "delete", namespace, key])
        if not self._in_transaction and self.verify and verify:
            try:
                self.verify_setting(namespace, key, None)
            except VerificationError:
                self._revert_ledger()
                raise

    def _get_device_config(self, namespace: str, key: str) -> Optional[str]:
        if namespace not in self._device_config_cache:
            try:
                output = self.client.shell_text(["device_config", "list", namespace], check=False)
                cache = {}
                for line in output.splitlines():
                    if "=" in line:
                        k, v = line.split("=", 1)
                        cache[k.strip()] = v.strip()
                self._device_config_cache[namespace] = cache
            except CommandError:
                self._device_config_cache[namespace] = {}
        return self._device_config_cache[namespace].get(key)

    def snapshot_device_config(self, namespace: str, key: str, new_value: Optional[str] = None) -> None:
        store = self.store.data["device_config"]
        snapshot_key = f"{namespace}/{key}"
        value = self._get_device_config(namespace, key)
        value = self._normalize_value(value)
        if snapshot_key not in store:
            store[snapshot_key] = {
                "namespace": namespace,
                "key": key,
                "value": value,
            }
            self.store.save()
        self._ledger.append({
            "type": "device_config",
            "namespace": namespace,
            "key": key,
            "prior_value": value,
            "new_value": self._normalize_value(new_value),
        })

    def put_device_config(self, namespace: str, key: str, value: object, verify: bool = True) -> None:
        self.snapshot_device_config(namespace, key, new_value=str(value))
        self._queue_or_run(["device_config", "put", namespace, key, value])
        if not self._in_transaction and self.verify and verify:
            try:
                self.verify_device_config(namespace, key, str(value))
            except VerificationError:
                self._revert_ledger()
                raise

    def delete_device_config(self, namespace: str, key: str, verify: bool = True) -> None:
        self.snapshot_device_config(namespace, key, new_value=None)
        self._queue_or_run(["device_config", "delete", namespace, key])
        if not self._in_transaction and self.verify and verify:
            try:
                self.verify_device_config(namespace, key, None)
            except VerificationError:
                self._revert_ledger()
                raise

    def _package_entry(self, package: str) -> Dict[str, object]:
        packages = self.store.data["packages"]
        return packages.setdefault(
            package,
            {
                "enabled": None,
                "appops": {},
                "standby_bucket": None,
            },
        )

    def _get_package_enabled(self, package: str) -> bool:
        if not self._prefetch_package_enabled_success or package not in self._package_enabled_cache:
            raise SnapshotError(f"Could not determine enabled state for package: {package}")
        return self._package_enabled_cache[package]

    def snapshot_package_enabled(self, package: str, new_value: Optional[bool] = None) -> None:
        entry = self._package_entry(package)
        value = self._get_package_enabled(package)
        if entry["enabled"] is None:
            entry["enabled"] = value
            self.store.save()
        self._ledger.append({
            "type": "package_enabled",
            "package": package,
            "prior_value": value,
            "new_value": new_value,
        })

    def _get_appop(self, package: str, op: str) -> str:
        if not self._prefetch_appops_success:
            raise SnapshotError(f"AppOps data was not collected or command failed for package: {package}")
        if package not in self._appops_cache:
             raise SnapshotError(f"Could not determine appops for package: {package}")
        return self._appops_cache[package].get(op, "default")

    def snapshot_appop(self, package: str, op: str, new_value: Optional[str] = None) -> None:
        entry = self._package_entry(package)
        value = self._get_appop(package, op)
        if op not in entry["appops"]:
            entry["appops"][op] = value
            self.store.save()
        self._ledger.append({
            "type": "appop",
            "package": package,
            "op": op,
            "prior_value": value,
            "new_value": new_value,
        })

    def _get_standby_bucket(self, package: str) -> str:
        if not self._prefetch_standby_bucket_success or package not in self._standby_bucket_cache:
            raise SnapshotError(f"Could not determine standby bucket for package: {package}")
        return self._standby_bucket_cache[package]

    def snapshot_standby_bucket(self, package: str, new_value: Optional[str] = None) -> None:
        entry = self._package_entry(package)
        value = self._get_standby_bucket(package)
        if entry["standby_bucket"] is None:
            entry["standby_bucket"] = value
            self.store.save()
        self._ledger.append({
            "type": "standby_bucket",
            "package": package,
            "prior_value": value,
            "new_value": new_value,
        })

    def set_package_enabled(self, package: str, enabled: bool, verify: bool = True) -> None:
        self.snapshot_package_enabled(package, new_value=enabled)
        command = ["pm", "enable", "--user", "0", package]
        if not enabled:
            command = ["pm", "disable-user", "--user", "0", package]
        self._queue_or_run(command)
        if not self._in_transaction and self.verify and verify:
            try:
                self.verify_package_enabled(package, enabled)
            except VerificationError:
                self._revert_ledger()
                raise

    def set_appop(self, package: str, op: str, value: str, verify: bool = True) -> None:
        self.snapshot_appop(package, op, new_value=value)
        self._queue_or_run(["cmd", "appops", "set", package, op, value])
        if not self._in_transaction and self.verify and verify:
            try:
                self.verify_appop(package, op, value)
            except VerificationError:
                self._revert_ledger()
                raise

    def set_standby_bucket(self, package: str, bucket: str, verify: bool = True) -> None:
        self.snapshot_standby_bucket(package, new_value=bucket)
        self._queue_or_run(["am", "set-standby-bucket", package, bucket])
        if not self._in_transaction and self.verify and verify:
            try:
                self.verify_standby_bucket(package, bucket)
            except VerificationError:
                self._revert_ledger()
                raise

    def verify_setting(self, namespace: str, key: str, expected_value: Optional[str]) -> None:
        if self.client.dry_run:
            return
        actual = self.client.shell_text(["settings", "get", namespace, key], check=False)
        actual = self._normalize_value(actual)
        expected = self._normalize_value(expected_value)
        if actual != expected:
            raise VerificationError(
                f"Verification failed for setting {namespace}/{key}: "
                f"expected {expected}, got {actual}"
            )

    def verify_device_config(self, namespace: str, key: str, expected_value: Optional[str]) -> None:
        if self.client.dry_run:
            return
        actual = self.client.shell_text(["device_config", "get", namespace, key], check=False)
        actual = self._normalize_value(actual)
        expected = self._normalize_value(expected_value)
        if actual != expected:
            raise VerificationError(
                f"Verification failed for device_config {namespace}/{key}: "
                f"expected {expected}, got {actual}"
            )

    def verify_appop(self, package: str, op: str, expected_value: str) -> None:
        if self.client.dry_run:
            return
        output = self.client.shell_text(["cmd", "appops", "get", package, op], check=False)
        match = re.search(r"mode[:=]\s*(\w+)", output)
        actual = match.group(1) if match else "default"
        if actual != expected_value:
            raise VerificationError(
                f"Verification failed for appop {op} for package {package}: "
                f"expected {expected_value}, got {actual}"
            )

    def verify_standby_bucket(self, package: str, expected_bucket: str) -> None:
        if self.client.dry_run:
            return
        actual = self.client.shell_text(["am", "get-standby-bucket", package], check=False).strip()
        expected_code = STANDBY_BUCKET_MAP.get(expected_bucket.lower(), expected_bucket)
        if actual != expected_code:
            raise VerificationError(
                f"Verification failed for standby bucket for package {package}: "
                f"expected {expected_bucket} ({expected_code}), got {actual}"
            )

    def verify_package_enabled(self, package: str, expected_enabled: bool) -> None:
        if self.client.dry_run:
            return
        output = self.client.shell_text(
            ["pm", "list", "packages", "-e" if expected_enabled else "-d", package],
            check=False,
        )
        found = False
        for line in output.splitlines():
            if line.strip() == f"package:{package}":
                found = True
                break
        if not found:
            actual = "enabled" if not expected_enabled else "disabled/missing"
            raise VerificationError(
                f"Verification failed for package {package} enabled state: "
                f"expected {expected_enabled}, but package is {actual}"
            )

    def _verify_entry(self, entry: Dict[str, object]) -> None:
        type_ = entry["type"]
        new_value = entry.get("new_value")
        if type_ == "setting":
            self.verify_setting(
                str(entry["namespace"]),
                str(entry["key"]),
                str(new_value) if new_value is not None else None,
            )
        elif type_ == "device_config":
            self.verify_device_config(
                str(entry["namespace"]),
                str(entry["key"]),
                str(new_value) if new_value is not None else None,
            )
        elif type_ == "appop":
            self.verify_appop(str(entry["package"]), str(entry["op"]), str(new_value))
        elif type_ == "standby_bucket":
            self.verify_standby_bucket(str(entry["package"]), str(new_value))
        elif type_ == "package_enabled":
            self.verify_package_enabled(str(entry["package"]), bool(new_value))

    def restore(self) -> List[str]:
        # Refuse restore on device mismatch
        current_metadata = self.client.get_device_metadata()
        saved_device = self.store.data.get("device", {})
        
        if saved_device:
            if current_metadata["serial"] != saved_device.get("serial"):
                raise ValueError(
                    f"Device serial mismatch: current={current_metadata['serial']}, "
                    f"saved={saved_device.get('serial')}"
                )
            
            current_fp = current_metadata.get("fingerprint")
            saved_fp = saved_device.get("fingerprint")
            if current_fp and saved_fp and current_fp != saved_fp:
                raise ValueError(
                    f"Device fingerprint mismatch: current={current_fp}, saved={saved_fp}"
                )

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
                msg = f"Failed to restore setting {namespace}/{key}: {exc}"
                messages.append(msg)
                self.client.output(msg)

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
                msg = f"Failed to restore device_config {namespace}/{key}: {exc}"
                messages.append(msg)
                self.client.output(msg)

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
                    msg = f"Failed to restore {package} appop {op}: {exc}"
                    messages.append(msg)
                    self.client.output(msg)

            bucket = item.get("standby_bucket")
            if bucket is not None:
                try:
                    self.client.shell(
                        ["am", "set-standby-bucket", package, bucket],
                        mutate=True,
                    )
                    messages.append(f"Restored {package} standby bucket")
                except CommandError as exc:
                    had_failures = True
                    msg = f"Failed to restore {package} standby bucket: {exc}"
                    messages.append(msg)
                    self.client.output(msg)

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
                    msg = f"Failed to restore {package} enabled state: {exc}"
                    messages.append(msg)
                    self.client.output(msg)

        if had_failures:
            self.store.save()
            self.client.output("Warning: Partial state corruption due to restore failures.")
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
    def __init__(self, client: AdbClient, state_dir: Path) -> None:
        self.client = client
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.whitelist_path = self.state_dir / WHITELIST_FILE
        self.store = StateStore(self.state_dir, client)
        self.recorder = StateRecorder(client, self.store)

    def rebind_device(self) -> None:
        self.store.rebind()

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


    def get_device_info(self) -> str:
        info = self.client.get_device_info_struct()
        return f"{info.brand} {info.model} (Android {info.android_release})".strip()

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

            installed = self.get_installed_packages_set()
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


class BatteryOptimizerCLI:
    def __init__(
        self,
        app: BatteryOptimizerApp,
        output: Callable[[str], None] = print,
        input_fn: Callable[[str], str] = input,
    ) -> None:
        self.app = app
        self.client = app.client
        self.output = output
        self.input = input_fn

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
            self.app.rebind_device()
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
        self.app.rebind_device()
        return True

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

    def manage_whitelist(self) -> None:
        whitelist = self.app.load_whitelist()
        installed = self.app.get_packages(third_party=True)
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
                    self.app.save_whitelist(whitelist)
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
                self.app.save_whitelist(whitelist)
                self.output(f"Removed {removed}.")
            elif choice == "3":
                return
            else:
                self.output("Invalid selection.")

    def run(self) -> int:
        if not self.check_environment():
            return 1

        device = self.app.get_device_info()
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
                    self.output("Applying documented safe optimizations...")
                    self.app.apply_documented_safe_optimizations()
                    self.output("Applied abusive-app auto restriction tracking from AOSP documentation.")
                elif choice == "3":
                    if not self.confirm_experimental("Experimental optimizations"):
                        self.output("Skipped experimental optimizations.")
                        continue
                    self.output("Applying experimental optimizations...")
                    self.app.apply_experimental_optimizations()
                    self.output("Experimental optimizations applied.")
                elif choice == "4":
                    brand = self.app.get_device_info()
                    if "samsung" not in brand.lower():
                        self.output("Connected device is not Samsung.")
                        continue
                    if not self.confirm_experimental("Samsung experimental optimizations"):
                        self.output("Skipped Samsung experimental optimizations.")
                        continue
                    self.output("Applying Samsung experimental optimizations...")
                    self.app.apply_samsung_experimental_optimizations()
                    self.output("Samsung experimental optimizations applied.")
                elif choice == "5":
                    if not self.confirm_experimental("Third-party app background restrictions"):
                        self.output("Skipped third-party app restrictions.")
                        continue
                    self.output("Setting RUN_ANY_IN_BACKGROUND=ignore for third-party apps...")
                    skipped = self.app.restrict_background_apps(level="ignore")
                    for pkg in skipped:
                        self.output(f"  Skipping whitelisted app: {pkg}")
                    self.output("Background restrictions updated.")
                elif choice == "6":
                    self.manage_whitelist()
                elif choice == "7":
                    if not self.confirm_experimental("Background dexopt job"):
                        self.output("Skipped dexopt.")
                        continue
                    self.output("Triggering background package optimization (dexopt)...")
                    self.app.run_bg_dexopt()
                    self.output("Dexopt job triggered.")
                elif choice == "8":
                    self.output("Restoring saved state...")
                    messages = self.app.revert_saved_state()
                    if not messages:
                        self.output("No saved state found to restore.")
                    else:
                        for msg in messages:
                            self.output(f"  {msg}")
                        self.output("Restore finished.")
                elif choice == "9":
                    return 0
                else:
                    self.output("Invalid selection.")
            except (CommandError, ValueError, SnapshotError, VerificationError) as exc:
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
    cli = BatteryOptimizerCLI(app=app)
    return cli.run()


if __name__ == "__main__":
    sys.exit(main())
