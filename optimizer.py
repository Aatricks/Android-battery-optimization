import sys
from android_battery_optimizer.cli import main
from android_battery_optimizer.adb import (
    AdbClient,
    CommandError,
    CommandResult,
    SubprocessRunner,
)
from android_battery_optimizer.app import BatteryOptimizerApp
from android_battery_optimizer.cli import BatteryOptimizerCLI
from android_battery_optimizer.recorder import (
    StateRecorder,
    SnapshotError,
    VerificationError,
)
from android_battery_optimizer.state import StateStore
from android_battery_optimizer.android import parse_adb_devices, resolve_package_choice

if __name__ == "__main__":
    sys.exit(main())
