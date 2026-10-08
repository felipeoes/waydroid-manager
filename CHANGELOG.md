# Changelog

All notable changes to Waydroid Manager (waydroid-multi before 1.0) are listed here. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.2.1] - 2026-10-08

### Fixed
- Prevent display service crash loops during startup and restart in affected Android 11–16
  builds, including Android 13 with NVIDIA graphics.

## [1.2.0] - 2026-10-08

### Added
- Restart buttons in the manager and each instance's toolbar. They stop and start the instance,
  apply saved settings and reopen its window.

### Fixed
- Toolbar tooltips stay above Android's display, reappear after scrolling, and cancel when the
  pointer leaves before they open.

## [1.1.1] - 2026-10-07

### Fixed
- Starting or showing a stopped instance restores the full Android UI, including its status and
  navigation bars, even if an earlier individual app launch saved immersive mode.

## [1.1.0] - 2026-10-07

### Added
- The window turns with Android: an app that wants the other orientation (a landscape game in a
  portrait instance, or a portrait one in a landscape instance) turns the window instead of being
  letterboxed, and it turns back when the app closes. The instance's size never changes.

### Fixed
- Rotated clicks and touches use each HWC layer's dimensions when subsurface composition is enabled.
- Stock instance #0 uses its actual Android version for rotated mouse input.
- Stopping frozen #0 restores stock Waydroid's rotation setting before releasing its shared data.
- Resizing a window is easier: the invisible border around it is wider, every edge also grips a few
  pixels inside the window, and pulling out a single edge makes the window bigger (before, only the
  corners could). Edge drags use the grabbed edge's axis even when the compositor rounds or holds
  the other dimension fixed.
- The inside-edge resize grips stay above Android content created after the window frame.
- The manager's Retry button starts the daemon (it asks for your password) when it is down.
- Android 11's picture is centred, with black around it, when the window is maximized or fullscreen.
- The window no longer disappears when a toolbar tooltip shows after the toolbar was scrolled with a
  touchpad.

## [1.0.1] - 2026-10-06

### Fixed
- Android 14 to 17 no longer fill the dock and Files with a drive for each of their APEX loop
  devices.
- `adb reboot` (and any other reboot from inside Android) restarts the instance in its window
  instead of stopping it. Powering Android off still stops it.
- Android 11's picture follows the window's size instead of running past the toolbar.
- A window too short for the whole toolbar scrolls the toolbar's buttons with the mouse wheel; the
  Back, Home and Recents buttons stay at the bottom.

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
  the `arm_translation` choice is for 11 and 13.
- Root (Magisk Delta) works on every version. Android 16's init killed Magisk's daemon together with
  the boot step that starts it; that step now moves to init's own cgroup first. Devices that already
  have Root get the new start script at their next start.
- **Full GPU speed on NVIDIA**, with NVIDIA's proprietary driver: Android 13 to 17 render on the
  NVIDIA card when it shows your desktop. Android's Vulkan goes to a renderer that runs as you, one
  per device (quinovax/waydroid-nvidia's build of virglrenderer with Venus, downloaded on first use);
  GLES runs on it through ANGLE. Several devices can share the card.
- **Graphics** setting ("Graphics" in Settings, `config <id> set gpu …`): Automatic (the GPU that
  shows your desktop when Android can use it, else software), Software, which renders on the CPU
  (slower, but it works on any PC), or one of your GPUs, listed as "GPU 0: NVIDIA GeForce RTX 5060
  Ti", "GPU 1: AMD Radeon Graphics"… (`doctor` lists them with the value to set). A GPU other than
  the one showing a desktop on NVIDIA works too: that GPU copies each frame into memory the desktop
  can show. Android 14 and 17 render in software into a hidden virtual display device (the
  kernel's vkms module) that no desktop shows; it needs Linux 6.19 or newer, as rendering on NVIDIA
  does. Android 15 needs a GPU: its image can't show
  frames rendered on the CPU. `doctor` reports which applies. When a device
  starts on another renderer than last time, its apps' shader caches are cleared: Android 13's
  launcher crashed in a loop on caches another renderer had left.
- A **Settings** button (the gear) at the top of the instance window's toolbar opens that device's
  Settings in a window of their own, without the manager. Saving there asks to restart the device
  too, when a change needs it. After the first time it opens at once: one small background process
  keeps these windows ready while any device runs.

### Changed
- Settings has a sidebar of sections (General, Display, Device, Performance, Graphics, System and
  Properties), each on its own short page, instead of one long page to scroll.
- In the manager, the instance list scrolls under its header row (Select all, Start, Stop,
  Delete), which stays in view.
- The manager's state dot sits before the state in each row ("● Running"), so the rows' checkboxes
  line up with Select all at the left edge.
- **waydroid-multi is now Waydroid Manager.** The command is `waydroid-manager`, the package
  `waydroid-manager`, the app "Waydroid Manager", and the bridge `wdm0`. Installing it over
  waydroid-multi 0.5 stops the running instances once and moves them, with their images and network
  settings, to `/var/lib/waydroid-manager`. The `waydroid-multi` command is gone. The project
  lives at github.com/felipeoes/waydroid-manager; the old address redirects there.
- The package is built for amd64 and ships its own libgbinder 1.1.53: Waydroid's 1.1.43 lacks the
  servicemanager protocols of Android 15 and newer. `doctor` checks for them. It also needs
  libepoxy and libgbm now (NVIDIA's renderer, and frames from another GPU).
- Devices run under their own AppArmor profile, `lxc-waydroid-manager`: stock Waydroid's, plus a
  rule that keeps Android from replaying the host's device events. The newer images did, at every
  boot, and that could log you out of GNOME.

### Fixed
- The manager works on Ubuntu 24.04: since 0.1 it used widgets from newer libadwaita than 24.04's
  1.5, so its instance list stayed empty and Settings didn't open. Newer libadwaita still gets them.
- The close confirmation of an instance window shows the manager's icon in the dock, not a generic
  one: it reported its script name as its app id.
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

[Unreleased]: https://github.com/felipeoes/waydroid-manager/compare/v1.2.1...HEAD
[1.2.1]: https://github.com/felipeoes/waydroid-manager/compare/v1.2.0...v1.2.1
[1.2.0]: https://github.com/felipeoes/waydroid-manager/compare/v1.1.1...v1.2.0
[1.1.1]: https://github.com/felipeoes/waydroid-manager/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/felipeoes/waydroid-manager/compare/v1.0.1...v1.1.0
[1.0.1]: https://github.com/felipeoes/waydroid-manager/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/felipeoes/waydroid-manager/compare/v0.5.0...v1.0.0
[0.5.0]: https://github.com/felipeoes/waydroid-manager/compare/v0.4.1...v0.5.0
[0.4.1]: https://github.com/felipeoes/waydroid-manager/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/felipeoes/waydroid-manager/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/felipeoes/waydroid-manager/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/felipeoes/waydroid-manager/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/felipeoes/waydroid-manager/releases/tag/v0.1.0
