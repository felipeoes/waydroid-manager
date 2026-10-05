# Changelog

All notable changes to waydroid-multi are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

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

[Unreleased]: https://github.com/felipeoes/waydroid-multi-instances/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/felipeoes/waydroid-multi-instances/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/felipeoes/waydroid-multi-instances/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/felipeoes/waydroid-multi-instances/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/felipeoes/waydroid-multi-instances/releases/tag/v0.1.0
