import shutil
import shlex
import subprocess
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence
from .android import DeviceInfo

@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str

class CommandError(RuntimeError):
    def __init__(self, message: str, result: Optional[CommandResult] = None) -> None:
        super().__init__(message)
        self.result = result

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
        serial = self.serial or "unknown-device"
        brand = self.shell_text(["getprop", "ro.product.brand"], check=True)
        model = self.shell_text(["getprop", "ro.product.model"], check=True)
        release = self.shell_text(["getprop", "ro.build.version.release"], check=True)
        sdk_str = self.shell_text(["getprop", "ro.build.version.sdk"], check=True)
        fingerprint = self.shell_text(["getprop", "ro.build.fingerprint"], check=True)

        try:
            int(sdk_str)
        except (ValueError, TypeError):
            sdk_str = "0"

        return {
            "serial": serial,
            "brand": brand,
            "model": model,
            "android_release": release,
            "sdk": sdk_str,
            "fingerprint": fingerprint,
        }

    def get_device_metadata_with_fallback(self) -> Dict[str, str]:
        try:
            return self.get_device_metadata()
        except CommandError:
            return self.get_minimal_device_metadata()

    def get_minimal_device_metadata(self) -> Dict[str, str]:
        return {
            "serial": self.serial or "unknown-device",
            "brand": "",
            "model": "",
            "android_release": "",
            "sdk": "",
            "fingerprint": "",
        }

    def supports_device_config(self) -> bool:
        try:
            result = self.shell(["device_config", "list"], check=False)
            return result.returncode == 0
        except Exception:
            return False

    def supports_appops(self) -> bool:
        try:
            result = self.shell(["cmd", "appops", "help"], check=False)
            return result.returncode == 0
        except Exception:
            return False

    def supports_standby_bucket(self) -> bool:
        try:
            info = self.get_device_info_struct()
            if info.sdk_int < 28:
                return False

            result = self.shell(["am", "get-standby-bucket", "android"], check=False)
            if result.returncode == 0 and result.stdout.strip().isdigit():
                return True

            help_result = self.shell(["am", "help"], check=False)
            help_output = help_result.stdout + help_result.stderr
            if "set-standby-bucket" in help_output and "get-standby-bucket" in help_output:
                return True

            return False
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

    def require_bound_device_for_mutation(self) -> None:
        if self.serial is None:
            raise CommandError("Refusing to mutate device state without a selected ADB serial.")

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
        if mutate and not self.dry_run:
            self.require_bound_device_for_mutation()

        command = self._base_command() + self._stringify(args)
        if mutate and self.dry_run:
            self.output(f"[dry-run] {self._format(command)}")
            if input_data:
                self.output(f"[dry-run-input]\n{input_data}")
            return CommandResult(returncode=0, stdout="", stderr="")

        if timeout is None:
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
        str_args = self._stringify(args)
        if timeout is None and str_args and str_args[0] == "adb":
            timeout = self.DEFAULT_TIMEOUT_SECONDS

        result = self.runner.run(
            str_args, input_data=input_data, timeout=timeout
        )
        if check and result.returncode != 0:
            details = result.stderr.strip() or result.stdout.strip() or "unknown error"
            raise CommandError(
                f"{self._format(str_args)} failed: {details}", result=result
            )
        return result.stdout.strip()
