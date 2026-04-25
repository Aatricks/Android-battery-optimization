import re
from contextlib import contextmanager
from typing import Dict, List, Optional, Sequence
from .adb import AdbClient, CommandError
from .state import StateStore
from .operations import STANDBY_BUCKET_MAP

class SnapshotError(RuntimeError):
    pass

class VerificationError(RuntimeError):
    pass

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
                    # If we reach here, batch shell succeeded.
                    if self.verify:
                        for entry in self._ledger:
                            self._verify_entry(entry)
        except Exception as exc:
            if batch_dispatched:
                if isinstance(exc, VerificationError):
                    # Batch succeeded, so all indices are successful
                    successful_indices = list(range(len(self._ledger)))
                    self._revert_ledger(successful_indices)
                else:
                    stdout = getattr(getattr(exc, "result", None), "stdout", "")
                    successful_indices = [
                        int(m.group(1)) for m in re.finditer(r"SUCCESS_(\d+)", stdout)
                    ]
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

    def _restore_appop_value(self, package: str, op: str, prior_value: Optional[str]) -> None:
        value = "default" if prior_value is None else str(prior_value)
        self.client.shell(["cmd", "appops", "set", package, op, value], mutate=True)

    def _revert_ledger(self, successful_indices: Optional[List[int]] = None) -> None:
        entries_to_revert = []
        if successful_indices is not None:
            # Revert in reverse order of indices to be safe
            for idx in sorted(successful_indices, reverse=True):
                if idx < len(self._ledger):
                    entries_to_revert.append(self._ledger[idx])
        else:
            entries_to_revert = list(reversed(self._ledger))

        had_failures = False
        for entry in entries_to_revert:
            try:
                self._perform_rollback(entry)
                self._remove_snapshot_for_entry(entry)
            except CommandError as exc:
                had_failures = True
                msg = f"Rollback failed for {entry}: {exc}"
                self.client.output(msg)
                self._persist_failed_rollback(entry)

        if had_failures:
            self.client.output("Warning: Partial state corruption due to rollback failures.")

        self.store.save()

    def _perform_rollback(self, entry: Dict[str, object]) -> None:
        type_ = entry["type"]
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
            self._restore_appop_value(package, op, prior_value)
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

    def _remove_snapshot_for_entry(self, entry: Dict[str, object]) -> None:
        type_ = entry["type"]
        if type_ == "setting":
            snapshot_key = f"{entry['namespace']}/{entry['key']}"
            self.store.data["settings"].pop(snapshot_key, None)
        elif type_ == "device_config":
            snapshot_key = f"{entry['namespace']}/{entry['key']}"
            self.store.data["device_config"].pop(snapshot_key, None)
        elif type_ == "appop":
            package = str(entry["package"])
            op = str(entry["op"])
            if package in self.store.data["packages"]:
                self.store.data["packages"][package]["appops"].pop(op, None)
                self._cleanup_package_entry(package)
        elif type_ == "standby_bucket":
            package = str(entry["package"])
            if package in self.store.data["packages"]:
                self.store.data["packages"][package]["standby_bucket"] = None
                self._cleanup_package_entry(package)
        elif type_ == "package_enabled":
            package = str(entry["package"])
            if package in self.store.data["packages"]:
                self.store.data["packages"][package]["enabled"] = None
                self._cleanup_package_entry(package)

    def _remove_snapshots_for_entries(self, entries: List[Dict[str, object]]) -> None:
        for entry in entries:
            self._remove_snapshot_for_entry(entry)

    def _cleanup_package_entry(self, package: str) -> None:
        pkg = self.store.data["packages"].get(package)
        if pkg and not pkg["appops"] and pkg["standby_bucket"] is None and pkg["enabled"] is None:
            self.store.data["packages"].pop(package)

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

    def _read_settings_namespace(self, namespace: str) -> Dict[str, str]:
        result = self.client.shell(["settings", "list", namespace], check=False)
        if result.returncode != 0:
            err = result.stderr.strip() or result.stdout.strip()
            raise SnapshotError(f"Failed to list settings in {namespace}: {err}")

        cache = {}
        for line in result.stdout.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                cache[k.strip()] = v.strip()
        return cache

    def _get_setting(self, namespace: str, key: str) -> Optional[str]:
        if namespace not in self._settings_cache:
            try:
                self._settings_cache[namespace] = self._read_settings_namespace(namespace)
            except SnapshotError as exc:
                raise SnapshotError(f"Failed to read setting {namespace}/{key}: {exc}") from exc
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

    def _read_device_config_namespace(self, namespace: str) -> Dict[str, str]:
        result = self.client.shell(["device_config", "list", namespace], check=False)
        if result.returncode != 0:
            err = result.stderr.strip() or result.stdout.strip()
            raise SnapshotError(f"Failed to list device_config in {namespace}: {err}")

        cache = {}
        for line in result.stdout.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                cache[k.strip()] = v.strip()
        return cache

    def _get_device_config(self, namespace: str, key: str) -> Optional[str]:
        if namespace not in self._device_config_cache:
            try:
                self._device_config_cache[namespace] = self._read_device_config_namespace(namespace)
            except SnapshotError as exc:
                raise SnapshotError(f"Failed to read device_config {namespace}/{key}: {exc}") from exc
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
        result = self.client.shell(["settings", "get", namespace, key], check=False)
        if result.returncode != 0:
            raise VerificationError(
                f"Verification failed for setting {namespace}/{key}: "
                f"read command failed with exit code {result.returncode}"
            )
        actual = self._normalize_value(result.stdout)
        expected = self._normalize_value(expected_value)
        if actual != expected:
            raise VerificationError(
                f"Verification failed for setting {namespace}/{key}: "
                f"expected {expected}, got {actual}"
            )

    def verify_device_config(self, namespace: str, key: str, expected_value: Optional[str]) -> None:
        if self.client.dry_run:
            return
        result = self.client.shell(["device_config", "get", namespace, key], check=False)
        if result.returncode != 0:
            raise VerificationError(
                f"Verification failed for device_config {namespace}/{key}: "
                f"read command failed with exit code {result.returncode}"
            )
        actual = self._normalize_value(result.stdout)
        expected = self._normalize_value(expected_value)
        if actual != expected:
            raise VerificationError(
                f"Verification failed for device_config {namespace}/{key}: "
                f"expected {expected}, got {actual}"
            )

    def verify_appop(self, package: str, op: str, expected_value: str) -> None:
        if self.client.dry_run:
            return
        result = self.client.shell(["cmd", "appops", "get", package, op], check=False)
        if result.returncode != 0:
            raise VerificationError(
                f"Verification failed for appop {op} for package {package}: "
                f"read command failed with exit code {result.returncode}"
            )
        output = result.stdout.strip()
        actual = self._parse_appop_output(output)
        expected = self._normalize_value(expected_value)
        if expected is not None:
            expected = expected.lower()

        if expected == "default":
            if actual not in {"default", "no operations.", "no overrides."}:
                raise VerificationError(
                    f"Verification failed for appop {op} for package {package}: "
                    f"expected default, got {actual}"
                )
            return

        if actual != expected:
            raise VerificationError(
                f"Verification failed for appop {op} for package {package}: "
                f"expected {expected_value}, got {actual}"
            )

    def _parse_appop_output(self, output: str) -> str:
        normalized_output = output.strip()
        if not normalized_output:
            raise VerificationError(f"Verification failed for appop: could not parse command output: {output}")

        if normalized_output in {"No operations.", "No overrides."}:
            return "default"

        for line in normalized_output.splitlines():
            candidate = line.strip()
            if not candidate:
                continue
            if candidate in {"No operations.", "No overrides."}:
                return "default"
            match = re.match(
                r"^(?:(?:[A-Z_a-z0-9]+)\s*:\s*)?(?:mode\s*[:=]\s*)?(?P<value>[A-Za-z0-9_]+)(?:\s*;.*)?$",
                candidate,
            )
            if match:
                return match.group("value").lower()

        raise VerificationError(
            f"Verification failed for appop: could not parse command output: {output}"
        )

    def verify_standby_bucket(self, package: str, expected_bucket: str) -> None:
        if self.client.dry_run:
            return
        result = self.client.shell(["am", "get-standby-bucket", package], check=False)
        if result.returncode != 0:
            raise VerificationError(
                f"Verification failed for standby bucket for package {package}: "
                f"read command failed with exit code {result.returncode}"
            )
        actual = result.stdout.strip()
        expected_code = STANDBY_BUCKET_MAP.get(expected_bucket.lower(), expected_bucket)
        if actual != expected_code:
            raise VerificationError(
                f"Verification failed for standby bucket for package {package}: "
                f"expected {expected_bucket} ({expected_code}), got {actual}"
            )

    def verify_package_enabled(self, package: str, expected_enabled: bool) -> None:
        if self.client.dry_run:
            return
        result = self.client.shell(
            ["pm", "list", "packages", "-e" if expected_enabled else "-d", package],
            check=False,
        )
        if result.returncode != 0:
            raise VerificationError(
                f"Verification failed for package {package} enabled state: "
                f"package-enabled verification readback failed with exit code {result.returncode}"
            )

        output = result.stdout.strip()
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
                    self._restore_appop_value(package, op, value)
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
