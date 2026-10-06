# Changelog

All notable changes to Waydroid Manager (waydroid-multi before 1.0) are listed here. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.0.0] - 2026-10-06

### Added
- **Pick the Android version of each device: 11, 13, 14, 15, 16 or 17**, every one with Google Play
  (`create --android 16`, or the version list in the New Instance dialog). A version is downloaded
  and checked the first time a device uses it, and shared by all devices on it. `images update` (or
  "Check for Android updates" in the menu) fetches newer builds, which devices switch to at their
  next start; `images list` shows what is installed. 11 and 13 are the official Waydroid builds;
  14, 16 and 17 are WayDroid-ATV's, and 15 is minhmc2007's. 15 and 17 are experimental. Android 12
  has no Waydroid build. Stock Waydroid's own images are reused when they are the same build.
- Android 14 and 15 get Google Play from MindTheGapps; Android 17's Google services are unpacked
  from the image so they run in a container.
- ARM apps on 14 to 17 run on the translation their image ships (Houdini on 14, libndk on 15 to 17);
  the `arm_translation` choice and Root (Magisk Delta) are for 11 and 13.
- **Full GPU speed on NVIDIA**, with NVIDIA's proprietary driver: Android 13 to 17 render on the
  NVIDIA card when it shows your desktop. Android's Vulkan goes to a renderer that runs as you, one
  per device (quinovax/waydroid-nvidia's build of virglrenderer with Venus, downloaded on first use);
  GLES runs on it through ANGLE. Several devices can share the card.
- **Graphics** setting ("Graphics" in Settings, `config <id> set gpu …`): Automatic (the GPU that
  shows your desktop when Android can use it, else software), Software, which renders on the CPU
  (slower, but it works on any PC), or one of your GPUs, listed as "GPU 0: NVIDIA GeForce RTX 5060
  Ti", "GPU 1: AMD Radeon Graphics"… (`doctor` lists them with the value to set). A GPU other than
  the one showing a desktop on NVIDIA works too: that GPU copies each frame into memory the desktop
  can show. Android 14, 15 and 17 render in software into a hidden virtual display device (the
  kernel's vkms module) that no desktop shows. `doctor` reports which applies. When a device
  starts on another renderer than last time, its apps' shader caches are cleared: Android 13's
  launcher crashed in a loop on caches another renderer had left.

### Changed
- **waydroid-multi is now Waydroid Manager.** The command is `waydroid-manager`, the package
  `waydroid-manager`, the app "Waydroid Manager", and the bridge `wdm0`. Installing it over
  waydroid-multi 0.5 stops the running instances once and moves them, with their images and network
  settings, to `/var/lib/waydroid-manager`. The `waydroid-multi` command is gone.
- The package is built for amd64 and ships its own libgbinder 1.1.53: Waydroid's 1.1.43 lacks the
  servicemanager protocols of Android 15 and newer. `doctor` checks for them. It also needs
  libepoxy and libgbm now (NVIDIA's renderer, and frames from another GPU).
- Devices run under their own AppArmor profile, `lxc-waydroid-manager`: stock Waydroid's, plus a
  rule that keeps Android from replaying the host's device events. The newer images did, at every
  boot, and that could log you out of GNOME.

### Fixed
- An app opened in its own window (`app launch`, app launchers) no longer stalls for a moment each
  time its window gains or loses focus: the desktop's focus change made Waydroid reconnect Android's
  display every time.
- `gsf-id` reads the ID from Google Services' database, so it works on Android 14 and newer too.

### Removed
- Support for upgrading from 0.4 and older: name-based instance ids from 0.1, the settings removed in
  0.4, and the stock-UI unit from 0.2 are no longer handled.

## [0.5.0] - 2026-10-05

### Added
- Running instances, #0 included, show up in `adb devices` by themselves as
  `waydroid-<name>:5555`, with no "Allow USB debugging?" prompt: like the emulator, your adb key
  is trusted in your own instances. The names live in a marked block in `/etc/hosts`, removed on
  uninstall.

### Changed
- Stock Waydroid has a single icon again. #0 no longer adds its own "Stock Waydroid (Waydroid)"
  entry: #0's icon is named **Waydroid**, and stock's own is hidden, so the one Waydroid icon opens
  #0. Uninstalling gives stock Waydroid its icon back.

## [0.4.1] - 2026-10-04

### Changed
- The instance window's Back, Home and Recents buttons sit at the bottom of the toolbar, like
  Android's navigation bar. The resize grip below them is gone: drag any edge to resize.

### Fixed
- Instance windows no longer open maximized. Waydroid maximizes its window before naming it,
  and that request reached the desktop. A phone-sized instance then showed its screen small in
  the middle of a wide window.

## [0.4.0] - 2026-10-04

### Added
- ARM translation per instance ("ARM translation" in Settings, `arm_translation` in the CLI):
  Houdini (default), libndk or off, so ARM-only apps install and run. Each build is downloaded
  once on first use and shared by all instances. Existing instances get Houdini at their next start.
- Drop `.apk` or `.xapk` files on an instance window to install them. XAPKs (an app split into
  several APKs) also install from the toolbar's and the manager's Install APK and `app install`.
- Saving settings of a running instance that need a restart asks to restart it now or later.

### Fixed
- The manager shows the disk space of your normal Waydroid (#0) also when it has not run
  since the daemon started: its data, in your home, is measured too.

### Removed
- The "Title bar and toolbar" switch: every instance window has them. The `window_frame` and
  `window_labels` settings and the `--no-frame` and `--no-window-labels` options of `create` are gone
  (a manager from an older version that still sends them is not refused).

## [0.3.0] - 2026-10-04

### Added
- Your normal Waydroid (#0) is now a full instance: window with title bar and toolbar, settings
  (resolution, device model, CPU and memory, root, writable system, …), Install APK, app grid entry,
  and every `waydroid-multi` command. It runs on stock Waydroid's own data, in place. Plain
  `waydroid` is paused while #0 runs and works as before once #0 stops or waydroid-multi is uninstalled.
- `cpuset all` leaves an instance unpinned (any host CPU, within its `cpus` limit).

### Fixed
- Automatic CPU pinning picks one thread per physical core before sharing a core, leaves CPU 0
  for last and skips CPUs isolated with `isolcpus=`. It weighs the other instances' pins by their
  `cpus` limit (a wide or no pin counts less per CPU), and ignores the old pins of instances that
  are starting or stopping. A malformed CPU pin in one instance no longer fails other instances'
  starts. When the host doesn't enable the cgroup cpuset controller, the daemon enables it, or
  logs a warning if it can't.
- Instance windows no longer show as "not responding" when the app that owns the clipboard is
  slow to hand over its text, or after copying text inside Android: the window proxy now
  fetches the clipboard for Android instead of letting Android's display service wait for it.

## [0.2.0] - 2026-10-04

### Added
- Per-instance `root` setting ("Root" switch): installs Magisk Delta (root for apps) into the instance.
- Per-instance `system_writable` setting ("Writable system" switch) to make the Android system partition writable.
- Install APK button in the instance window's side toolbar, and tooltips on its buttons.
- Every instance row has a checkbox; a Select all row at the top of the list starts, stops or
  deletes the checked instances. This replaces the separate selection mode and its bottom bar.
- Instance rows show the disk space the instance uses (in GB), instead of its RAM, IP and screen size.

### Changed
- The "Own dock icon and window title" switch is gone from the instance dialog (the `window_labels` setting remains in the CLI; turning on "Title bar and toolbar" turns it back on).
- Start all and Start (on checked instances) start them in parallel, like Stop all.

### Fixed
- Select all checked only the first instance; a second click was needed for the rest.
- Instances under load no longer show as "not responding": a CPU-limited instance without a
  `cpuset` is now pinned at start to as many host CPUs as its `cpus` limit, the ones running
  instances use least, instead of a quota spread over every host CPU that stalled the whole
  container, UI included.
- The `.deb`'s Installed-Size no longer depends on the filesystem it was built on.

## [0.1.0] - 2026-10-03

First release.

### Added
- Run several Waydroid instances at the same time, next to an unchanged stock Waydroid.
  Each instance has its own apps, accounts, settings, window, IP address and binder nodes.
- Create instances from the stock image, or clone stock Waydroid or another instance.
  Clones get a new device identity (Android ID, SSAID, GSF ID).
- Phone, tablet or custom resolutions; CPU and memory limits; device model presets
  (Samsung, Google Pixel, Xiaomi, OnePlus, ASUS ROG, or custom).
- Instance windows with a title bar and an LDPlayer-style toolbar: move, resize (the picture
  scales and the size is remembered), maximize, fullscreen (F11/Esc), Back/Home/Recents,
  volume and screenshots (saved to `~/Pictures/Waydroid/<instance name>/`).
- Closing a window asks before stopping the instance.
- GTK4/libadwaita manager app: create, clone, start, stop, settings, APK install, app grid
  launchers, batch actions, and uninstall.
- `waydroid-multi` command line for everything the app does, plus `adb`, `shell`, `logcat`,
  `doctor` and more.
- Automatic image sync after `waydroid upgrade`.
- `.deb` package for Ubuntu 24.04+ and Debian 13+.

### Security
- Root daemon hardened against symlink and race attacks, root-enabling Android properties,
  and misuse of passed file descriptors. Each user sees and controls only their own instances.

[Unreleased]: https://github.com/felipeoes/waydroid-multi-instances/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/felipeoes/waydroid-multi-instances/compare/v0.5.0...v1.0.0
[0.5.0]: https://github.com/felipeoes/waydroid-multi-instances/compare/v0.4.1...v0.5.0
[0.4.1]: https://github.com/felipeoes/waydroid-multi-instances/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/felipeoes/waydroid-multi-instances/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/felipeoes/waydroid-multi-instances/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/felipeoes/waydroid-multi-instances/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/felipeoes/waydroid-multi-instances/releases/tag/v0.1.0
