import subprocess
import sys
import os
import time

WHITELIST_FILE = "whitelist.txt"

def run_command(command):
    try:
        result = subprocess.run(command, shell=True, capture_output=True, text=True)
        return result.stdout.strip()
    except Exception as e:
        return f"Error: {str(e)}"

def adb_shell(command):
    return run_command(f"adb shell {command}")

def load_whitelist():
    if os.path.exists(WHITELIST_FILE):
        with open(WHITELIST_FILE, "r") as f:
            return [line.strip() for line in f if line.strip()]
    return []

def save_whitelist(whitelist):
    with open(WHITELIST_FILE, "w") as f:
        for pkg in whitelist:
            f.write(f"{pkg}\n")

def get_packages(third_party=True):
    cmd = "pm list packages"
    if third_party:
        cmd += " -3"
    output = adb_shell(cmd)
    if not output:
        return []
    return [line.split(":")[1] for line in output.splitlines() if ":" in line]

def get_device_info():
    brand = adb_shell("getprop ro.product.brand")
    model = adb_shell("getprop ro.product.model")
    android_ver = adb_shell("getprop ro.build.version.release")
    return f"{brand} {model} (Android {android_ver})"

def check_battery():
    print("\n--- Battery Status ---")
    output = adb_shell("dumpsys battery")
    print(output)
    
    print("\n--- Power Consumption Summary (Since Charged) ---")
    output = adb_shell("dumpsys batterystats --charged")
    found_summary = False
    for line in output.splitlines():
        if "Estimated power use" in line or "Capacity:" in line or "Computed drain:" in line:
            print(line.strip())
            found_summary = True
        elif found_summary and (" mAh" in line or ":" in line):
            if "Estimated power use" not in line and not line.startswith("  "):
                if " mAh" not in line:
                   found_summary = False
                   continue
            print(line.strip())

def optimize_doze(aggressive=False):
    print(f"Optimizing Doze Mode (Aggressive: {aggressive})...")
    settings = {
        "light_after_inactive_to": "0",
        "light_pre_idle_to": "30000",
        "light_idle_to": "15000",
        "light_idle_factor": "2",
        "light_max_idle_to": "60000",
        "inactive_to": "30000" if not aggressive else "15000",
        "sensing_to": "0",
        "locating_to": "0",
        "motion_inactive_to": "0",
        "idle_after_inactive_to": "0",
        "quick_doze_delay_to": "10000" if not aggressive else "5000"
    }
    
    for key, value in settings.items():
        adb_shell(f"device_config put device_idle {key} {value}")
    
    adb_shell("dumpsys deviceidle enable")
    adb_shell("dumpsys deviceidle force-idle")
    print("Doze Mode settings applied.")

def restrict_background_apps(level="ignore"):
    print(f"Setting background restriction to '{level}' for 3rd party apps...")
    packages = get_packages(third_party=True)
    whitelist = load_whitelist()
    for pkg in packages:
        if pkg in whitelist:
            print(f"  Skipping whitelisted app: {pkg}")
            adb_shell(f"cmd appops set {pkg} RUN_ANY_IN_BACKGROUND allow")
            adb_shell(f"am set-standby-bucket {pkg} active")
            continue
            
        adb_shell(f"cmd appops set {pkg} RUN_ANY_IN_BACKGROUND {level}")
        if level == "ignore":
            adb_shell(f"am set-standby-bucket {pkg} rare")
        else:
            adb_shell(f"am set-standby-bucket {pkg} active")
    print("App restrictions updated.")

def manage_whitelist():
    whitelist = load_whitelist()
    while True:
        print("\n--- Whitelist Management ---")
        print("Current Whitelist:")
        for i, pkg in enumerate(whitelist):
            print(f"  {i+1}. {pkg}")
        
        print("\n1. Add App to Whitelist")
        print("2. Remove App from Whitelist")
        print("3. Back")
        
        choice = input("Select an option: ")
        if choice == "1":
            pkg = input("Enter package name to add (or part of it to search): ")
            if "." not in pkg:
                all_pkgs = get_packages(third_party=True)
                matches = [p for p in all_pkgs if pkg in p]
                if not matches:
                    print("No matches found.")
                    continue
                for i, m in enumerate(matches):
                    print(f"  {i+1}. {m}")
                idx = input("Select number to add (or 0 to cancel): ")
                if idx.isdigit() and 0 < int(idx) <= len(matches):
                    pkg = matches[int(idx)-1]
                else:
                    continue
            
            if pkg not in whitelist:
                whitelist.append(pkg)
                save_whitelist(whitelist)
                print(f"Added {pkg} to whitelist.")
        elif choice == "2":
            idx = input("Enter number to remove: ")
            if idx.isdigit() and 0 < int(idx) <= len(whitelist):
                removed = whitelist.pop(int(idx)-1)
                save_whitelist(whitelist)
                print(f"Removed {removed} from whitelist.")
        elif choice == "3":
            break

def optimize_system_settings():
    print("Applying system-wide power-saving settings...")
    adb_shell("settings put global window_animation_scale 0.5")
    adb_shell("settings put global transition_animation_scale 0.5")
    adb_shell("settings put global animator_duration_scale 0.5")
    adb_shell("settings put global ble_scan_always_enabled 0")
    adb_shell("settings put system nearby_scanning_enabled 0")
    adb_shell("settings put global wifi_scan_throttle_enabled 1")
    adb_shell("settings put global mobile_data_always_on 0")
    adb_shell("settings put global wifi_power_save 1")
    adb_shell("settings put global cached_apps_freezer enabled")
    adb_shell("settings put global adaptive_battery_management_enabled 1")
    
    constants = "advertise_is_enabled=true,datasaver_disabled=false,enable_night_mode=true,launch_boost_disabled=true,vibration_disabled=true,animation_disabled=true,soundtrigger_disabled=true,fullbackup_deferred=true,keyvaluebackup_deferred=true,firewall_disabled=true,gps_mode=0,adjust_brightness_disabled=false,adjust_brightness_factor=2,force_all_apps_standby=true,force_background_check=true,optional_sensors_disabled=true,aod_disabled=false,quick_doze_enabled=true"
    adb_shell(f"settings put global battery_saver_constants \"{constants}\"")
    
    adb_shell("device_config put activity_manager bg_current_drain_auto_restrict_abusive_apps_enabled 1")
    adb_shell("device_config put app_hibernation app_hibernation_enabled 1")
    print("System settings applied.")

def optimize_samsung():
    brand = adb_shell("getprop ro.product.brand")
    if brand.lower() != "samsung":
        print("Device is not Samsung.")
        return
    print("Applying Samsung-specific optimizations...")
    samsung_settings = {
        "system": {
            "master_motion": "0", 
            "motion_engine": "0", 
            "mcf_continuity": "0",
            "adaptive_fast_charging": "1",
            "p_battery_charging_efficiency": "1"
        },
        "global": {
            "sem_enhanced_cpu_responsiveness": "0", 
            "ram_expand_size": "0",
            "cached_apps_freezer": "enabled",
            "restricted_device_performance": "1,1" # Limit CPU speed to 70%
        },
        "secure": {
            "vibration_on": "0",
            "refresh_rate_mode": "1", # Adaptive/High refresh rate (up to 120Hz)
            "min_refresh_rate": "10.0" # Allow it to drop low to save battery when idle
        }
    }
    for ns, kv in samsung_settings.items():
        for k, v in kv.items():
            adb_shell(f"settings put {ns} {k} {v}")
    
    adb_shell("pm disable-user --user 0 com.samsung.android.game.gos")
    adb_shell("pm disable-user --user 0 com.samsung.android.game.gamelab")
    print("Samsung optimizations applied (including GOS disable).")

def revert_all():
    print("Reverting all changes...")
    
    # 1. Revert Doze settings
    doze_keys = [
        "light_after_inactive_to", "light_pre_idle_to", "light_idle_to",
        "light_idle_factor", "light_max_idle_to", "inactive_to",
        "sensing_to", "locating_to", "motion_inactive_to",
        "idle_after_inactive_to", "quick_doze_delay_to"
    ]
    for key in doze_keys:
        adb_shell(f"device_config delete device_idle {key}")
    adb_shell("settings delete global device_idle_constants")
    adb_shell("dumpsys deviceidle unforce")
    adb_shell("dumpsys deviceidle enable")
    
    # 2. Revert Animation scales
    adb_shell("settings put global window_animation_scale 1.0")
    adb_shell("settings put global transition_animation_scale 1.0")
    adb_shell("settings put global animator_duration_scale 1.0")
    
    # 3. Revert Connectivity and System settings
    adb_shell("settings put global ble_scan_always_enabled 1")
    adb_shell("settings put system nearby_scanning_enabled 1")
    adb_shell("settings delete global wifi_scan_throttle_enabled")
    adb_shell("settings put global mobile_data_always_on 1")
    adb_shell("settings delete global wifi_power_save")
    adb_shell("settings put global cached_apps_freezer enabled")
    adb_shell("settings put global adaptive_battery_management_enabled 1")
    adb_shell("settings delete global battery_saver_constants")
    adb_shell("settings put global low_power 0")
    adb_shell("settings put global low_power_sticky 0")
    adb_shell("settings delete global low_power_trigger_level")
    adb_shell("cmd battery-saver set-front-restricted 0")
    adb_shell("cmd battery-saver set-back-restricted 0")
    adb_shell("settings put global app_standby_enabled 1")
    adb_shell("settings put global master_sync_status 1")
    adb_shell("settings put global default_restrict_background_data 0")
    adb_shell("settings put global zram_enabled 1")
    
    # 4. Revert Device Config optimizations
    adb_shell("device_config delete activity_manager bg_current_drain_auto_restrict_abusive_apps_enabled")
    adb_shell("device_config delete app_hibernation app_hibernation_enabled")
    
    # 5. Revert Background Restrictions for 3rd party apps
    restrict_background_apps(level="allow")
    
    # 6. Revert Samsung specific changes
    brand = adb_shell("getprop ro.product.brand")
    if brand.lower() == "samsung":
        print("Reverting Samsung-specific optimizations...")
        # Force turn off any Samsung power saving modes
        adb_shell("settings put system pwr_save_mode 0")
        adb_shell("settings put global pwr_save_mode 0")
        adb_shell("settings put global pwr_save_mode_on 0")
        adb_shell("settings put system psm_switch 0")
        adb_shell("settings put system psm_skipped_time 0")
        adb_shell("settings put system minimal_battery_use 0")
        adb_shell("settings put global sem_power_mode_limited_apps_and_home_screen 0")
        adb_shell("settings put system persist.sys_emc_mode performance")
        adb_shell("settings put system speed_mode 1")
        adb_shell("settings put system high_priority 1")
        adb_shell("settings put system low_priority 0")
        
        adb_shell("cmd power set-fixed-performance-mode-enabled false")
        adb_shell("cmd power set-mode 0")
        
        samsung_revert = {
            "system": {
                "master_motion": "1", 
                "motion_engine": "1", 
                "air_motion_engine": "1",
                "air_motion_wake_up": "1",
                "mcf_continuity": "1",
                "mcf_continuity_permission_denied": "0",
                "mcf_permission_denied": "0",
                "intelligent_sleep_mode": "1",
                "adaptive_fast_charging": "1",
                "p_battery_charging_efficiency": "0",
                "nearby_scanning_enabled": "1",
                "nearby_scanning_permission_allowed": "1",
                "remote_control": "1",
                "send_security_reports": "1"
            },
            "global": {
                "sem_enhanced_cpu_responsiveness": "1", 
                "ram_expand_size": "4096",
                "ram_expand_size_list": "2,4,6,8",
                "restricted_device_performance": "0,0",
                "enhanced_processing": "1",
                "app_restriction_enabled": "0",
                "automatic_power_save_mode": "1",
                "dynamic_power_savings_enabled": "1",
                "zram_enabled": "1"
            },
            "secure": {
                "vibration_on": "1",
                "refresh_rate_mode": "1",
                "adaptive_sleep": "1",
                "game_auto_temperature_control": "1"
            }
        }
        for ns, kv in samsung_revert.items():
            for k, v in kv.items():
                adb_shell(f"settings put {ns} {k} {v}")
        
        adb_shell("settings put system peak_refresh_rate 120.0")
        adb_shell("settings put secure user_refresh_rate 120.0")
        adb_shell("settings delete secure min_refresh_rate")
        adb_shell("pm enable --user 0 com.samsung.android.game.gos")
        adb_shell("pm enable --user 0 com.samsung.android.game.gamelab")
        
    print("Revert complete.")
        
    print("Revert complete.")

def main():
    if not run_command("adb devices").count("\tdevice"):
        print("No device connected via ADB.")
        return
    device = get_device_info()
    print(f"Connected to: {device}")
    while True:
        print("\n--- Android Battery Optimizer ---")
        print("1. Check Battery Status")
        print("2. Apply Safe Optimizations")
        print("3. Apply Aggressive Optimizations")
        print("4. Restrict 3rd Party Apps (with Whitelist)")
        print("5. Manage Whitelist")
        print("6. Samsung Specific Optimizations")
        print("7. Revert All Changes")
        print("8. Exit")
        choice = input("\nSelect an option: ")
        if choice == "1": check_battery()
        elif choice == "2":
            optimize_doze(aggressive=False)
            optimize_system_settings()
        elif choice == "3":
            optimize_doze(aggressive=True)
            optimize_system_settings()
            adb_shell("settings put global low_power 1")
        elif choice == "4": restrict_background_apps(level="ignore")
        elif choice == "5": manage_whitelist()
        elif choice == "6": optimize_samsung()
        elif choice == "7": revert_all()
        elif choice == "8": break

if __name__ == "__main__":
    main()