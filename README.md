# waydroid-multi

Run **several Android instances at the same time** with [Waydroid](https://waydro.id),
like BlueStacks or LDPlayer multi-instance, but on Linux.

Each instance is a separate Android device with its own apps, accounts, settings and
window. Your normal Waydroid keeps working exactly as before.

![Three instances side by side, each one a different device: a Galaxy S24 Ultra, a Pixel 8 Pro and a Xiaomi 14](docs/screenshots/multi-instance.png)

## What you can do

- Run as many Android instances as your computer can handle, side by side
- Create a fresh instance, or **clone** an existing one (including your normal Waydroid)
  to copy its apps and logins. Clones get their own device identity
- Pick a **phone or tablet resolution**, the **CPU and memory** each instance may use, and a
  **device model** (Samsung, Pixel, Xiaomi, … or your own) that apps will see
- Every instance window has a **title bar and side toolbar**, LDPlayer style:
  - drag the title bar to move it, drag an edge to **resize** (the picture scales, and the size
    is remembered)
  - double-click the title bar to maximize
  - **◁ ○ □** Back / Home / Recents, volume buttons, screenshots, Install APK and fullscreen
  - drop `.apk` files on the window to install them
- Find each instance in the dock and app grid under its own name
- Install APKs, launch apps, and connect with `adb`, per instance
- Run **ARM-only apps**: each instance picks its ARM translation (Houdini, libndk or off)

## Requirements

- Linux with a Wayland desktop (GNOME, KDE Plasma, …)
- **Waydroid installed and set up**: you can already run `waydroid show-full-ui`
- Optional: `wl-clipboard` for clipboard sharing

Tested on Ubuntu 26.04 with Waydroid 1.6 and GNOME.

## Install

### Ubuntu 24.04+ and Debian 13+

Download `waydroid-multi_<version>_all.deb` from the
[latest release](https://github.com/felipeoes/waydroid-multi-instances/releases/latest), then:

```sh
sudo apt install ./waydroid-multi_*_all.deb
```

Updates work the same way: install the newer `.deb`. Running instances keep running.

### Other distributions (from source)

```sh
git clone https://github.com/felipeoes/waydroid-multi-instances.git
cd waydroid-multi-instances
sudo scripts/install.sh
```

Then open **Waydroid Multi-Instance Manager** from your app grid. If you installed from source
before, you can switch to the `.deb` at any time; your instances are kept.

## Using the manager app

![The manager: your normal Waydroid as #0 under Default, the other instances below with their disk use, some running](docs/screenshots/manager.png)

- **+ New Instance** creates an instance. Instances are numbered automatically: your normal
  Waydroid is **#0**, new ones get the next free number (#1, #2, …).
- **▶** starts an instance, **👁** brings its window back, and **■** stops it.
- **⋮** opens a menu with Settings, Clone, Install APK, Add to/Remove from app grid, and Delete.
- Tick instances' checkboxes (or **Select all**) to start, stop or delete several at once.
- Your normal Waydroid appears under **Default** as #0. It works like any other instance:
  window with title bar and toolbar, Settings, Install APK, app grid entry and Clone. It runs on
  your normal Waydroid's own apps and data, in place.

## The instance window

| | |
|---|---|
| Move | drag the title bar |
| Resize | drag any edge or the grip at the bottom of the toolbar |
| Maximize / restore | double-click the title bar |
| Fullscreen | toolbar button or **F11**; leave with **Esc** or **F11** |
| Android buttons | ◁ Back, ○ Home, □ Recents, volume up/down |
| Screenshot | toolbar camera button, saved to `~/Pictures/Waydroid/<instance name>` |
| Install APK | toolbar **APK** button, then pick an `.apk`; or drop `.apk` files on the window |

Closing the window stops the instance after asking you to confirm. In its Settings you can
make closing pause it or keep it running instead.

## Using the command line

Everything in the app can also be done from a terminal. Instances are referred to by their
number or their name:

```sh
waydroid-multi create --name "Account 1"           # new instance (prints its number, e.g. #1)
waydroid-multi clone default --name "Account 2"    # copy your normal Waydroid
waydroid-multi start 1                             # start and open its window
waydroid-multi list                                # see all instances
waydroid-multi app install 1 my-app.apk            # install an app
waydroid-multi config 1 set device_model pixel_7   # what apps see (see: waydroid-multi devices)
waydroid-multi config "Account 1" set cpus 4 memory 6G
waydroid-multi stop 1
waydroid-multi delete 2
```

Run `waydroid-multi --help` to see all commands.

## Good to know

- **Resolution, device model, CPU and memory** changes apply at the next start of the instance.
- **Writable system:** the per-instance "Writable system" switch (`config set N system_writable true`,
  then restart) lets Android change `/system` and `/vendor`, e.g. to install Magisk. Changes live in
  the instance's `overlay_rw/` and are lost when Waydroid's images are upgraded. Turning it off keeps
  earlier changes visible but read-only.
- **Root:** the per-instance "Root" switch (`config set N root true`, then restart) installs
  Magisk Delta, the build `waydroid_script` uses, so apps can get root. The first start downloads
  it, so it needs internet. Then install the Magisk app:
  `waydroid-multi app install N /var/lib/waydroid-multi/magisk-delta.apk`. Official Magisk does
  not work on Waydroid (no boot image). The switch is open to the instance owner, and root in
  Android is close to root on the host, so only enable it on machines you trust.
- **ARM translation:** instances run ARM-only apps through Houdini by default. In Settings, or with
  `config set N arm_translation libndk` (or `none`), pick libndk or turn it off, then restart. The
  first start with each one downloads it (the builds `waydroid_script` uses), so it needs internet;
  without internet the instance starts without it. Works on x86_64 with Android 11 or 13 images.
- **Updating Waydroid:** run `waydroid upgrade` as usual. Instances pick up the new Android
  version the next time they start.
- **Google Play on a clone:** a clone counts as a new device, so register it once. Run
  `waydroid-multi gsf-id <instance>` and enter the number at
  <https://www.google.com/android/uncertified>.
- **Memory:** each running instance uses about 1.5–3 GB of RAM.
- **Your normal Waydroid (#0):** #0 and plain `waydroid` use the same Android data, so only one
  of them runs at a time: starting #0 stops `waydroid` first, and while #0 runs, `waydroid` can't
  start. Stopping #0 (or uninstalling waydroid-multi) gives it back. #0's settings live in
  waydroid-multi only; plain `waydroid` keeps its own. Two things carry over, because they're stored
  in Android's data: with Root on, Magisk leaves `/data/adb` behind (harmless without it), and a
  different device model makes Google services see a different device when you switch between them.
- **Several users on one computer:** each user sees and controls only their own instances.
  Only the first user who opens waydroid-multi gets their normal Waydroid as #0.
- Running several accounts may be against some apps' or games' terms of service.

## Uninstall

In the manager app, open the menu and choose **Uninstall Waydroid Multi…**. You can keep your
instances or delete them with it. From a terminal:

```sh
sudo apt remove waydroid-multi       # keeps your instances
sudo apt purge waydroid-multi        # also deletes all instances and their data
```

If you installed from source:

```sh
sudo /usr/lib/waydroid-multi/uninstall.sh            # keeps your instances
sudo /usr/lib/waydroid-multi/uninstall.sh --purge    # also deletes all instances and their data
```

Your normal Waydroid is never modified.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for how it works inside and how to develop it.

## License

[GPL-3.0](LICENSE)
