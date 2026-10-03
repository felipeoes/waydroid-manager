# waydroid-multi

Run **several Android instances at the same time** with [Waydroid](https://waydro.id),
like BlueStacks or LDPlayer multi-instance, but on Linux.

Each instance is a separate Android device with its own apps, accounts, settings and
window. Your normal Waydroid keeps working exactly as before.

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
  - **◁ ○ □** Back / Home / Recents, volume buttons, screenshots and fullscreen
- Find each instance in the dock and app grid under its own name
- Install APKs, launch apps, and connect with `adb`, per instance

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

- **+ New Instance** creates an instance. Instances are numbered automatically: your normal
  Waydroid is **#0**, new ones get the next free number (#1, #2, …).
- **▶** starts an instance, **👁** brings its window back, and **■** stops it.
- **⋮** opens a menu with Settings, Clone, Install APK, Add to/Remove from app grid, and Delete.
- The **☑** button switches to selection mode: start, stop or delete several instances at once.
- Your normal Waydroid appears under **Default**. You can start, stop, or clone it from there.

## The instance window

| | |
|---|---|
| Move | drag the title bar |
| Resize | drag any edge or the grip at the bottom of the toolbar |
| Maximize / restore | double-click the title bar |
| Fullscreen | toolbar button or **F11**; leave with **Esc** or **F11** |
| Android buttons | ◁ Back, ○ Home, □ Recents, volume up/down |
| Screenshot | toolbar camera button, saved to `~/Pictures/Waydroid/<instance name>` |

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
- **Updating Waydroid:** run `waydroid upgrade` as usual. Instances pick up the new Android
  version the next time they start.
- **Google Play on a clone:** a clone counts as a new device, so register it once. Run
  `waydroid-multi gsf-id <instance>` and enter the number at
  <https://www.google.com/android/uncertified>.
- **Memory:** each running instance uses about 1.5–3 GB of RAM.
- **Several users on one computer:** each user sees and controls only their own instances.
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
