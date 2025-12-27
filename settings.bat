adb shell settings put global cached_apps_freezer enabled
adb shell settings put global adaptive_battery_management_enabled 1
adb shell settings put system multicore_packet_scheduler 1

adb shell settings put global ble_scan_always_enabled 0
adb shell settings put system nearby_scanning_enabled 0
adb shell settings put system nearby_scanning_permission_allowed 0

adb shell settings put system master_motion 0
adb shell settings put system motion_engine 0
adb shell settings put system air_motion_engine 0
adb shell settings put system air_motion_wake_up 0

adb shell settings put system mcf_continuity 0
adb shell settings put system mcf_continuity_permission_denied 1
adb shell settings put system mcf_permission_denied 1

adb shell settings put system intelligent_sleep_mode 1
adb shell settings put secure adaptive_sleep 1

adb shell settings put global bixby_pregranted_permissions null

adb shell settings put system rakuten_denwa 0
adb shell settings put system remote_control 0
adb shell settings put system send_security_reports 0

adb shell settings put global sem_enhanced_cpu_responsiveness 1

adb shell settings put global restricted_device_performance 1,1

adb shell settings put global private_dns_specifier dns.adguard.com 

adb shell cmd power set-fixed-performance-mode-enabled true
adb shell cmd power set-mode 1

adb shell settings put global zram 0 default 
adb shell settings put global zram_enabled 0

adb shell settings put global default windows_antimation_scale 0.25 
adb shell settings put global default transition_animation_scale 0.25 
adb shell settings put global default animator_duration_scale 0.25 
adb shell settings put global ram_expand_size 0
