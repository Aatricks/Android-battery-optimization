import argparse
import os
import sys
from pathlib import Path
from typing import Callable, Optional, Sequence
from .adb import AdbClient, SubprocessRunner, CommandError
from .app import BatteryOptimizerApp
from .android import parse_adb_devices, resolve_package_choice
from .recorder import SnapshotError, VerificationError

APP_NAME = "android-battery-optimizer"
DEFAULT_STATE_DIR = (
    Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / APP_NAME
)

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
