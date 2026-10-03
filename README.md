# waydroid-multi

Run **several Waydroid instances side by side** on one Linux host, BlueStacks/LDPlayer
style. Each instance has its own Android data, accounts, IP address and window. Your
existing (stock) Waydroid keeps working untouched as the `default` instance.

```
$ waydroid-multi list
ID       NAME            STATE    IP              SIZE         LIMITS
default  stock Waydroid  RUNNING  192.168.240.x   -            -
game1    Farm account 1  RUNNING  192.168.241.11  960x540      2 cpu, 4G mem
game2    Farm account 2  RUNNING  192.168.241.12  960x540      2 cpu, 4G mem
tests    Test device     STOPPED  192.168.241.13  540x960@240  -
```

It comes with a CLI and a GTK4/libadwaita manager app ("Waydroid Multi-Instance Manager").

> Status: early (0.1). Developed and tested on Ubuntu with Waydroid 1.6.2, LXC 6, GNOME 50.
> Upstream Waydroid has no multi-instance support (waydroid/waydroid#566); this is an
> independent add-on, not a fork.

## Features

- Any number of instances running at the same time, next to stock Waydroid
- **Create** a fresh Android from the stock image, or **clone** an instance or your stock
  Waydroid (apps, logins, data and settings). Clones get a **new device identity** by default
  (new Android ID / SSAID and Google Services Framework ID)
- **Per-instance window size and DPI**, plus **CPU and memory limits** (cgroup v2)
- Windows are **labelled per instance** ("Waydroid · Farm account 1") and get their own dock
  icon and app-grid launcher
- Closing an instance's window **stops** it (configurable: stop, freeze or keep running).
  Android idle-suspend **freezes** it (configurable)
- Fixed IP per instance on a separate bridge (`192.168.241.0/24` by default). Instances
  can't reach each other, and `waydroid-multi adb connect <id>` works
- Per-instance app install/launch/list, Android property overrides, root shell and logcat
- Clipboard sharing and desktop notifications labelled with the instance name

## How it works

Waydroid runs Android in an LXC container that talks to the host over binder.
waydroid-multi gives every instance its own copy of everything that is single-instance in
stock Waydroid:

| Stock Waydroid | Per waydroid-multi instance |
|---|---|
| binder nodes `anbox-binder` … | `binderfs/wdm<N>-binder` … in the same binderfs (binder contexts are per device node) |
| `/var/lib/waydroid`, `~/.local/share/waydroid/data` | `/var/lib/waydroid-multi/instances/<id>/{rootfs,overlay*,data,…}` |
| LXC container `waydroid` | `wdm-<id>` in `/var/lib/waydroid-multi/lxc` |
| bridge `waydroid0`, 192.168.240.0/24 | shared bridge `wdmulti0` with fixed leases `.10+N` and isolated ports |
| D-Bus `id.waydro.Container` / `id.waydro.Session` | one root daemon `io.github.waydroidmulti.Manager` plus one user session per instance |

- **Root daemon** (`waydroid-multi.service`) manages all instances: mounts, LXC, network,
  image store and cloning. Containers keep running across daemon restarts.
- **User session** (a transient `systemd --user` unit per instance) passes your Wayland and
  PulseAudio sockets to the daemon. It also hosts the instance's clipboard, notification and
  user-monitor binder services and runs a tiny **Wayland proxy**. The vendor hwcomposer
  hardcodes the window title and app_id to "Waydroid"; the proxy relabels them per instance
  and detects window close.
- **Image store**: instances run from copies of the stock `system.img`/`vendor.img` in
  `/var/lib/waydroid-multi/images`, because `waydroid upgrade` rewrites the stock images in
  place and would corrupt running instances. The daemon notices stock upgrades and copies the
  new images automatically once they have settled. Each instance switches at its next start,
  and image sets nothing uses any more are removed.
- Stock Waydroid's own helpers are reused at runtime (device-node list, prop generation,
  binder interface wrappers), so new hardware support upstream is picked up automatically.

See [docs/spike-findings.md](docs/spike-findings.md) for the verified design assumptions.

## Requirements

- Stock **Waydroid installed and initialised** (`waydroid init`). Tested with 1.6.x.
- A Wayland desktop session
- `lxc`, `dnsmasq`, `iproute2`, `python3-dbus`, `python3-gi`, `python3-gbinder`
  (all already pulled in by Waydroid on Debian/Ubuntu)
- For the GUI: GTK 4 and libadwaita ≥ 1.5 (`gir1.2-adw-1`)
- Optional: `wl-clipboard` (clipboard sharing), `adb`

## Install

```sh
git clone https://github.com/felipeoes/waydroid-multi-instances.git
cd waydroid-multi-instances
sudo scripts/install.sh            # installs to /usr/lib/waydroid-multi, enables waydroid-multi.service
waydroid-multi doctor              # checks the setup
```

## Usage

```sh
waydroid-multi create game1 --name "Farm account 1" --width 960 --height 540 --cpus 2 --memory 4G
waydroid-multi clone default game2           # copy your stock Waydroid (it is stopped first)
waydroid-multi clone game1 game3 --keep-ids  # exact copy incl. settings, same device identity
waydroid-multi start game1                   # opens "Waydroid · Farm account 1"
waydroid-multi start game2 --background --wait
waydroid-multi show game2                    # bring its window back
waydroid-multi app install game1 some.apk
waydroid-multi app launch game1 com.example.game
waydroid-multi config game1 set width 1280 height 720 close_action freeze
waydroid-multi config game1 prop ro.hardware.egl mesa   # per-instance Android property (next start)
waydroid-multi adb connect game1
waydroid-multi shell game1 -- getprop ro.build.version.release   # root (sudo)
waydroid-multi stop --all
waydroid-multi delete game3
waydroid-multi gsf-id game2                  # GSF ID to register at google.com/android/uncertified
waydroid-multi start default                 # stock Waydroid too (also recovers a wedged session)
```

### Manager app

Open **Waydroid Multi-Instance Manager** from the app grid (`waydroid-multi gui`). It
lists your instances with live state, IP, size and limits.
- **▶ / 👁** starts an instance or brings its window back; **■** stops it.
- **+** creates a fresh instance. The size presets fit your monitor.
- The **⋮** menu has *Settings* (display, CPU and memory, close and idle behaviour, Android
  properties), *Clone*, *Install APK*, *Add to/Remove from app grid* and *Delete*.
- Stock Waydroid appears as the **Default** row, where you can start, show, stop and clone it.

### Settings

| Key | Default | Meaning |
|---|---|---|
| `name` | id | display name (window title, launcher) |
| `width`, `height` | `0` (fill screen) | Android display / window size; keep it within your monitor |
| `dpi` | `0` (auto) | screen density |
| `cpus` | unlimited | CPU quota in cores (`cpu.max`) |
| `cpuset` | any | pin to host CPUs, e.g. `0-3` |
| `memory` | unlimited | soft limit (`memory.high`); hard limits make Android kill system_server |
| `close_action` | `stop` | closing the window: `stop`, `freeze` or `none` |
| `idle_action` | `freeze` | Android idle/screen-off suspend: `freeze`, `stop` or `none` |
| `window_labels` | `true` | per-instance window title, app_id and dock icon (Wayland proxy) |
| `desktop_apps` | `false` | app-grid launchers for the instance's own apps |

Stock Waydroid's `[properties]` (from `/var/lib/waydroid/waydroid.cfg`, e.g. a forced
`ro.hardware.egl`) apply to every instance; per-instance properties override them.

Network settings live in `/etc/waydroid-multi/daemon.conf`: bridge, subnet, isolation and nft.

## Things to know

- **Upgrading Waydroid**: just run `waydroid upgrade` as usual. The new images are picked up
  automatically, and each instance switches at its next start, which resets its `/system`
  overlay. `waydroid-multi images sync` forces a sync.
- **Cloning** stops the source first. Clones of an instance inherit its settings.
- **Polling stock Waydroid**: stock 1.6.x leaks two file descriptors per `waydroid status` call
  in its container service, and after ~500 calls it can no longer start or stop anything. Avoid
  polling `waydroid status` in scripts; waydroid-multi reads the stock state from cgroups instead.
- **GAPPS clones** get a new GSF ID, so register it once at
  <https://www.google.com/android/uncertified> before the Play Store works.
- **No title bar on GNOME**: Waydroid windows have no decorations on GNOME (no server-side
  decorations, and Waydroid draws none). This is the same as stock. Move them with
  **Super + drag**. The size is fixed per boot; change it with `width`/`height`.
- Running several instances takes RAM (≈1.5–3 GB each) and, with software rendering
  (swiftshader), CPU.
- Multi-account use may violate some apps' or games' terms of service.

## Uninstall

```sh
sudo scripts/uninstall.sh            # keeps instances and images in /var/lib/waydroid-multi
sudo scripts/uninstall.sh --purge    # deletes them too
```

Stock Waydroid is never modified.

## Development

```sh
python3 -m unittest discover -s tests/unit -t .          # unit tests (no root)
sudo install -m644 data/dbus/io.github.waydroidmulti.Manager.conf /etc/dbus-1/system.d/
sudo systemd-run --unit=waydroid-multi-dev -p KillMode=process --setenv=PYTHONPATH=$PWD --setenv=PYTHONDONTWRITEBYTECODE=1 \
     --setenv=WAYDROID_MULTI_DEBUG=1 python3 -m waydroid_multi.daemon.main
python3 -m waydroid_multi list                           # CLI from the checkout
tests/integration/smoke.sh                               # end-to-end test on a real host
```

Layout: `waydroid_multi/daemon` (root daemon), `session` (per-instance user session,
Wayland proxy), `gui` (GTK app), `cli.py`, `lxcconfig.py` (pure config generation),
`data/` (network script, LXC hooks, D-Bus/systemd files).

## License

GPL-3.0-or-later. Waydroid is GPL-3.0. waydroid-multi imports parts of the installed stock
Waydroid at runtime.
