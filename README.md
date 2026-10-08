# Waydroid Manager

Create and run **Android devices on Linux** with [Waydroid](https://waydro.id), the way
BlueStacks or LDPlayer do on Windows. Each device is a real Android system running natively
in a container, not an emulator.

- **Android 11, 13, 14, 15, 16 or 17**, chosen per device, all with Google Play
- **Full GPU speed, NVIDIA included**: 3D games run on your graphics card, even with NVIDIA's
  proprietary driver
- **As many devices as your PC can handle**, side by side, each with its own apps, accounts,
  settings, window and network address
- **Per device:** phone or tablet resolution, CPU and memory, the device model apps see
  (Samsung, Pixel, Xiaomi, …), ARM translation for ARM-only apps, and root
- **Clone** a device, your normal Waydroid included, to copy its apps and logins
- Your normal Waydroid keeps working, and shows up as device **#0**

![Three devices side by side, each a different phone: a Galaxy S24 Ultra, a Pixel 8 Pro and a Xiaomi 14](docs/screenshots/multi-instance.png)

## Android versions

| Android | Build | Google Play | NVIDIA | Download |
|---|---|---|---|---|
| 11 | official Waydroid (LineageOS 18.1) | in the image | – | 1.1 GB |
| 13 | official Waydroid (LineageOS 20) | in the image | ✓ | 1.4 GB, nothing when your Waydroid has the same build |
| 14 | [WayDroid-ATV](https://github.com/WayDroid-ATV) (LineageOS 21) | MindTheGapps | ✓ | 1.1 GB + 0.2 GB |
| 15 *(experimental)* | [minhmc2007](https://huggingface.co/datasets/Minhmc2077/My_Binary_Build) (LineageOS 22.2) | MindTheGapps | ✓ | 1.2 GB + 0.2 GB |
| 16 | WayDroid-ATV (LineageOS 23.2) | in the image | ✓ | 1.6 GB |
| 17 *(experimental)* | WayDroid-ATV (LineageOS 24.0, a pre-release) | in the image | ✓ | 1.7 GB |

A version is downloaded the first time a device uses it, and shared by all devices on it.
Android 15 needs a GPU: it can't render in software.

## Requirements

- An x86_64 PC running Linux with a Wayland desktop (GNOME, KDE Plasma, …)
- **Waydroid installed and set up**: you can already run `waydroid show-full-ui`
- **On NVIDIA:** NVIDIA's proprietary driver and **Linux 6.19 or newer** (Ubuntu 26.04's kernel;
  Ubuntu 24.04's and Debian 13's are older). Rendering 14 or 17 in software needs it too.
  Devices on other GPUs work on any of them.

`waydroid-manager doctor` checks your setup. Tested on Ubuntu 26.04 with Waydroid 1.6 and
GNOME 50, on an NVIDIA GeForce RTX 5060 Ti with an AMD Radeon iGPU.

## Install

**Ubuntu 24.04+ and Debian 13+:** set up Waydroid as
[its install guide](https://docs.waydro.id/usage/install-on-desktops) says; its package
repository has packages this one needs. Then download the `.deb` from the
[latest release](https://github.com/felipeoes/waydroid-manager/releases/latest) and install it.
Updates work the same way.

```sh
sudo apt install ./waydroid-manager_*_amd64.deb
```

**Other distributions,** from source:

```sh
git clone https://github.com/felipeoes/waydroid-manager.git
cd waydroid-manager
sudo scripts/install.sh
```

Then open **Waydroid Manager** from your app grid.

## Usage

![The manager: your normal Waydroid as #0 under Default, the other instances below with their Android version and disk use, some running](docs/screenshots/manager.png)

- **+ New Instance** creates a device. **▶** starts it, **■** stops it, **⟳** restarts it, and **⋮** has Settings,
  Clone, Install APK and Delete. Starting it opens the full Android UI with its status and
  navigation bars, including after an individual app launch.
- Each device's window has a toolbar: **⚙** Settings, **⟳** Restart, **◁ ○ □** Back / Home / Recents, volume,
  screenshot, Install APK and fullscreen (**F11**). Hover over a button to see its label.
  Drop `.apk` files on the window to install them.
- Restart immediately stops and starts the device, applies saved settings and reopens the full Android UI.
  Known display startup crashes in Android 11–16 are fixed automatically.
- The window turns with Android: a landscape game in a portrait device turns it to landscape, and
  it turns back when the game closes.
- Running devices show up in `adb devices` as `waydroid-<name>:5555`.
- **Root** (Magisk Delta): turn it on in Settings, then install the Magisk app with
  `waydroid-manager app install N /var/lib/waydroid-manager/magisk-delta.apk`. Root in Android is
  close to root on your PC: only use it on machines you trust.
- **A clone is a new device to Google:** register it once at
  <https://www.google.com/android/uncertified> with the number `waydroid-manager gsf-id N` prints.
- **#0 and plain `waydroid` share your Waydroid's data**, so only one of them runs at a time.

Everything also works from a terminal, with devices referred to by number or name:

```sh
waydroid-manager create --name Work --android 16
waydroid-manager start Work
waydroid-manager app install Work my-app.apk
waydroid-manager config Work set cpus 4 memory 6G
waydroid-manager --help
```

## Graphics

Each device's **Graphics** setting decides where Android draws:

| Choice | Where Android draws |
|---|---|
| **Automatic** (default) | on your NVIDIA card when it shows your desktop; otherwise on the GPU your normal Waydroid uses; in software when neither can |
| **Software** | on the CPU: slower, above all in 3D games, but it works on any PC |
| **GPU 0, GPU 1, …** | on that card, e.g. your integrated GPU while the desktop runs on NVIDIA |

On NVIDIA, RAID: Shadow Legends runs at 60 fps, against 9 in software. The first start there
downloads [waydroid-nvidia](https://github.com/quinovax/waydroid-nvidia)'s renderer (about 25 MB).
It's young (0.1): a game may hit a driver error.

## Uninstall

In the app's menu, choose **Uninstall Waydroid Manager…**, or:

```sh
sudo apt remove waydroid-manager    # keeps your devices; purge deletes them too
```

From source: `sudo /usr/lib/waydroid-manager/uninstall.sh` (add `--purge` to delete the devices).
Your normal Waydroid is never modified.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for how it works inside and how to develop it.

## License

[GPL-3.0](LICENSE)
