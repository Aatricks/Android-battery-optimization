# Android Battery Optimizer

Local ADB utility for experimenting with Android battery-related settings from a computer.

This project is now `safe by default`:
- The default optimization path only applies changes that are documented and reasonably reversible.
- Riskier changes are still available, but they are labeled `Experimental` and require confirmation.
- Every mutating action snapshots the prior state first, so `Revert Saved State` restores the original values instead of guessing defaults.

This is not a guarantee of better battery life. Android already includes its own battery-management systems, and some device-specific tweaks can reduce battery drain on one phone while breaking notifications, sync, companion devices, or app reliability on another.

## What Android Already Does

Android already ships with several battery-management features:
- [Doze](https://developer.android.com/training/monitoring-device-state/doze-standby) defers background CPU, network, jobs, syncs, and standard alarms when a device is idle.
- [App Standby](https://developer.android.com/training/monitoring-device-state/doze-standby) limits background activity for apps the user is not actively using.
- [App Standby Buckets](https://developer.android.com/topic/performance/appstandby) prioritize apps dynamically based on actual usage.
- [App hibernation](https://developer.android.com/topic/performance/app-hibernation) can reset permissions and stop background jobs or notifications for long-unused apps.
- Android documents [Developer Options](https://developer.android.com/studio/debug/dev-options) primarily as profiling and debugging tools, not as general end-user battery settings.

That is why this project no longer treats test-only commands like `dumpsys deviceidle force-idle` as normal “optimization” steps.

## What This Tool Changes Safely

`Apply Documented Safe Optimizations` currently enables AOSP’s abusive-app auto-restriction tracker:
- `device_config put activity_manager bg_auto_restrict_abusive_apps 1`
- `device_config put activity_manager bg_current_drain_auto_restrict_abusive_apps_enabled 1`

Reference: [AOSP app background trackers](https://source.android.com/docs/core/power/trackers)

This is intentionally narrow. The default path is supposed to be conservative, documented, and reversible.

## Experimental Features

The menu also includes experimental flows:
- `Apply Experimental Optimizations`
- `Apply Samsung Experimental Optimizations`
- `Restrict 3rd Party Apps (Experimental, with Whitelist)`
- `Run Background Optimization (Dexopt, Experimental)`

These can help on some devices, but they can also cause regressions:
- delayed or missing notifications
- broken background sync
- messaging apps becoming unreliable
- music playback interruption
- companion device or watch issues
- vendor-specific instability on Samsung devices

The Samsung path is deliberately narrower than older versions of this repo. Undocumented property-like toggles and non-reversible package-data wipes were removed from the default implementation.

## Requirements

- [Android Platform Tools](https://developer.android.com/tools/releases/platform-tools) installed and `adb` available in `PATH`
- USB debugging enabled on the device
- An authorized ADB connection
- Python 3

## Usage

```bash
python3 optimizer.py
```

Linux wrapper:

```bash
./optimize.sh
```

Optional flags:

```bash
python3 optimizer.py --serial SERIAL
python3 optimizer.py --dry-run
python3 optimizer.py --state-dir /path/to/state
```

## Data Location

Mutable files are no longer stored in the repo root.

By default, the script uses:

```text
~/.local/state/android-battery-optimizer/
```

Files written there:
- `whitelist.txt`
- `state.json`

`state.json` is the saved rollback snapshot. If a restore partially fails, the snapshot is kept so the restore can be retried.

## Whitelist Behavior

If you use `Restrict 3rd Party Apps`, apps in the whitelist are forced back toward a less restrictive state:
- `RUN_ANY_IN_BACKGROUND` is set to `allow`
- standby bucket is set to `active`

Use the whitelist for apps where you care about real-time background behavior, such as:
- messaging apps
- email apps
- music or podcast apps
- companion device apps
- smartwatch / wearable apps

## What Changed From Older Versions

Older versions of this repo mixed together:
- documented Android behavior
- anecdotal device advice
- test-only ADB commands
- vendor-specific settings with unclear support

This version removes or demotes several risky patterns from the default flow, including:
- disabling App Standby as an “optimization”
- setting `ro.config.low_ram` through Settings
- using `dumpsys deviceidle force-idle` as a persistent end-user tweak
- treating app hibernation test toggles as normal optimization settings
- guessing factory defaults during revert

## Notes On Manual Tweaks

Some common battery advice is highly device-specific or workload-specific. This repo no longer makes strong claims like:
- “set no background processes”
- “enable Don’t keep activities”
- “widgets are massive battery drainers”
- “disable updates broadly”

Those changes can help in narrow cases, but they can also create obvious usability regressions. If you try them, treat them as manual experiments, not universally good defaults.

## Verification Status

Observed in this repo:
- the optimizer now uses argv-based subprocess calls instead of `shell=True`
- mutating changes are snapshotted before being applied
- rollback restores saved prior values instead of hard-coded defaults
- mutable files moved out of the repo root

Blocked verification:
- no live Android device or local `adb` binary was available in the implementation environment, so end-to-end device behavior has not been validated here

## References

- [Optimize for Doze and App Standby](https://developer.android.com/training/monitoring-device-state/doze-standby)
- [App Standby Buckets](https://developer.android.com/topic/performance/appstandby)
- [App hibernation](https://developer.android.com/topic/performance/app-hibernation)
- [Configure on-device developer options](https://developer.android.com/studio/debug/dev-options)
- [AOSP app background trackers](https://source.android.com/docs/core/power/trackers)
- [Android dumpsys battery diagnostics](https://developer.android.com/tools/dumpsys)
