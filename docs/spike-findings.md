# Feasibility spike — findings (2026-10-03)

Host: Ubuntu, kernel 7.0, LXC 6.0.6, cgroup v2, stock Waydroid 1.6.2 (LineageOS 20 /
Android 13 GAPPS image), GNOME 50 Wayland, swiftshader rendering. The stock instance
kept running (frozen) during the whole spike and was unaffected.

Scripts: `scripts/spike/spike.py` (bring-up as root), `scripts/spike/spike_hw.py`
(binder service test), `waydroid_multi/session/wlproxy.py` (window labelling proxy).

| # | Question | Result |
|---|----------|--------|
| 1 | Extra instance with its own binder nodes (`binderfs/wdm<N>-*`) in the existing binderfs, own LXC path/name, own rootfs mounts | **Works.** Boots from an empty data dir to `sys.boot_completed=1` in ~10 s. HWC opens the full-UI window on the host compositor. |
| 1b | Host user talks to the instance over binder | **Works** without root: stock `IPlatform.get_service()` with a per-instance `args.config` returns that instance's props/apps. |
| 2 | Several instances at once | **Works** (4 simultaneous + stock). ~1.7–3.4 GiB `memory.current` per instance (incl. page cache). |
| 3 | Shared bridge + static DHCP + isolation | **Works**: fixed IP `.10+N`, internet + DNS, instances cannot reach each other (`bridge link set … isolated on`), gateway reachable. |
| 4 | Binder services from one process | python3-gbinder's async `add_service()` is **broken** (`add_service_func` attribute missing). `add_service_sync()` works for several instances in one process. |
| 4b | What triggers `IHardware.suspend` | Closing the window does **not** (Android stays awake holding a screen wake lock; HWC sets `waydroid.active_apps=none`, `waydroid.open_windows=0`). Suspend only comes after idle/screen timeout. |
| 5 | Title/app_id rewriting proxy | **Works**: window shows as "Waydroid · <name>", separate dock entry, and `xdg_toplevel.close` on the full-UI window is detected. |
| 6 | Clone with `cp -a` as root | **Boots fine** (no boot loop; ownership/xattrs preserved). 179 MiB fresh data copies in <1 s. |
| 6b | Device identity reset | Settings files are binary XML (ABX) — don't edit offline. Deleting **both** `settings_ssaid.xml` and `settings_ssaid.xml.fallback` before boot regenerates the SSAID user key (per-app Android IDs). `settings put secure android_id <hex>` as root works after boot. |
| 7 | Container survives its starter exiting | **Yes** with `lxc-start -d`. |
| 8 | cgroup2 limits | `memory.high` and `cpuset.cpus` applied and boot works. |
| 9 | Window size per instance | `persist.waydroid.width/height` in the instance's `vendor/waydroid.prop` sets the Android display/window size. |
| 10 | Clipboard on GNOME 50 | `wl-copy` / `wl-paste` work without focus. |

## Bugs/gotchas found
- Stock run helpers call `logging.verbose()`: call `tools.helpers.logging.add_verbose_log_level()` after import.
- **Bridge MAC must not collide with instance MACs** (the bridge used `02:57:44:4d:00:01`, the
  same as instance 1's eth0 → DHCP offers never reached the container). Bridge now uses `…:00:00`.
- Recreating the bridge under a running dnsmasq breaks its DHCP socket binding → bridge setup and
  dnsmasq must live and restart together (one systemd service).
- LXC `script.up` for veth gets `$1=name $2=net $3=up $4=veth $5=<link/bridge> $6=<host veth>`.
- `lxc-attach` changes the mode of its stdout file → pipe its output.
- `abx2xml`, `pm`, etc. need the full Android environment (`ANDROID_ENV` + generated classpath).
- One boot showed a composer abort "Binder threadpool cannot be shrunk after starting" (vendor HWC
  race, observed with `cpuset=0-3`); the HAL restarted itself and the window came up.

## UX observations
- Full-UI windows have **no title bar on GNOME** (no server-side decorations; Waydroid's HWC
  draws none) — same as stock. Move with Super+drag. The Android display size is fixed per boot,
  so the window is not resizable.
- Window sizes must fit the monitor (this host: 1366×768); presets are computed from the monitor's
  work area.
