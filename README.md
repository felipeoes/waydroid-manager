# waydroid-multi

Run **several Android instances at the same time** with [Waydroid](https://waydro.id),
like BlueStacks or LDPlayer multi-instance, but on Linux.

Each instance is a separate Android device with its own apps, accounts, settings and
window. Your normal Waydroid keeps working exactly as before.

## What you can do

- Run as many Android instances as your computer can handle, side by side
- Create a fresh instance, or **clone** an existing one (including your normal Waydroid)
  to copy its apps and logins. Clones get their own device identity
- Choose each instance's **window size**, **screen density**, and how much **CPU and memory**
  it may use
- Find each instance in the dock and app grid under its own name
- Install APKs, launch apps, and connect with `adb`, per instance

## Requirements

- Linux with a Wayland desktop (GNOME, KDE Plasma, …)
- **Waydroid installed and set up**: you can already run `waydroid show-full-ui`
- Optional: `wl-clipboard` for clipboard sharing

Tested on Ubuntu with Waydroid 1.6 and GNOME.

## Install

```sh
git clone https://github.com/felipeoes/waydroid-multi-instances.git
cd waydroid-multi-instances
sudo scripts/install.sh
```

Then open **Waydroid Multi-Instance Manager** from your app grid.

## Using the manager app

- **+** creates a new instance.
- **▶** starts an instance, **👁** brings its window back, and **■** stops it.
- **⋮** opens a menu with Settings, Clone, Install APK, Add to/Remove from app grid, and Delete.
- Your normal Waydroid appears under **Default**. You can start, stop, or clone it from there.

Closing an instance's window stops that instance. You can change this in its Settings.

## Using the command line

Everything in the app can also be done from a terminal:

```sh
waydroid-multi create game1 --name "Account 1"   # new instance
waydroid-multi clone default game2               # copy your normal Waydroid
waydroid-multi start game1                       # start and open its window
waydroid-multi list                              # see all instances
waydroid-multi app install game1 my-app.apk      # install an app
waydroid-multi config game1 set width 1280 height 720
waydroid-multi stop game1
waydroid-multi delete game2
```

Run `waydroid-multi --help` to see all commands.

## Good to know

- **Moving windows:** Waydroid windows have no title bar on GNOME. Hold the **Super** (Windows)
  key and drag a window to move it.
- **Window size** is fixed while an instance runs. Change it in Settings; it applies at the
  next start.
- **Updating Waydroid:** run `waydroid upgrade` as usual. Instances pick up the new Android
  version the next time they start.
- **Google Play on a clone:** a clone counts as a new device, so register it once. Run
  `waydroid-multi gsf-id <instance>` and enter the number at
  <https://www.google.com/android/uncertified>.
- **Memory:** each running instance uses about 1.5–3 GB of RAM.
- Running several accounts may be against some apps' or games' terms of service.

## Uninstall

```sh
sudo scripts/uninstall.sh            # keeps your instances
sudo scripts/uninstall.sh --purge    # also deletes all instances and their data
```

Your normal Waydroid is never modified.

## License

[GPL-3.0](LICENSE)
