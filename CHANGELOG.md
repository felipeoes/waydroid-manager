# Changelog

All notable changes to waydroid-multi are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

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

[Unreleased]: https://github.com/felipeoes/waydroid-multi-instances/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/felipeoes/waydroid-multi-instances/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/felipeoes/waydroid-multi-instances/releases/tag/v0.1.0
