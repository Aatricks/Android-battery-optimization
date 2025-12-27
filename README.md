# -NO-ROOT-battery-optimization
Here's a repo including diverse adb shells to maximize battery life and sot on non-rooted android

---

## For starters

Download the latest version of the [android platform tools](https://developer.android.com/tools/releases/platform-tools), you'll need it to use adb commands.

Now go into your phone developpers settings, enable the USB debugging setting and plug your phone into your computer.

First off it is highly recommended to install the [Greenify](https://play.google.com/store/apps/details?id=com.oasisfeng.greenify) app  and set the apps to hibernate and enable the Aggressive doze and wake-up tracking
To set up those two settings you'll have to grant some permissions to greenify via the adb (Android Debug Bridge)

``adb -d shell pm grant com.oasisfeng.greenify android.permission.WRITE_SECURE_SETTINGS``

``adb -d shell pm grant com.oasisfeng.greenify android.permission.READ_LOGS``



## Android Settings

There are also a lot of battery and settings tweaks any user can do on its phone without using adb.

Here's a list of them (there are probably a couple ones I'm still missing)

#### Background Processes

Go into your phone's developper settings and select either no background processes or maximum one.

#### Background Check

Go into your phone's developper settings and in the Background check setting, uncheck every app. This will remove the hidden "running in background" permission.

#### Suspend Execution for Cached Apps

Go into your phone's developper settings and enable the suspend execution for cached apps setting. This will freeze the cached system services making them use less ressources when unused.

#### Don't keep Activities

Additionally you can enable the don't keep activities setting located in your phone's developper settings. Do note that will destroy every activity the moment you leave it, meaning you'll to have to disable it when doing things in between apps, like connecting to accounts using your phone's browser and authentificator app.

#### RAM+ 

If the setting exists, disable the RAM+ setting that is located in the device care tab. This setting permits your phone to use it's storage as extra RAM, this setting is useless for most users given, you need to be constantly at max usage of your phone's RAM for it to be useful. Disable it to save some battery.

#### Sleeping Apps (samsung only)

In your device care tab, go in battery -> background usage limits. First enable the "put unused apps to sleep", now go into the sleeping apps tab and add every app, then go into the deep sleeping apps tab and every app. Do put the apps you'd want notifications from into the sleeping apps and not in the deep sleeping apps and put the music apps like Spotify into the never-sleeping apps or sleeping apps.

#### Adaptive battery

Type Adaptive battery into your settings search bar and enable it. Do keep in mind that some user report that this setting actually deteriorates their battery so test it for a while and see for yourself, if it is worse than with it disabled go test this [optimizer](https://github.com/KelvinCrag/Optimizer), it will basically reset your phones usage data and so reset the usage data adaptive battery's relying on. If it's still worse, just disable it.

#### Cache Partition

If your battery usage reports a high SystemUI battery usage you should restart your phone into recovery mode and wipe the cache partition. Methods to enter recovery mode vary with phone brands so go check yours on google.

#### Restrict Battery Usage and Data Usage

Go into your apps settings and in the battery tab select restricted if you don't need it to send notifications else set it to optimized. Now go in the Mobile data tab and disable background usage for apps that don't need it. Note that you can do it for all apps, system apps included (you should be able to show every system package).
Also note that some apps will have their battery setting greyed out, those will need you to restrict them via adb, go check under, the guide on how to do that.

#### Permission controller

Type permission or permission controller in the settings search bar, and remove all unnecessary permissions some apps may have especially notifications (given those apps will have to run in background because of it), body sensors, nearby device scanning and locations (specifically the improved precision setting).

#### Digital well-being 

Disable it by removing the access to usage data.

#### Location

Go into you phone's location settings -> location services and disable Wi-Fi scanning, Bluetooth scanning, location sharing, location history and google location accuracy.

#### Data and Analytics

Type data, analytics, usage into your settings search bar and disable all settings related to data usage and analytics given those will just make your phone send out more data and so use more battery.

#### Auto Update

Type auto update, update into your settings search bar and disable all settings related to auto updating your phone, also disable the auto updates in your bloatware store and google play store.

#### Wi-Fi

Go into your phone's Wi-Fi settings -> three dots -> intelligent Wi-Fi, disable every thing but the Wi-Fi power saving mode.
Now go into your phone's developpers settings and enable the Wi-Fi scan throttling setting.

#### 5G

If your phone has it and you don't use it, disable the 5G Network band, Connections -> Mobile Networks -> Network mode and select 4G/3G/2G (auto connect).

#### Network Operator

In your connection settings go into your Mobile Networks settings and in the Network Operator tab, disable the select automatically setting.

#### Auto-Sync

Type sync in your settings search bar and disable the auto sync setting. Now go into android routines and create a routine to enable the auto-sync automatically at certain hours for 15 minutes xor when the phone is charging.

#### Sharing

In your connectivity settings go into the more connection settings and disable the printing service.
Type share in your settings search bar and disable the nearby sharing and group sharing (if you have it) setting.
Type scanning in your setttings search bar and disable the nearby device scanning setting. This will also stop you from receiving the annoying popup to connect to other people's headsets in the public transport.

#### Google

As you may know google's a major culprit for battery drain on android devices (especially the play store and play services), reduce it by going through all your settings in the google tab, and disable everything that looks useless, like analytics, device scanning, ads, ...

#### Display

Your phone's display is probably the main baterry drainer, reduce that drain by enabling the adaptive brightness setting. You can also enable the extra dim setting to reduce even more the brightness of your display.
Additionally you can also reduce your display resolution via phone's settings on certain phone or via adb for all the others, with the command :

``adb shell wm size <WIDTH>x<HEIGHT>``

``adb shell wm density <DPI>``

You should also preferrably use a pure black wallpaper if you have an OLED/AMOLED display and enable dark mode. Given on OLED displays pure black pixels are put to off so it's not draining any battery

If you have the option, you can also disable the adaptive/high refresh rate setting in the display tab if you want maximum battery savings. Though I'd recommend leaving it on or setting up an android routine to enable the setting automatically when lauching an app where you scroll a lot. You paid for the feature and it's too good of a feature to leave it off.

You can also check out [Galaxy max Hz](https://github.com/tribalfs/GalaxyMaxHzPub) to settle personalized refresh rates on phones that support adaptive refresh rate (samsung is officially supported).

#### Widgets

You should also preferrably don't use any widgets given they are massive battery drainers, even the simple weather widget.

#### Sensors

You can also disable some sensors like motion and the tactile panel of your screen when off, by disabling wake up the screen by double taping.

#### Routines

As said in the Display and auto-sync subcategory, you should set up an android routine to enable the high refresh rate setting automatically when lauching an app where you scroll a lot and a routine to enable the auto-sync automatically at certain hours for 15 minutes xor when the phone is charging.
You can also set routines to enable location and depending on your usage, wifi, 4g, bluetooth, nfc, only when launching apps that need them.
You should also always enable the Power Saving mode and set up a routine to disable power saving in the apps of your chosing.

### Launcher

Additionnaly you can change your default launcher to something like Nova launcher which is lighter and more efficient than most launchers or things like ap15 which barely takes any battery but isn't for everyone.

### Smart Watches

Don't put any restrictions on the watch plug-in app and preferably app, only the samsung wearables has been tested and works normally in deep sleep. Restricting the plug-in would mean not being able to connect to the watch.

On the other hand you can restrict and delete most permissions (like the sensors given they are only used if you use your phone as a step counter) of samsung health, if you don't want it to sync your watch steps in the background.




## Unified Optimizer (Recommended)

A new, comprehensive Python-based optimizer has been added to replace the fragmented batch files. It works on Linux, macOS, and Windows.

### Features
- **Dynamic Package Detection**: Automatically finds 3rd party apps (no manual list needed).
- **Whitelist Support**: Protect your favorite apps from background restrictions.
- **Categorized Optimizations**: Safe vs Aggressive modes.
- **Samsung Specifics**: Targeted fixes for Samsung device drains.
- **Battery Status**: Live view of your battery and top drainers.
- **Easy Revert**: One-click rollback of all ADB changes.

### Usage
Ensure you have Python installed and your device is connected via ADB.

```bash
python3 optimizer.py
# or on Linux
./optimize.sh
```

## Whitelist

You can exclude specific apps (like Spotify or WhatsApp) from being restricted by adding them to the whitelist in the optimizer menu. This ensures you still get notifications or background playback from apps you need while optimizing everything else.

### Revert

If you want to undo all changes made by the script, simply select the **Revert All Changes** option in the menu. This will reset Doze parameters, animations, and background restrictions to system defaults.





## Bloatware

If you want to maximise the results you can also debloat your phone by using the adb shell command to list all packages

``adb shell pm list packages`` 

then to disable a package

``adb shell pm disable-user --user 0 <PACKAGE_NAME>``

or to uninstall completely

``adb shell pm uninstall --user 0 <PACKAGE_NAME>``


if you want to reinstall a package simply type in

``adb shell cmd package install-existing <PACKAGE_NAME>``

You can also go check the [universal-android-debloater](https://github.com/0x192/universal-android-debloater) repo which is an app made to make debloating the easiest possible.




## Theoretical maximun

Install the Island app by the same dev as Greenify (oasisfeng) and follow the [installation guide](https://island.oasisfeng.com/)

Freeze every app you don't use, enable auto freeze by greenify and create an unfreeze and launch shortcut for every app. You can also freeze some unused/useless system processes.

(for samsung) freeze or disable device care, and in the adb force the power saving mode on (given we don't have it without device care)
by launching the power-saving.bat and settings.bat files.


### ⭐ If you came this far, thanks a lot. Consider starring this repo to help others find it !

> [!TIP]
> If you decide to root, using magisk you should still be able to use your phone as before including banking and payments apps except for knox-reliant ones like samsung-pay.
