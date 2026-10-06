# Feasibility spike — findings (2026-10-03)

Host: Ubuntu, kernel 7.0, LXC 6.0.6, cgroup v2, stock Waydroid 1.6.2 (LineageOS 20 /
Android 13 GAPPS image), GNOME 50 Wayland, swiftshader rendering. The stock instance
kept running (frozen) during the whole spike and was unaffected.

The window labelling proxy from this spike became `waydroid_manager/session/wlproxy.py`.

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

# 1.0.0 spikes: NVIDIA and Android 11–17 (2026-10-05)

Host: Ubuntu 26.04, kernel 7.0, GNOME 50 on an NVIDIA RTX 5060 Ti (nvidia-open 595.91.07), plus an AMD
Granite Ridge iGPU, Waydroid 1.6.2. Every check ran in throwaway instances on this host.

## NVIDIA: Venus over vtest (waydroid-nvidia)

The only way onto the NVIDIA GPU with the proprietary driver: Android's Venus Vulkan driver sends Vulkan
over a unix socket to a patched `virgl_test_server --no-virgl --venus --multi-clients` on the host. GLES
goes through ANGLE on Vulkan, and gralloc allocates through `libgbm_mesa_wrapper.so`. Binaries:
quinovax/waydroid-nvidia v0.1.2 (sha256 checked against its SHA256SUMS).

| Check | Result |
|---|---|
| Android 13 boots | ANGLE `Vulkan 1.3.329 (NVIDIA Virtio-GPU Venus (NVIDIA GeForce RTX 5060 Ti))`; each Android client gets its own `virgl_render_server` |
| Display through our proxy (`persist.waydroid.use_subsurface=false`) | works, smooth |
| RAID: Shadow Legends 3D intro (arm64, Houdini) | **59.8 fps** (game cap), 27% CPU. Software (ANGLE on SwiftShader): **8.6 fps** at 173% CPU |
| Two instances, each with its own renderer | 59.7 fps each; GPU 12%, 2.6 GB VRAM |
| Freeze 15 s, then thaw | survives |
| Kill one renderer | only that instance breaks |
| New renderer on the same socket path | not seen by the container: a socket-file bind pins the dead inode. Bind the renderer's **directory** (rbind) |
| A renderer died during a SurfaceFlinger crash loop | respawn is needed |

- The socket mount must be `rbind`: a plain bind drops the nested socket bind.
- No render node is needed in the container for this path.
- **Android 14–17 need only** the A13-built `vulkan.virtio.so` (x86 + x86_64) and
  `libgbm_mesa_wrapper.so`. Their own ANGLE and hwcomposer work; the A13 ANGLE breaks 14 ("no suitable
  EGLConfig"). Android 13 uses the full set (Venus, ANGLE, wrapper, hwcomposer).

## Android images

| Android | Image | FS | Result on this host |
|---|---|---|---|
| 11 | official OTA lineage-18.1 GAPPS 20250628 | ext4 | boots in 12 s (SwiftShader); Houdini pin for SDK 30 works; Play Store in image |
| 13 | official OTA lineage-20.0 GAPPS | ext4 | as above (NVIDIA) |
| 14 | WayDroid-ATV 20260125 lineage-21.0 (vanilla) | ext4 | NVIDIA ✓; AMD ✓ with quirk props; software ✗ (solved later: vkms, below) |
| 15 | minhmc2007 lineage-22.2 20261005 (vanilla) | squashfs | NVIDIA ✓, boots in 15 s |
| 15 | WayDroid-ATV 20260224 lineage-22.2 | squashfs | ✗: system_server dies building MediaCodecList |
| 16 | WayDroid-ATV OTA a16-qpr2 lineage-23.2 GAPPS | EROFS + ext4 | NVIDIA ✓ (RAID renders via libndk), software ✓ |
| 17 | WayDroid-ATV OTA a17 lineage-24.0 GAPPS | EROFS + ext4 | NVIDIA ✓ with four fixes (below) |

Notes:
- **Checksums.** The OTA JSON `id` is the zip's sha256. GitHub release digests and the Hugging Face
  LFS oid are sha256 too. MindTheGapps `.sha256sum` files hold a bare hash with no file name.
- **Pairing.** Pair system and vendor by `version`. The a17 vendor channel lists two builds with the
  same datetime, so pick by the file name's date.
- **Downloads.** SourceForge's automatic mirror can crawl (80–190 KB/s). `master.dl.sourceforge.net`
  was ~1.5 MB/s. Downloads must resume.
- **Verification.** Verify by read-only loop mount (ext4, squashfs, EROFS), not debugfs.
- **Stock's set.** Official 13's newest build has the same datetimes as stock's set, so it is reused.
- **Host link.** libgbinder 1.1.53 built against the host's libglibutil 1.0.80 and preloaded with
  `ctypes.CDLL(RTLD_GLOBAL)` works with python3-gbinder 1.3.1: aidl5 (15) and aidl6 (16, 17 = API 37).

### How each image family picks graphics HALs

| Image | Selector | Our setting |
|---|---|---|
| 13 (official) | Waydroid init patch | plain props; a later duplicate key wins |
| 14 (ATV) | `/system/bin/waydroid-init`: amdgpu → `minigbm_amdgpu`, which fails on RDNA | `gralloc.override=0` + `ro.hardware.gralloc=minigbm_gbm_mesa` |
| 15 (minhmc) | vendor waydroid-init | `ro.gralloc.override=0` + `ro.hardware.gralloc=minigbm_gbm_mesa` |
| 16, 17 (ATV) | vendor waydroid-init: detects NVIDIA and falls back to software | `ro.waydroid.override_props=0` keeps our `ro.hardware.*` |

- On 14+, the FIRST duplicate key wins. Generated props must have exactly one line per key.
- With `egl=angle`, 14+ load ANGLE from `/system/lib64`.
- An overlay layer must contain `system/…`. Mounting a layer's `system/` at the image root hides the
  `/product` and `/system_ext` symlinks, and boot hangs.

### 14 and 15 bugs and workarounds
- **14 + AMD:** `allocator@4.0-service.minigbm_amdgpu: Failed to initialize driver`, fixed by the props
  above. On this host GNOME (on NVIDIA) then rejects the AMD dmabufs (mutter#3930, the same as 13 on the
  iGPU).
- **14/15 software mode:** the hwcomposer can't read gralloc-default buffer metadata (format 0) and
  sends `wl_shm` format −EINVAL. It's fixed in source (android_hardware_waydroid `1761e9a7af`) but not
  in any published build. Solved later with vkms, below.
- **ATV 15:** a `c2.ffmpeg.dts.decoder` entry with two `<Type>`s trips
  `AudioCapabilities::getDefaultFormat` (ubsan). Fixed upstream (stagefright-plugins `a44e827c55`); the
  minhmc build includes it.

### Google Play on 14 and 15 (MindTheGapps layer)
- The zip's `system/` tree becomes an overlay layer. Pre-extract the x86/x86_64 libs of each APK next
  to it: the system partition is read-only.
- Leave out **SetupWizard**: it crashes ("WifiService: Permission denied") and blocks GSF check-in.
  Images without a setup wizard of their own then need `device_provisioned=1` / `user_setup_complete=1`.
- Use **MindTheGapps 14.0.0 for 15 as well.** 15.0.0's GSF registers its gservices provider as
  `…gms.gservices.provider.do.not.use`, and Play Store and GMS crash ("Failed to find provider
  com.google.android.gsf.gservices").
- The GSF ID: `content query` on the gservices provider returns nothing on 14+. Read
  `data/data/com.google.android.gsf/databases/gservices.db` on the host instead.

### Android 17 needs
1. `/dev/loop-control` plus loop nodes (`/dev/block/loopN`) in the container: `apexd-bootstrap` mounts
   `.apex` files through loop devices (45 APEXes). Host nodes must already exist for the numbers
   `LOOP_CTL_GET_FREE` returns.
2. The host `videodev` module loaded. Without `/sys/class/video4linux`, 17's ueventd passes a null DIR*
   to `dirfd()` ("FORTIFY: dirfd: null DIR*"), aborts 4×, and init reboots.
3. `debug.hwui.renderer=skiavk`. skiagl over ANGLE+Venus aborts ("Failed to set damage region …
   EGL_BAD_ACCESS", "GL errors! SkiaOpenGLPipeline.cpp").
4. GMS ships as a signed APEX (`com.google.android.gmssystem.prodvic.apex`) that needs device-mapper.
   Unpack its EROFS payload into a layer as `system/product/priv-app/PrebuiltGmsCoreVic/` plus its
   permission and sysconfig XML, and hide the `.apex` with an overlayfs whiteout. GmsCore then provides
   `com.google.android.gsf.gservices`.

### Navigation on 15, 16 and 17
The ATV images (and minhmc's 15) use Launcher3's large-screen Taskbar as the navigation bar, and it
draws an empty 72 px strip on Waydroid. `qemu.hw.mainkeys=1` removes it. Back/Home/Recents come from
the window toolbar.

### Also found
- App windows (single-window mode) stalled on every focus change: the hwcomposer hotplugs Android's
  display on each sized `xdg_toplevel.configure`. Fixed in the proxy (only size changes pass through).
- 15 started in time zone GMT-11: pass the host time zone.

# Found while building 1.0.0 (2026-10-05 and 06)

Same host. Every item below is in the code now.

## Android replayed the host's device events (GNOME logouts)
- The WayDroid-ATV images' ueventd remounts sysfs (`fsopen`/`fsmount`/`fspick` RECONFIGURE) and, at
  coldboot, writes `add` into every `uevent` file it finds. Through the privileged container that is
  the host's sysfs: every host device event fired again. With vgem around, GNOME re-probed its GPUs,
  and the session ended.
- Fix: an AppArmor profile, stock's `lxc-waydroid` plus `deny /**/uevent w,`. Deny rules hold even in
  complain mode. lxc-start only switches to profiles named `lxc-*`, hence `lxc-waydroid-manager`.
- A seccomp filter on the mount calls was tried first: it broke Android 16's apexd.

## Software rendering on 14, 15 and 17: vkms
17 has no gralloc.default mapper, and 14 and 15's hwcomposer can't show gralloc.default buffers.
They render on the CPU into buffers of a hidden vkms device instead:
- The device is made through configfs (`/sys/kernel/config/vkms/waydroid-manager`: one plane, crtc,
  encoder and connector). The connector is disconnected, so no desktop shows it, and a udev rule
  loaded first tags it `mutter-device-ignore` for GNOME.
- Its node must be `0666`: app processes open it too. At `0660` Android 13 on NVIDIA (which uses
  the same node, below) flickered and went black at times.
- minigbm allocates linear dumb buffers there: `minigbm_generic` on 14, plain `minigbm` on 15 and
  17. The hwcomposer only reads the metadata of gralloc modules named `minigbm_*` (or `gbm`). Pastel
  draws, and 17 runs ANGLE on it.
- **15 can't render in software.** With plain `minigbm` Android draws (screencap shows it), but the
  hwcomposer, lacking the metadata, imports each frame into SwiftShader as format 0 ("UNSUPPORTED:
  AHardwareBuffer_Format 0") and sends black `wl_shm` frames with a −errno format. Its image has no
  `minigbm_generic`; `minigbm_gbm_mesa` on vkms aborts SurfaceFlinger ("Failed to create a valid
  texture", SwiftShader can't import it), `minigbm_celadon` crashes the hwcomposer, and `gbm` (the
  2.0 allocator) crash-loops. 15 runs on a GPU only; Software is refused.
- GNOME can't import vkms dmabufs, so the proxy hands the same fd over as a `wl_shm` pool.

## NVIDIA
- minigbm opens a DRM device even with the gbm wrapper. Without one, SurfaceFlinger aborts with
  "output buffer not gpu writeable". The vkms node stands in.
- **17: games' windows were black.** Mesa's `vk_image_usage_to_ahb_usage` turns
  `VK_IMAGE_CREATE_MUTABLE_FORMAT_BIT` into `CPU_WRITE_RARELY` (to force a linear layout), and
  NVIDIA can't create those images: an instrumented Venus showed the creation failing with -11.
  ANGLE asks for mutable-format swapchains, and only 17's libvulkan takes the producer usage from
  `vkGetPhysicalDeviceImageFormatProperties2`. Fix: `debug.angle.feature_overrides_disabled` with
  `supportsSwapchainMutableFormat`. ANGLE's override lists are colon-separated, not comma-separated.
- RAID: Shadow Legends: 62 fps on 16 and 17, 56–60 on 13.

## Loop devices and device-mapper
- 14, 15 and 16 need loop devices too, not only 17.
- apexd uses device-mapper when it can open it: it left 35 dm devices on the host. The containers
  now deny `c 10:236`.
- An LXC `lxc.cgroup2.devices.deny` line on its own turns the device list into deny-all. Start with
  `lxc.cgroup2.devices.allow = a`.

## A picked GPU
- Stock's generated `ro.hardware.gralloc`/`egl`/`vulkan` props must not be inherited: on a picked
  GPU they kept Android in software. So did a leftover user prop `ro.waydroid.software_rendering=1`.

## Another GPU under a desktop on NVIDIA
- GNOME on NVIDIA rejects the AMD iGPU's dmabufs (tiled, modifier `0x200000000401b03`; mutter#3930).
- `minigbm_gbm_mesa` honours the hwcomposer's `waydroid.modifiers.*` props (the compositor's
  modifiers ∩ the GPU's), which gives LINEAR buffers. Handed to GNOME as `wl_shm` over the same fd,
  they displayed, but gnome-shell sat at 78% CPU idle and 99% animating.
- Those buffers live in the iGPU's VRAM carve-out, and the CPU reads them through the PCI BAR,
  uncached: 340 MB/s, 11 ms a 720p frame. `AMD_DEBUG=nowc` doesn't help (that is GTT, not VRAM).
- Mapping them for reading through libgbm makes radeonsi blit them into a cached GTT staging
  texture first: 0.8 ms. The proxy now copies every attached frame that way into a memfd of its
  own. With RAID at 62 fps, gnome-shell is at 10% CPU and the proxy at 3%.

## Shader caches of another renderer
- A 0.5 device (software) moved to NVIDIA: its launcher crashed every 2 s, SIGFPE in hwui's
  `BlobCache::clean()` (integer division by zero) while storing a shader. The caches in
  `code_cache/com.android.skia.shaders_cache` were SwiftShader's; with them gone it ran. hwui only
  checks the GLSL version string, which ANGLE reports the same on SwiftShader and on NVIDIA.
- The daemon clears the apps' shader caches when a device's renderer changes. #0 shares its data with
  stock Waydroid (software here): when #0 renders otherwise, they are cleared at its start and stop.

## Android 15 on NVIDIA: Xid 69 on screen transitions (open)
- Opening a new activity (any Settings sub-page) on 15 ends in `NVRM: Xid 69 … Class 0000ce97,
  Offset 000019d0, Data 0000003c` from SurfaceFlinger's context; the renderer reports
  VK_ERROR_DEVICE_LOST and restarts, SurfaceFlinger aborts in `vn_relax`, and Android's display
  restarts. Method 0x19D0 of the 3D class is `CLEAR_SURFACE` (NVIDIA open-gpu-doc), data 0x3C =
  clear R, G, B and A of colour target 0: the GPU rejects a colour clear.
- 15's Shell builds a "Right Edge Extension" layer for activity transitions (a 1 px wide capture
  of the window's edge through SurfaceFlinger, stretched); 17 makes the same transition without one
  and doesn't fault. Not the cause: the RenderEngine backend (skiavkthreaded faults too), the
  animation scale (0 still builds the extension), Settings' activity embedding.
- waydroid-nvidia's own telemetry names this fingerprint (Class 0xC197 Offset 0x19D0 Data 0x3C) as
  a recurring, unattributed fault. Candidates for a fix: a framework overlay dropping `<extend>`
  from 15's activity animations, or Venus skipping degenerate clears.

## Measuring
- `dumpsys SurfaceFlinger --latency` is empty on 16 and newer. `dumpsys SurfaceFlinger --timestats
  -enable`, then `-dump`, gives per-layer fps on every version.
