adb shell device_config put device_idle light_after_inactive_to 0
adb shell device_config put device_idle light_pre_idle_to 15000
adb shell device_config put device_idle light_idle_to_initial_flex 0
adb shell device_config put device_idle light_idle_to 15000
adb shell device_config put device_idle light_idle_factor 2
adb shell device_config put device_idle light_max_idle_to 30000
adb shell device_config put device_idle flex_time_short 0
adb shell device_config put device_idle light_max_idle_to_flex 0
adb shell device_config put device_idle light_idle_maintenance_min_budget 15000
adb shell device_config put device_idle light_idle_maintenance_max_budget 30000
adb shell device_config put device_idle min_light_maintenance_time 5000
adb shell device_config put device_idle min_deep_maintenance_time 30000
adb shell device_config put device_idle inactive_to 10000
adb shell device_config put device_idle sensing_to 0
adb shell device_config put device_idle locating_to 0
adb shell device_config put device_idle motion_inactive_to 0
adb shell device_config put device_idle motion_inactive_to_flex 0
adb shell device_config put device_idle idle_after_inactive_to 0


adb shell device_config put device_idle idle_pending_to 30000
adb shell device_config put device_idle idle_pending_factor 2
adb shell device_config put device_idle max_idle_pending_to 60000


adb shell device_config put device_idle quick_doze_delay_to 10000


adb shell device_config put device_idle idle_to 7200000
adb shell device_config put device_idle idle_factor 2
adb shell device_config put device_idle max_idle_to 86400000


adb shell device_config put device_idle min_time_to_alarm 1800000


adb shell device_config put device_idle max_temp_app_allowlist_duration_ms 20000
adb shell device_config put device_idle wait_for_unlock true


adb shell device_config put device_idle mms_temp_app_allowlist_duration_ms 20000
adb shell device_config put device_idle notification_allowlist_duration_ms 20000

adb shell device_config put device_idle pre_idle_factor_long 0 
adb shell device_config put device_idle pre_idle_factor_short 0

adb shell dumpsys deviceidle enable
adb shell dumpsys deviceidle force-idle
pause
