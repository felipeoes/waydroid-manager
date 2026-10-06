# Contributing to Waydroid Manager

Thanks for helping! This guide explains how Waydroid Manager works inside, how to run it from a
checkout, and the hard-won lessons that are easy to break. For using the program, see the
[README](README.md).

Bug reports are most useful with: your distro, desktop, `waydroid --version`, the output of
`waydroid-manager doctor`, and the logs listed under [Debugging](#debugging).

**Quick links:** [Workflow](#workflow) · [Development](#development) ·
[Packaging](#packaging) · [Releasing](#releasing) · [Code style](docs/code-style.md)

## Workflow

| Branch | Purpose |
|---|---|
| `main` | **Always the latest release.** It only changes through a release PR from `dev`. |
| `dev` | Integration branch: the next release is assembled here. |
| `feature/…`, `fix/…` | One branch per change, made from `dev`. |

Both `main` and `dev` are protected: changes land only through pull requests (this applies to
the maintainer too), and force-pushes and deletion are blocked.

1. **Branch from `dev`:** `git switch dev && git pull && git switch -c fix/short-name`.
2. **Make the change**, following the [code style](docs/code-style.md). Add tests, update the
   README or this guide when behaviour changes, and add a line under `## [Unreleased]` in
   [CHANGELOG.md](CHANGELOG.md).
3. **Test:** run the unit tests, and also `tests/integration/smoke.sh` for daemon, container,
   network or cloning changes.
4. **Open a pull request against `dev`, not `main`.** GitHub proposes `main` (the default branch)
   for new PRs, so change the base. Fill in the template
   ([.github/pull_request_template.md](.github/pull_request_template.md)): what changed, why, and
   how you tested it.
5. **The PR is reviewed and merged into `dev`.** Small PRs are squash-merged.

Releases are cut from `dev` by the maintainer, see [Releasing](#releasing).

## How it works

Waydroid runs Android in an LXC container that talks to the host over binder. Stock Waydroid
can only run one of them. waydroid-manager gives every instance its own copy of everything that
is single-instance in stock Waydroid, and runs **next to** stock Waydroid without modifying it.

| Stock Waydroid | Per waydroid-manager instance *N* |
|---|---|
| binder nodes `anbox-binder` … | `binderfs/wdmN-{binder,vndbinder,hwbinder}` in the same binderfs (binder contexts are per device node) |
| `/var/lib/waydroid`, `~/.local/share/waydroid/data` | `/var/lib/waydroid-manager/instances/N/` (`rootfs`, `overlay*`, `data`, generated props) |
| LXC container `waydroid` | `wdm-N` with lxcpath `/var/lib/waydroid-manager/lxc` |
| bridge `waydroid0`, 192.168.240.0/24 | shared bridge `wdm0`, 192.168.241.0/24, fixed IP `.10+N`, isolated bridge ports |
| D-Bus `id.waydro.Container` / `id.waydro.Session` | root daemon `io.github.waydroidmanager.Manager` plus one user session per instance |

Instances are numbered: stock Waydroid is **#0** (`default`), and new instances take the lowest
free number from 1 to 240 (`registry.allocate_index`). The number is the instance id
everywhere: directory, container name, veth `wdmNv`, MAC `02:57:44:4d:HH:LL`, IP, binder node
names, launcher `waydroid-manager.N.desktop` and window app_id `waydroid-manager.N`. #0's launcher is
named plain "Waydroid", and stock's `Waydroid.desktop` is hidden for the user by a per-user
override (`NoDisplay=true`, marked `X-WaydroidManager=true`), so there is a single Waydroid icon.
Overriding stock's entry with #0's own doesn't work: GNOME Shell's app grid keeps launching the
entry it first loaded.

### Components

```
 GUI (gui/) ─┐                         ┌─ LXC containers wdm-N (Android)
 CLI (cli.py)├─ D-Bus (system bus) ──> root daemon (daemon/) ── mounts, binder nodes,
 session ────┘   io.github.waydroidmanager.Manager           network, image store, cgroups
   │
   └─ per-instance user session (session/main.py, transient user unit
      waydroid-manager-session-N.service)
        ├─ Wayland proxy (session/wlproxy.py): window title, frame, toolbar, zoom, clipboard
        ├─ NVIDIA mode: the device's renderer (virgl_test_server --venus)
        └─ binder services for Android: clipboard, notifications, user monitor
```

- **Root daemon** (`daemon/main.py`, `waydroid-manager.service`, `KillMode=process`) owns all
  privileged work. Every D-Bus call checks the caller's uid against the instance owner. Long
  operations run in worker threads under a per-instance lock. A watcher polls LXC state every
  3 s. Containers and the dnsmasq unit keep running across daemon restarts, and a restarted
  daemon adopts them.
- **Hardware helper** (`daemon/hwhelper.py`): python3-gbinder never releases the GIL, and its
  async `add_service` is broken. Each instance's `IHardware` service therefore runs in its own
  helper process using `add_service_sync`. It reports events to the daemon as `wdm:` lines.
- **User session** (`session/main.py`): started by `waydroid-manager start` as a transient
  `systemd --user` unit. It hands the user's Wayland and PulseAudio sockets to the daemon,
  which validates them and bind-mounts them at root-owned paths (`util.stage_socket`). It also
  hosts the per-instance clipboard, notification and user-monitor services.
- **Wayland proxy** (`session/wlproxy.py`, `wlproto.py`, `wlschema.py`, `frame.py`): sits
  between Android's hwcomposer (HWC) and the compositor. It:
  - labels windows ("Waydroid · Name", app_id `waydroid-manager.N`);
  - draws the title bar, side toolbar and resize border as its own subsurfaces;
  - scales the picture when the window is resized, and scales input back;
  - handles maximize, fullscreen, F11 and Esc;
  - synthesizes key presses for the toolbar;
  - takes file drags over the window away from the HWC and reports dropped APKs (`install <path>`)
    to the session, which installs them like the toolbar's Install APK.

  Details are in [Wayland proxy notes](#wayland-proxy-notes).
- **Android versions** (`catalog.py`): where each version's images come from (pinned OTA channels
  or zips) and what each image needs in a container: props, Google Play layer, loop devices,
  graphics quirks. [docs/spike-findings.md](docs/spike-findings.md) has the why of each.
- **Image store** (`daemon/images.py`): sets in `/var/lib/waydroid-manager/images/<id>/`.
  A device runs the newest installed set of its Android version. `install` downloads, verifies
  (read-only loop mount: ext4, squashfs or EROFS, and the SDK level) and unpacks a version the
  first time a device needs it; `update` fetches newer builds. #0 runs a copy of stock's own
  images, because `waydroid upgrade` rewrites them *in place*, which would corrupt a running
  device: the daemon notices new stock images, waits until they have been stable for 30 s and no
  `waydroid upgrade/init` runs, then copies them. Devices switch sets at their next start, and
  unused sets are garbage-collected.
- **Shared layers** (`daemon/layers.py`): pinned downloads unpacked once into read-only overlay
  layers that every device mounts below its own: ARM translation (`armtrans.py`), Google Play
  (`gapps.py`), NVIDIA's Android drivers (`nvidia.py`).
- **Graphics** (`gpu.py`): the `gpu` setting resolves to a mode at each start (`gpu.mode`):
  - `nvidia`: Android's Venus Vulkan driver talks over a unix socket to a renderer the session
    runs as the user (`daemon/nvidia.py`); ANGLE runs GLES on it.
  - `gpu`: a host GPU's render node in the container, Mesa in Android.
  - `software`: stock Waydroid's way (gralloc.default, SwiftShader).
  - `vkms`: CPU rendering for 14 and 17, which can't show gralloc.default buffers (15 can't
    render on the CPU at all, `catalog` `software=False`). Buffers
    come from a hidden vkms device (configfs `waydroid-manager`, its connector disconnected, udev
    tag `mutter-device-ignore`).

  The session asks `PrepareGpu` before `Start`. When the desktop can't import Android's buffers
  (vkms's, or another GPU's under GNOME on NVIDIA), the proxy shows them as `wl_shm`, see
  [Wayland proxy notes](#wayland-proxy-notes).
- **Stock code reuse** (`stock.py`): stateless helpers from stock Waydroid's `tools` package are
  imported at runtime. These are the device-node list, prop generation, binder probing and the
  binder interface wrappers. New hardware support upstream is therefore picked up automatically.
  Stock *stateful* code (its run helpers, D-Bus services, `waydroid status`) is never used; see
  [Stock Waydroid pitfalls](#stock-waydroid-pitfalls).

### Code layout

| Path | What |
|---|---|
| `waydroid_manager/instance.py` | instance config (`instance.cfg`), settings and validators, defaults |
| `waydroid_manager/catalog.py` | the Android versions: image sources, pins and per-image needs |
| `waydroid_manager/gpu.py` | graphics modes, the host's GPUs, the props of each mode |
| `waydroid_manager/devices.py` | device-model presets (`ro.product.waydroid.*`) |
| `waydroid_manager/lxcconfig.py`, `netconfig.py` | pure generation of LXC and network config (unit-tested) |
| `waydroid_manager/registry.py` | instance number allocation |
| `waydroid_manager/stock.py`, `stockctl.py` | stock Waydroid import shim; stock state without `waydroid status` |
| `waydroid_manager/client.py`, `cli.py` | D-Bus client and the `waydroid-manager` command |
| `waydroid_manager/daemon/` | root daemon: `main.py` (D-Bus API), `container.py`, `storage.py` (clone, identity reset, APK, screenshot), `images.py`, `layers.py`, `armtrans.py`, `gapps.py`, `nvidia.py`, `magisk.py`, `network.py`, `binder.py`, `util.py` (mounts, safe opens), `hwhelper.py` |
| `waydroid_manager/session/` | user session, Wayland proxy, frame drawing, desktop launchers; `gbm.py` reads another GPU's buffers |
| `waydroid_manager/session/protocols/` | vendored Wayland protocol XML (MIT, see its README) |
| `waydroid_manager/gui/` | GTK4/libadwaita manager app; `confirm.py` is the close confirmation |
| `data/` | network script, LXC hooks, D-Bus policy and activation, systemd unit, desktop file |
| `scripts/` | `install.sh` (the installed file layout), `uninstall.sh`, `spike/` (original feasibility scripts) |
| `packaging/deb/` | `.deb` build script, control template and maintainer scripts |
| `.github/workflows/release.yml` | builds and publishes a release when a version tag is pushed |
| `.github/pull_request_template.md` | what a PR describes and checks |
| `tests/unit/`, `tests/integration/smoke.sh` | unit tests; end-to-end test on a real host |
| `docs/spike-findings.md` | verified design assumptions from the feasibility spike |

### Files on the system

| Path | Contents |
|---|---|
| `/usr/lib/waydroid-manager/` | the program, `data/`, `lib/libgbinder.so.1` and its license, `uninstall.sh` and `install-method` (`script` or `deb`) |
| `/usr/lib/udev/rules.d/70-waydroid-manager.rules` | keeps GNOME off our vkms device |
| `/usr/lib/systemd/system/waydroid-manager.service` | the daemon's unit |
| `/etc/waydroid-manager/daemon.conf` | bridge name, subnet, instance isolation, nftables |
| `/var/lib/waydroid-manager/instances/N/` | `instance.cfg`, `data/` (Android `/data`), overlays, generated props, `container.log` |
| `/var/lib/waydroid-manager/lxc/wdm-N/` | generated LXC config |
| `/var/lib/waydroid-manager/images/` | image store; `current` is the copy of stock's set |
| `/var/lib/waydroid-manager/{arm,gapps,nvidia}/` | shared read-only layers and NVIDIA's host renderer |
| `/run/waydroid-manager/` | network env, dnsmasq hosts file, staged sockets, the AppArmor profile |
| `/sys/kernel/config/vkms/waydroid-manager/` | the hidden vkms device (made on first software start) |
| `~/.local/share/applications/waydroid-manager.*` | per-user launchers |
| `~/.local/share/applications/Waydroid.desktop` | hides stock's Waydroid icon (#0's is the Waydroid icon) |
| `~/.cache/waydroid-manager/` | proxy and renderer logs (`wlproxy-N.log`, `renderer-N.log`) |

## D-Bus API

Bus name `io.github.waydroidmanager.Manager` on the **system** bus, object
`/io/github/waydroidmanager/Manager`, interface `io.github.waydroidmanager.Manager1`. Instance ids
are strings (`"1"` … `"240"`). Callers only see and control their own instances; root sees all.

| Method | Signature | Notes |
|---|---|---|
| `GetInfo` | `→ a{ss}` | versions, subnet, bridge, image ids, warnings |
| `List` | `→ aa{ss}` | the caller's instances with state, IP, memory use |
| `Get` | `s → a{ss}` | one instance |
| `Create` | `a{ss} → s` | settings (`android` among them), `prop:<key>`, `clone_from` (`default` or an id), `reset_ids`; returns the new id |
| `Delete`, `Stop`, `Freeze`, `Unfreeze` | `s` | |
| `SetConfig` | `sa{ss}` | settings and `prop:<key>` (empty value removes a prop) |
| `Start` | `sa{ss}` | called by the user session with its sockets and pid |
| `ReportClose` | `s` | window closed; the daemon applies `close_action` |
| `InstallApk` | `shs → s` | fd of a regular file opened for reading, and its name (`.xapk` or `.apk`); returns pm's output |
| `AuthorizeAdbKey` | `ss` | the caller's `adbkey.pub`, added to the running instance's `/data/misc/adb/adb_keys` |
| `SendKey` | `su` | evdev code, written into Android's keyboard FIFO (Recents = 580) |
| `Screenshot` | `sh → t` | fd of a regular file opened for writing; returns the size |
| `GetGsfId` | `s → s` | Google Services Framework id (for Play registration) |
| `PrepareGpu` | `s → a{ss}` | called by the session before `Start`: the mode, NVIDIA's renderer directory, how the proxy shows buffers (`cpu_buffers`) |
| `UpdateImages` | `→ s` | fetch newer builds of the devices' versions; returns those updated |
| `Images` | `→ aa{ss}` | the image sets, with the devices using them |

Signals: `StateChanged(ss)`, `InstanceAdded(s)`, `InstanceRemoved(s)`, `ConfigChanged(s)`.

## Settings reference

Stored in `instance.cfg` and changed with `waydroid-manager config N set KEY VALUE` or the GUI.
`SETTINGS` in `instance.py` is the source of truth.

| Key | Default | Meaning |
|---|---|---|
| `name` | `Instance N` | display name |
| `width`, `height`, `dpi` | 1280, 720, 240 | Android screen (restart) |
| `cpus` | 2 (or fewer cores on the host) | CPU limit, cgroup `cpu.max` (restart) |
| `cpuset` | as many CPUs as `cpus`, the least used by running instances at start | pin to host CPUs, e.g. `0-3` (restart). A quota alone lets Android stall on all host CPUs at once |
| `memory` | 4G, or 2G on hosts with ≤ 6 GB | cgroup `memory.high` (restart) |
| `device_model` | `waydroid` | preset from `waydroid-manager devices`, or `custom` (restart) |
| `zoom` | `auto` | window scale in %, saved when the window is resized |
| `close_action` | `stop` | closing the window: `stop` (asks first), `freeze` or `none` |
| `idle_action` | `freeze` | Android idle-suspend: `freeze`, `stop` or `none` |
| `root` | false | install Magisk Delta into the instance (`daemon/magisk.py`, restart) |
| `system_writable` | false | mount system/vendor overlays writable (restart) |
| `arm_translation` | `houdini` | `houdini`, `libndk` or `none`: a shared read-only system layer (`daemon/armtrans.py`) plus native bridge props (restart). 14 to 17 use their image's own; `none` turns it off |
| `android` | `13` | Android version (`catalog.VERSIONS`), set at creation only |
| `gpu` | `auto` | `auto`, `software` or a GPU's PCI address (`gpu.mode`, restart) |
| `desktop_apps` | false | launchers for the instance's apps |

Android properties are set with `prop:<key>`. They go into the instance's `vendor/waydroid.prop`,
which Android reads last.

## Security model

- **Who can do what.** Any local user may create instances, with a limit of 64 per user.
  Instances belong to the uid that created them, and every D-Bus call checks it. There is no
  polkit prompt by design. Instance directories are `0710 root:<owner group>`. That matters
  because files inside `data/` belong to raw Android uids, which can coincide with other host
  users' uids.
- **Android root is close to host root.** Instances are privileged LXC containers, as with stock
  Waydroid. The per-instance `root` switch is deliberately open to the instance owner (like
  LDPlayer's), so an owner can get root inside Android and, through it, close to host root.
  Keep that in mind when sharing a machine. The switch puts Magisk Delta (the build
  `waydroid_script` uses, pinned by commit and sha256 in `daemon/magisk.py`) into the
  instance's own overlay layer. Magisk then decides which apps get root. Other props that would give root
  (`ro.debuggable`, `ro.secure`, `ro.adb.*`, `service.adb.*`, `ro.boot.*`, …; `PROTECTED_PROP_RE`
  in `instance.py`) still can only be set by root and are filtered when props are written.
- **ARM translation runs third-party binaries in every instance by default.** The Houdini and
  libndk builds are the ones `waydroid_script` uses, pinned by commit and sha256 in
  `daemon/armtrans.py`, and unpacked once into a root-owned layer under
  `/var/lib/waydroid-manager/arm/` that all instances share read-only. Their init scripts register
  binfmt_misc handlers, which are host-wide (the containers are privileged) and stay until reboot.
- **NVIDIA mode runs third-party binaries too:** waydroid-nvidia's renderer runs as the user, and
  its Android drivers sit in a shared layer; both are pinned by sha256 in `daemon/nvidia.py`. The
  renderer's socket directory is the user's, so the daemon opens it like the session's sockets
  (owner checked, `O_NOFOLLOW`, `util.stage_dir`) and binds it read-only at `/dev/venus`.
- **Android 14 to 17 get the host's loop devices** (`/dev/loop-control` and the loop nodes; apexd
  mounts APEXes through them). device-mapper (`c 10:236`) is denied: apexd made host-wide dm
  devices that outlived the device.
- **AppArmor:** containers run under `lxc-waydroid-manager`, stock's `lxc-waydroid` profile plus
  `deny /**/uevent w`: the newer images' ueventd remounts sysfs and replays every host device
  event, which made GNOME re-probe its GPUs and could end the session. lxc-start only switches to
  `lxc-*` profiles. Each start regenerates it from stock's and loads it
  (`container.load_apparmor_profile`).
- **Instance names are visible to every local user:** the daemon keeps a marked block in the
  world-readable `/etc/hosts` (`netconfig.etc_hosts_text`) with one `waydroid-<name>` per instance,
  so adb can show instances by name. The names are reduced to `[a-z0-9-]`, so a display name can't
  inject lines. The block is rewritten in place, and only lines inside it are touched. The session
  runs `adb connect` once Android is ready (again after a rename) and `adb disconnect` at stop.
  Before connecting, it has the daemon add the user's `adbkey.pub` to the instance's `adb_keys`
  (`AuthorizeAdbKey`, owner only, key checked by `validate_adb_key`). The daemon writes the file
  from the host with `open_in_container`, so a link planted inside isn't followed. That gives the
  owner adb, never adb root (`ro.debuggable` stays protected).
- **Everything below a user's home, and everything inside a container, is hostile.**
  - `storage.open_stock_data` opens `~/.local/share/waydroid/data` one step at a time with
    `O_NOFOLLOW`. Clone from stock copies through `/proc/<pid>/fd/N`, and #0 bind-mounts it onto
    the root-owned `instances/0/data`, so LXC never resolves a user path.
  - Files inside a running container are opened with `util.open_in_container`, which never
    follows symlinks. Without it, an absolute link planted inside the container would resolve
    on the host.
  - Sockets from the session are opened with `O_PATH|O_NOFOLLOW` and their owner is checked.
  - Passed fds must be regular files with the right access mode.
  - Files in a device's data are deleted through directory fds opened with `O_NOFOLLOW`
    (`storage.clear_shader_caches`).
- **Untrusted values never reach a shell or a config line.** Validators reject newlines, `%`
  (configparser interpolation is disabled everywhere anyway) and whitespace in LXC values.
- `rm -rf --one-file-system` does **not** stop at bind mounts from the same filesystem. Anything
  that deletes trees (instance delete, `uninstall.sh --purge`) must make sure nothing is
  mounted below first.

Please report security problems privately via GitHub's "Report a vulnerability" on the
repository rather than in a public issue.

## Development

Requirements: a host with Waydroid set up, plus `lxc`, `dnsmasq`, `python3-dbus`,
`python3-gi` (GTK 4, libadwaita ≥ 1.5), `python3-gbinder` and `python3-cairo`. The window frame
also uses librsvg and PangoCairo through GObject introspection, for icons and text.

```sh
python3 -m unittest discover -s tests/unit -t .     # unit tests (no root, no Waydroid needed)
sudo scripts/install.sh                             # install and restart the daemon
tests/integration/smoke.sh [some.apk]               # end-to-end test on a real host
```

`smoke.sh` creates instances named "Smoke …", checks booting, networking, isolation,
limits, device model, cloning with identity reset, daemon restart and number reuse, and
deletes them again. It needs sudo for its shell checks and leaves stock Waydroid untouched.

Running the daemon from the checkout instead of installing it:

```sh
sudo systemctl stop waydroid-manager
sudo systemd-run --unit=waydroid-manager-dev -p KillMode=process \
     --setenv=PYTHONPATH=$PWD --setenv=PYTHONDONTWRITEBYTECODE=1 --setenv=WAYDROID_MANAGER_DEBUG=1 \
     python3 -m waydroid_manager.daemon.main
PYTHONPATH=$PWD python3 -m waydroid_manager list      # CLI from the checkout
PYTHONPATH=$PWD python3 -m waydroid_manager gui
```

Remember that the **session and proxy** of an instance run the code that was installed when the
instance started. After changing `session/`, reinstall and restart the instance.

### Debugging

| What | Where |
|---|---|
| daemon | `journalctl -u waydroid-manager -f` |
| an instance's session | `journalctl --user -u waydroid-manager-session-N -f` |
| Wayland proxy | `~/.cache/waydroid-manager/wlproxy-N.log`; `waydroid-manager log N --window` dumps its live state (sends SIGUSR1) |
| NVIDIA renderer | `~/.cache/waydroid-manager/renderer-N.log` |
| proxy wire trace | start the instance with `WDM_PROXY_TRACE=1` |
| container | `/var/lib/waydroid-manager/instances/N/container.log` |
| Android | `waydroid-manager logcat N`, `waydroid-manager shell N` |
| HWC crash | `/data/waydroid_hwc_wayland_error.txt` inside the instance |
| setup problems | `waydroid-manager doctor` |

### Style

See [docs/code-style.md](docs/code-style.md): Python conventions, rules for the root daemon,
the Wayland proxy, the GUI and shell scripts, tests, and commits.

## Packaging

The installed file layout has a single source: `scripts/install.sh`. It installs and starts the
daemon for a source install. With `--destdir DIR --no-activate` it only stages the files, and
that staged tree is what the package is built from. A new installed file therefore goes into
`install.sh` only.

```sh
packaging/deb/build.sh                     # → dist/waydroid-manager_<version>_amd64.deb (no root needed)
sudo apt install ./dist/waydroid-manager_*_amd64.deb
```

- **Version:** taken from `__version__` in `waydroid_manager/__init__.py`.
- **libgbinder:** `packaging/build-libgbinder.sh` builds the pinned tag (and commit) into
  `build/lib/`; `build.sh` runs it when needed and `install.sh` ships the result. Bump both pins together.
- **Dependencies:** listed in `packaging/deb/control.in`. Keep them in sync with the code's
  imports. libadwaita ≥ 1.5 means Ubuntu 24.04+ and Debian 13+.
- **Maintainer scripts** (`packaging/deb/`):
  - `preinst`: when moving from a source install, removes its `/etc` unit and an unmodified
    `daemon.conf`, so nothing shadows the package or prompts.
  - `postinst`: byte-compiles, then enables and restarts the daemon. A restarted daemon adopts
    running instances, so **upgrades don't stop instances**.
  - `prerm`: on removal, runs `uninstall.sh --stop-only` (stops instances, network and helpers,
    removes per-user launchers). It always removes the byte-code caches.
  - `postrm purge`: deletes `/var/lib/waydroid-manager`, but only if nothing is mounted there,
    plus `/etc/waydroid-manager` and per-user caches.
- **Uninstall from the app** runs `uninstall.sh` through pkexec. For a package install
  (`install-method` = `deb`), that runs `apt-get remove` (or `purge`), so both install methods
  share one entry point.

To test a package change: install it over a running instance (upgrade), then `apt remove` (data
kept), then `apt purge` with your real `/var/lib/waydroid-manager` moved aside first. After that,
run `smoke.sh` against the installed package.

## Releasing

Versions follow [Semantic Versioning](https://semver.org/). Releases are published by
`.github/workflows/release.yml` when a `vX.Y.Z` tag is pushed.

1. **Prepare on `dev`** (through a PR, like any change):
   - bump `__version__` in `waydroid_manager/__init__.py`;
   - in `CHANGELOG.md`, rename `## [Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD`, add a new
     empty `## [Unreleased]` above it, and update the links at the bottom.
2. **Open a release PR `dev` → `main`** titled "Release X.Y.Z". Merge it with **"Create a merge
   commit"**, not squash or rebase. Every commit of `dev` then reaches `main` unchanged
   (`dev` stays an ancestor of `main`), so the next release PR never conflicts. Squashing would
   rewrite the commits, and later release PRs would conflict.
3. **Tag the merge commit on `main`:**
   ```sh
   git switch main && git pull
   git tag -a vX.Y.Z -m "waydroid-manager X.Y.Z"
   git push origin vX.Y.Z
   ```
4. **The workflow then:**
   - checks that the tag matches `__version__`, is on `main` and has a CHANGELOG section;
   - runs the unit tests;
   - builds the `.deb`;
   - publishes the GitHub release with the `.deb` attached and that CHANGELOG section as the
     notes.

   If it fails, fix the problem through `dev` → `main` again. Never move a published tag.
5. **Merge `main` back into `dev`.** The release PR's merge commit exists only on `main`, so
   GitHub would show `dev` as one commit "behind". Open a PR `main` → `dev` (or merge
   `origin/main` into a branch from `dev`) and merge it with **"Create a merge commit"**. It
   changes no files. Afterwards `dev` is only ever ahead of `main`.

### Instance #0 (stock Waydroid's data)

#0 is an ordinary instance (`instances/0/`, container `wdm-0`, MAC `…:ff:00` since `…:00:00` is
the bridge's), created on the first `List` by a user who has stock data. Its `data/` is the
owner's stock data dir, bind-mounted in place at start (`container.mount_stock_data`) and
unmounted in `container.cleanup`. Stock Waydroid must stay usable without us:

- Its data is never copied, chowned or identity-reset, and nothing under `/var/lib/waydroid` is written.
- #0 only starts on the exact image set stock uses (`images.stock_image_id()`), so stock's data never
  boots an older Android than stock's own.
- While #0 runs, `waydroid-container.service` is stopped and masked with `--runtime` (cleared by a
  reboot). `cleanup` and `uninstall.sh` unmask it, and start it again if it was running before #0
  (`/run/waydroid-manager/stock-was-active`).
- #0 can't be deleted. Purge refuses while anything is mounted under the state dir.

## Stock Waydroid pitfalls

- **Don't poll `waydroid status`.** Stock 1.6.x leaks a pipe and an epoll fd per `GetSession`
  call in its container service (`foreground_pipe()` never closes its selector;
  [waydroid#2357](https://github.com/waydroid/waydroid/issues/2357)). After about 500 calls,
  stock Waydroid can no longer start or stop anything. `stockctl.py` reads the stock state from
  `/sys/fs/cgroup/lxc.payload.waydroid/cgroup.events` and the `id.waydro.Session` bus name
  instead.
- **Stock run helpers** call `logging.verbose()`, so call `add_verbose_log_level()` after
  importing `tools` (the shim does). The daemon uses its own mount and run helpers.
- **`lxc-attach` chowns and chmods its stdio.** Never give it a caller's file or fd: it made a
  user's APK root-owned once. Always go through pipes (`storage.install_apk` streams the APK
  into `pm install -S`).
- **Binder probing** goes through the same leaky command runner. Its fds are only freed by the
  garbage collector, so `binder.py` calls `gc.collect()` afterwards.

## Wayland proxy notes

The HWC is a libwayland client we can't change, so the proxy rewrites its traffic. Things that
are easy to break:

- **Object ids.** libwayland-server only accepts contiguous client ids. The proxy creates its
  own objects (frame surfaces, buffers, cursor shapes), so it translates ids in both directions
  (`Translator`, driven by the vendored protocol XML). Globals the proxy doesn't know are hidden
  from the HWC.
- **Never forward a real configure size.** Every `xdg_toplevel.configure` with a non-zero size
  makes the HWC hotplug Android's display. The proxy always forwards 0×0 and does the window
  geometry itself.
- **Don't forward `xdg_toplevel.close`.** The HWC then wipes all Android recent tasks. The frame's
  close button reports `close` to the session instead. With `close_action=stop`, the session
  asks for confirmation (`gui/confirm.py`) and then calls `ReportClose`.
- **The scale is normalized to 1** (`wl_output.scale`, fractional preferred scale = 120). Android
  pixels therefore equal the configured resolution, and zoom is done by rewriting the HWC's
  viewport destination and subsurface positions. Input coordinates are scaled back.
- **Keys.** The HWC forwards evdev codes but drops codes ≥ 239. Back (158), Home (172) and
  volume (115/114) are synthesized `wl_keyboard.key` events. Recents (KEYCODE_APP_SWITCH, 580)
  goes through the daemon instead: `SendKey` writes raw `struct input_event`s into
  `/dev/input/wl_keyboard_events` inside the container, and keeps that FIFO's writer open
  while the instance runs.
- **fds.** Messages carrying file descriptors are only injected when the inbound buffer is empty,
  so fds stay paired with their messages.
- The HWC's first window can be a short-lived calibration window, and its `set_maximized` is
  dropped.
- **Buffers the desktop can't import** (`--cpu-buffers`; GNOME on NVIDIA takes no dmabufs from
  another device) become `wl_shm`:
  - `shared` (vkms): the linear dmabuf's own fd becomes the shm pool.
  - a render node (another GPU): never map its buffers for the compositor. They sit in VRAM, and
    reading VRAM through the PCI BAR is uncached (11 ms a 720p frame: gnome-shell at 99% CPU).
    The proxy imports each one with libgbm (`session/gbm.py`) and gives the compositor a memfd of
    its own. On every attach it maps the buffer for reading, so Mesa has the GPU copy it into
    cached memory first, and copies that into the memfd (0.8 ms).

## License

By contributing you agree that your contributions are licensed under the
[GPL-3.0-or-later](LICENSE), like the rest of the project.
