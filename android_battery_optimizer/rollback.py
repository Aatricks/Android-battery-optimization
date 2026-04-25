from typing import Dict, List, Optional, Callable, cast
from .adb import AdbClient, CommandError
from .state import StateStore
from .verification import VerificationError, verify_setting, verify_device_config, verify_appop, verify_standby_bucket, verify_package_enabled
from .ledger import AnyLedgerEntry

PACKAGE_USER_ID = "0"

def restore_appop_value(client: AdbClient, package: str, op: str, prior_value: Optional[str]) -> None:
    value = "default" if prior_value is None else str(prior_value)
    client.shell(["cmd", "appops", "set", package, op, value], mutate=True)

def perform_rollback(client: AdbClient, entry: AnyLedgerEntry) -> None:
    type_ = entry["type"]
    if type_ == "setting":
        namespace = str(entry["namespace"])
        key = str(entry["key"])
        prior_value = entry.get("prior_value")
        if prior_value is None:
            client.shell(["settings", "delete", namespace, key], mutate=True)
        else:
            client.shell(["settings", "put", namespace, key, prior_value], mutate=True)
    elif type_ == "device_config":
        namespace = str(entry["namespace"])
        key = str(entry["key"])
        prior_value = entry.get("prior_value")
        if prior_value is None:
            client.shell(["device_config", "delete", namespace, key], mutate=True)
        else:
            client.shell(["device_config", "put", namespace, key, prior_value], mutate=True)
    elif type_ == "appop":
        package = str(entry["package"])
        op = str(entry["op"])
        prior_value = entry.get("prior_value")
        restore_appop_value(client, package, op, cast(Optional[str], prior_value))
    elif type_ == "standby_bucket":
        package = str(entry["package"])
        prior_value = entry.get("prior_value")
        if prior_value:
            client.shell(["am", "set-standby-bucket", package, str(prior_value)], mutate=True)
    elif type_ == "package_enabled":
        package = str(entry["package"])
        prior_value = bool(entry.get("prior_value"))
        command = ["pm", "enable", "--user", PACKAGE_USER_ID, package]
        if not prior_value:
            command = ["pm", "disable-user", "--user", PACKAGE_USER_ID, package]
        client.shell(command, mutate=True)

def restore_and_verify(
    restore_action: Callable[[], object],
    verify_action: Callable[[], None],
) -> None:
    restore_action()
    verify_action()

def restore_state(
    client: AdbClient,
    store: StateStore,
    remove_snapshot_for_entry: Callable[[AnyLedgerEntry], None]
) -> List[str]:
    if client.serial is None and not client.dry_run:
        raise CommandError("Refusing to restore device state without a selected ADB serial.")

    current_metadata = client.get_device_metadata_with_fallback()
    saved_device = cast(Dict[str, object], store.data.get("device") or {})

    messages: List[str] = []

    if saved_device:
        saved_serial = saved_device.get("serial")
        if saved_serial is not None and client.serial != saved_serial:
            raise ValueError(
                f"Device serial mismatch: current={client.serial}, "
                f"saved={saved_serial}"
            )

        current_fp = current_metadata.get("fingerprint") or ""
        saved_fp = saved_device.get("fingerprint") or ""
        if saved_fp and current_fp:
            if current_fp != saved_fp:
                raise ValueError(
                    f"Device fingerprint mismatch: current={current_fp}, saved={saved_fp}"
                )
        elif saved_fp and not current_fp:
            warning = (
                "Warning: could not verify device fingerprint; proceeding with serial match only."
            )
            messages.append(warning)
            client.output(warning)

    had_failures = False
    settings = cast(Dict[str, Dict[str, object]], store.data.get("settings", {}))
    device_config = cast(Dict[str, Dict[str, object]], store.data.get("device_config", {}))
    packages = cast(Dict[str, Dict[str, object]], store.data.get("packages", {}))

    for item in list(settings.values()):
        namespace = cast(str, item["namespace"])
        key = cast(str, item["key"])
        value = cast(Optional[str], item["value"])
        try:
            if value is None:
                restore_and_verify(
                    lambda: client.shell(["settings", "delete", namespace, key], mutate=True),
                    lambda: verify_setting(client, namespace, key, None),
                )
            else:
                restore_and_verify(
                    lambda: client.shell(
                        ["settings", "put", namespace, key, value],
                        mutate=True,
                    ),
                    lambda: verify_setting(client, namespace, key, value),
                )
            messages.append(f"Restored setting {namespace}/{key}")
            if not client.dry_run:
                remove_snapshot_for_entry(cast(AnyLedgerEntry, {
                    "type": "setting",
                    "namespace": namespace,
                    "key": key,
                }))
        except (CommandError, VerificationError) as exc:
            had_failures = True
            msg = f"Failed to restore setting {namespace}/{key}: {exc}"
            messages.append(msg)
            client.output(msg)

    for item in list(device_config.values()):
        namespace = cast(str, item["namespace"])
        key = cast(str, item["key"])
        value = cast(Optional[str], item["value"])
        try:
            if value is None:
                restore_and_verify(
                    lambda: client.shell(
                        ["device_config", "delete", namespace, key],
                        mutate=True,
                    ),
                    lambda: verify_device_config(client, namespace, key, None),
                )
            else:
                restore_and_verify(
                    lambda: client.shell(
                        ["device_config", "put", namespace, key, value],
                        mutate=True,
                    ),
                    lambda: verify_device_config(client, namespace, key, value),
                )
            messages.append(f"Restored device_config {namespace}/{key}")
            if not client.dry_run:
                remove_snapshot_for_entry(cast(AnyLedgerEntry, {
                    "type": "device_config",
                    "namespace": namespace,
                    "key": key,
                }))
        except (CommandError, VerificationError) as exc:
            had_failures = True
            msg = f"Failed to restore device_config {namespace}/{key}: {exc}"
            messages.append(msg)
            client.output(msg)

    for package, item in list(packages.items()):
        appops = cast(Dict[str, Optional[str]], item.get("appops", {}))
        for op, value in list(appops.items()):
            try:
                restore_and_verify(
                    lambda: restore_appop_value(client, package, op, value),
                    lambda: verify_appop(client, package, op, str(value)),
                )
                messages.append(f"Restored {package} appop {op}")
                if not client.dry_run:
                    remove_snapshot_for_entry(cast(AnyLedgerEntry, {
                        "type": "appop",
                        "package": package,
                        "op": op,
                    }))
            except (CommandError, VerificationError) as exc:
                had_failures = True
                msg = f"Failed to restore {package} appop {op}: {exc}"
                messages.append(msg)
                client.output(msg)

        bucket = cast(Optional[str], item.get("standby_bucket"))
        if bucket is not None:
            try:
                restore_and_verify(
                    lambda: client.shell(
                        ["am", "set-standby-bucket", package, bucket],
                        mutate=True,
                    ),
                    lambda: verify_standby_bucket(client, package, str(bucket)),
                )
                messages.append(f"Restored {package} standby bucket")
                if not client.dry_run:
                    remove_snapshot_for_entry(cast(AnyLedgerEntry, {
                        "type": "standby_bucket",
                        "package": package,
                    }))
            except (CommandError, VerificationError) as exc:
                had_failures = True
                msg = f"Failed to restore {package} standby bucket: {exc}"
                messages.append(msg)
                client.output(msg)

        enabled = cast(Optional[bool], item.get("enabled"))
        if enabled is not None:
            try:
                command = ["pm", "enable", "--user", PACKAGE_USER_ID, package]
                if not enabled:
                    command = ["pm", "disable-user", "--user", PACKAGE_USER_ID, package]
                restore_and_verify(
                    lambda: client.shell(command, mutate=True),
                    lambda: verify_package_enabled(client, package, enabled),
                )
                messages.append(f"Restored {package} enabled state")
                if not client.dry_run:
                    remove_snapshot_for_entry(cast(AnyLedgerEntry, {
                        "type": "package_enabled",
                        "package": package,
                    }))
            except (CommandError, VerificationError) as exc:
                had_failures = True
                msg = f"Failed to restore {package} enabled state: {exc}"
                messages.append(msg)
                client.output(msg)

    if client.dry_run:
        return messages

    if had_failures:
        store.save_or_clear()
        client.output("Warning: Partial state corruption due to restore failures.")
    else:
        store.clear()
    return messages
