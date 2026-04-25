import re
from typing import Optional
from .adb import AdbClient
from .operations import STANDBY_BUCKET_MAP

PACKAGE_USER_ID = "0"

class VerificationError(RuntimeError):
    pass

def normalize_value(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    stripped = value.strip()
    if stripped in {"", "null", "None", "undefined"}:
        return None
    return stripped

def parse_appop_output(output: str) -> str:
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

def verify_setting(client: AdbClient, namespace: str, key: str, expected_value: Optional[str]) -> None:
    if client.dry_run:
        return
    result = client.shell(["settings", "get", namespace, key], check=False)
    if result.returncode != 0:
        raise VerificationError(
            f"Verification failed for setting {namespace}/{key}: "
            f"read command failed with exit code {result.returncode}"
        )
    actual = normalize_value(result.stdout)
    expected = normalize_value(expected_value)
    if actual != expected:
        raise VerificationError(
            f"Verification failed for setting {namespace}/{key}: "
            f"expected {expected}, got {actual}"
        )

def verify_device_config(client: AdbClient, namespace: str, key: str, expected_value: Optional[str]) -> None:
    if client.dry_run:
        return
    result = client.shell(["device_config", "get", namespace, key], check=False)
    if result.returncode != 0:
        raise VerificationError(
            f"Verification failed for device_config {namespace}/{key}: "
            f"read command failed with exit code {result.returncode}"
        )
    actual = normalize_value(result.stdout)
    expected = normalize_value(expected_value)
    if actual != expected:
        raise VerificationError(
            f"Verification failed for device_config {namespace}/{key}: "
            f"expected {expected}, got {actual}"
        )

def verify_appop(client: AdbClient, package: str, op: str, expected_value: str) -> None:
    if client.dry_run:
        return
    result = client.shell(["cmd", "appops", "get", package, op], check=False)
    if result.returncode != 0:
        raise VerificationError(
            f"Verification failed for appop {op} for package {package}: "
            f"read command failed with exit code {result.returncode}"
        )
    output = result.stdout.strip()
    actual = parse_appop_output(output)
    expected = normalize_value(expected_value)
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

def verify_standby_bucket(client: AdbClient, package: str, expected_bucket: str) -> None:
    if client.dry_run:
        return
    result = client.shell(["am", "get-standby-bucket", package], check=False)
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

def verify_package_enabled(client: AdbClient, package: str, expected_enabled: bool) -> None:
    if client.dry_run:
        return
    result = client.shell(
        [
            "pm",
            "list",
            "packages",
            "--user",
            PACKAGE_USER_ID,
            "-e" if expected_enabled else "-d",
            package,
        ],
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
