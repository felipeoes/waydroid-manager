# SPDX-License-Identifier: GPL-3.0-or-later
"""Per-instance container lifecycle (root).

Mirrors stock ``container_manager.do_start``/``stop`` with per-instance
paths. Callers hold the instance lock.
"""
import configparser
import glob
import os
import platform
import pwd
import select
import shutil
import stat
import subprocess
import threading
import time

from .. import catalog, devices, gpu, lxcconfig, paths, stock, stockctl
from ..instance import PROTECTED_PROP_RE, Instance
from . import armtrans, binder, images, magisk, nvidia, storage
from .util import (CommandError, apparmor_profile_loaded, attach, bind, bind_file, bind_mount, chown_tree_top,
                   is_mount, log, lxc_state, mount_image, mount_overlay, run, stage_dir, stage_socket,
                   umount_tree)

DEVICE_NODES = [
    "/dev/ashmem", "/dev/sw_sync", "/sys/kernel/debug/sync/sw_sync",
    "/dev/Vcodec", "/dev/MTK_SMI", "/dev/mdp_sync", "/dev/mtk_cmdq",
    "/dev/graphics", "/dev/pvr_sync", "/dev/ion",
]


def stock_args(inst, config=None):
    w = inst.cfg["waydroid"]
    return stock.make_args(
        inst.dir, config or inst.cfg_path,
        vendor_type=w.get("vendor_type", "MAINLINE"),
        arch=w.get("arch", platform.machine()),
        images_path=w.get("images_path", ""),
        system_ota=w.get("system_ota", "None"),
        vendor_ota=w.get("vendor_ota", "None"),
        BINDER_DRIVER=w["binder"], VNDBINDER_DRIVER=w["vndbinder"], HWBINDER_DRIVER=w["hwbinder"],
        BINDER_PROTOCOL=w.get("binder_protocol"), SERVICE_MANAGER_PROTOCOL=w.get("service_manager_protocol"),
    )


STOCK_UNIT = "waydroid-container.service"
STOCK_WAS_ACTIVE = os.path.join(paths.RUN_DIR, "stock-was-active")  # release_stock starts it again
# No device-mapper: Android 14 and 15's apexd maps its APEXes there when it can, and those
# mappings belong to the host: they outlive the container and collide between devices. Without
# it apexd mounts its loop devices directly, as 16 and 17 do.
DEVICE_DENY = "lxc.cgroup2.devices.allow = a\nlxc.cgroup2.devices.deny = c 10:236 rwm\n"   # alone, a deny allows nothing
GRAPHICS_PROPS = ("ro.hardware.gralloc", "ro.hardware.egl", "ro.hardware.vulkan")
APPARMOR_PROFILE = "lxc-waydroid-manager"   # lxc-start may only switch to lxc-* profiles
APPARMOR_FILE = os.path.join(paths.RUN_DIR, "apparmor-profile")


def _stock_stopped():
    if stockctl.container_state() != "STOPPED":
        raise RuntimeError("stock Waydroid is running; stop it first ('waydroid session stop')")


def mount_stock_data(inst):
    """#0 runs on stock Waydroid's own data, bind-mounted in place (never copied or
    changed by us). Stock's container service is masked until #0 stops, so stock
    Waydroid can't boot the same data alongside it; the mask is --runtime, so a
    reboot clears it even if we never get to."""
    if run(["systemctl", "is-active", STOCK_UNIT], check=False).stdout.strip() == "active":
        open(STOCK_WAS_ACTIVE, "w").close()
    run(["systemctl", "mask", "--runtime", STOCK_UNIT])
    run(["systemctl", "stop", STOCK_UNIT], check=False)
    _stock_stopped()  # also catches a start between start()'s check and the mask
    umount_tree(inst.data_dir)
    fd = storage.open_stock_data(inst.owner_uid)
    try:
        bind_mount("/proc/self/fd/{}".format(fd), inst.data_dir)
    finally:
        os.close(fd)


def release_stock():
    """Undo mount_stock_data's mask (only if it is ours) and start stock's service again if
    it was running before."""
    if run(["systemctl", "is-enabled", STOCK_UNIT], check=False).stdout.strip() != "masked-runtime":
        return
    run(["systemctl", "unmask", "--runtime", STOCK_UNIT], check=False)
    try:
        os.remove(STOCK_WAS_ACTIVE)
    except FileNotFoundError:
        return
    run(["systemctl", "start", STOCK_UNIT], check=False)


def ensure_dirs(inst):
    # Only root and the owner's group may enter the instance directory: inside data/,
    # files belong to raw Android uids that can match other host users' uids.
    pw = pwd.getpwuid(inst.owner_uid)
    os.makedirs(inst.dir, exist_ok=True)
    os.chown(inst.dir, 0, pw.pw_gid, follow_symlinks=False)
    os.chmod(inst.dir, 0o710)
    for d in ("rootfs", "overlay/vendor", "overlay_rw/system", "overlay_rw/vendor",
              "overlay_work/system", "overlay_work/vendor"):
        os.makedirs(os.path.join(inst.dir, d), exist_ok=True)
    os.makedirs(inst.lxc_dir, exist_ok=True)
    if not os.path.isdir(inst.data_dir):
        chown_tree_top(inst.data_dir, inst.owner_uid, pw.pw_gid, 0o771)


def wipe_overlay_rw(inst):
    for d in ("overlay_rw", "overlay_work"):
        shutil.rmtree(os.path.join(inst.dir, d), ignore_errors=True)


def stock_android():
    """The catalog version stock Waydroid's images are, or None (one Waydroid Manager doesn't
    offer, or not synced yet)."""
    sdk = images.read_cfg(images.current_id()).get("sdk", "")
    return catalog.key_for_sdk(int(sdk)) if sdk.isdigit() else None


def android_of(inst):
    """The catalog version an instance runs: #0 runs stock's images."""
    return stock_android() if inst.index == 0 else inst.get("android")


def runs_stock_android(inst):
    """Stock Waydroid's overlay, and what waydroid_script put there and in its properties (ARM
    translation), were made for stock's own Android version."""
    return android_of(inst) == stock_android()


def finish_setup(inst):
    """Images without a setup wizard of their own (MindTheGapps' is left out: it crashes):
    mark the device set up, or Google Play's check-in never happens."""
    for args in (("global", "device_provisioned", "1"), ("secure", "user_setup_complete", "1")):
        attach(inst.id, ["/system/bin/cmd", "settings", "put"] + list(args), check=False)


def select_image(inst, in_use):
    """Pick the image set the instance runs: #0 stock's, any other the newest of its Android
    version (downloaded first if there is none yet). in_use: see images.gc. Returns its directory."""
    key = inst.get("android")
    if inst.index != 0 and not images.latest(key):
        images.install(key)
    with images.lock:   # the set is recorded before a gc can see it unused
        cur = images.ensure_synced(in_use) if inst.index == 0 else images.latest(key)
        if inst.image_id != cur:
            if inst.image_id:
                log.info("%s: switching image set %s -> %s", inst.id, inst.image_id, cur)
            wipe_overlay_rw(inst)
            inst.cfg["instance"]["image_id"] = cur
            inst.cfg["waydroid"]["images_path"] = images.image_dir(cur)
            if inst.index == 0:
                stock_cfg = stock.load_stock_cfg()["waydroid"]
                for k in ("system_datetime", "vendor_datetime", "system_ota", "vendor_ota"):
                    if k in stock_cfg:
                        inst.cfg["waydroid"][k] = stock_cfg[k]
            inst.save()
    d = images.image_dir(cur)
    if not os.path.isfile(os.path.join(d, "system.img")):
        raise RuntimeError("image set {} is missing; run 'waydroid-manager images update'".format(cur))
    return d


# Held from reading the other instances' pins to writing this one's, so parallel starts
# see each other's picks
_pin_lock = threading.Lock()


def _read(path):
    with open(path) as f:
        return f.read()


def host_cpus():
    """CPUs the containers' cgroups (children of the root one) can be pinned to, without the
    isolated ones (isolcpus=: no load balancing there); [] when the cpuset controller can't be
    enabled for them, so no pin is written that would fail the start."""
    try:
        # systemd enables it on most hosts; enabling it changes no other cgroup's CPUs
        if "cpuset" not in _read("/sys/fs/cgroup/cgroup.subtree_control").split():
            with open("/sys/fs/cgroup/cgroup.subtree_control", "w") as f:
                f.write("+cpuset")
        cpus = lxcconfig.parse_cpus(_read("/sys/fs/cgroup/cpuset.cpus.effective"))
    except OSError as e:
        log.warning("no cgroup v2 cpuset controller (%s): CPU-limited instances can't be pinned "
                    "and may stall under load", e)
        return []
    try:
        isolated = lxcconfig.parse_cpus(_read("/sys/devices/system/cpu/isolated"))
    except (OSError, ValueError):
        isolated = []
    return [c for c in cpus if c not in isolated]


def host_cores(cpus):
    """CPU -> the CPUs of its physical core (SMT threads)."""
    cores = {}
    for c in cpus:
        try:
            cores[c] = lxcconfig.parse_cpus(_read(
                "/sys/devices/system/cpu/cpu{}/topology/thread_siblings_list".format(c)))
        except (OSError, ValueError):
            pass
    return cores


def pinned_cpus(inst):
    """CPUs an instance was pinned to at its last start ([] = not pinned)."""
    try:
        with open(os.path.join(inst.lxc_dir, "config")) as f:
            return lxcconfig.config_cpuset(f)
    except (OSError, ValueError):
        return []


def write_lxc_config(inst, net, cpus_busy=(), render=("gpu", None)):
    """render: (gpu mode, Android's DRM node or None), see render_for."""
    a = stock_args(inst)
    cpuset = inst.get("cpuset")
    if cpuset == "all":
        cpuset = ""
    else:
        cpus = host_cpus()  # also enables the cpuset controller, which a set cpuset needs too
        if inst.get("cpus") and not cpuset:
            cpuset = lxcconfig.pick_cpus(inst.get("cpus"), cpus, cpus_busy, host_cores(cpus))
    limits = lxcconfig.cgroup_limits(inst.get("cpus"), cpuset, inst.get("memory"))
    text = lxcconfig.build_config(
        stock.lxc_snippets(),
        rootfs=inst.rootfs, lxc_dir=inst.lxc_dir, bridge=net.cfg.bridge, mac=inst.mac,
        veth=inst.veth, uts_name="waydroid-" + inst.id.replace("_", "-"), arch=platform.machine(),
        apparmor_profile=load_apparmor_profile(),
        poststop_hook=paths.POSTSTOP_SCRIPT,
        netup_hook=paths.NET_UP_SCRIPT if net.cfg.isolate else None, limits=limits)
    _write(os.path.join(inst.lxc_dir, "config"), text + DEVICE_DENY)
    shutil.copy(stock.seccomp_profile(), os.path.join(inst.lxc_dir, "waydroid.seccomp"))
    nodes = stock.tools().helpers.lxc.generate_nodes_lxc_config(a)
    if catalog.get(inst.get("android"), "loop") and inst.index != 0:
        nodes += loop_entries()
    mode, node = render
    if node or mode != "gpu":   # only the one picked: minigbm takes the first DRM device it can use
        nodes = [n for n in nodes if "= /dev/dri/" not in n]
    if node:
        nodes.append("lxc.mount.entry = {} {} none bind,create=file 0 0".format(node, node[1:]))
    _write(os.path.join(inst.lxc_dir, "config_nodes"), "\n".join(nodes) + "\n")
    session = os.path.join(inst.lxc_dir, "config_session")
    if not os.path.exists(session):
        _write(session, "")


def _write(path, text, mode=0o644):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def set_device_permissions():
    nodes = list(DEVICE_NODES)
    for pattern in ("/dev/dri/renderD*", "/dev/fb*", "/dev/video*", "/dev/dma_heap/*"):
        nodes.extend(glob.glob(pattern))
    for p in nodes:
        if os.path.exists(p):
            run(["chmod", "777", "-R", p], check=False)


def apparmor_profile(text, stock_name):
    """Stock Waydroid's container profile under our name, denying writes to sysfs uevent files.
    WayDroid-ATV's ueventd (16, 17) mounts its own read-write sysfs and writes "add" into every
    device's uevent file at boot: the kernel replays those events on the host, where the desktop
    re-adds its GPUs and input devices (GNOME can crash on it). Android needs only the nodes the
    container config binds, and apexd's other sysfs writes stay allowed."""
    head = "profile {} ".format(stock_name)
    if head not in text:
        raise RuntimeError("unexpected AppArmor profile " + stock_name)
    text = text.replace(head, "profile {} ".format(APPARMOR_PROFILE), 1).replace(stock_name + "//", APPARMOR_PROFILE + "//")
    return text.replace("{\n", "{\n  deny /**/uevent w,\n", 1)


def load_apparmor_profile():
    """Our profile's name once loaded; None without AppArmor or stock Waydroid's profile."""
    name = stock.tools().helpers.lxc.LXC_APPARMOR_PROFILE
    if not apparmor_profile_loaded(name):
        return None
    with open(os.path.join("/etc/apparmor.d/lxc", name)) as f:
        _write(APPARMOR_FILE, apparmor_profile(f.read(), name))
    run(["apparmor_parser", "--replace", "--skip-cache", APPARMOR_FILE])
    return APPARMOR_PROFILE


LOOP_DEVICES = 256


def loop_entries():
    """Android 14 to 17's apexd mounts its APEXes through loop devices: the container gets
    loop-control and the host's loop nodes, at /dev/block/loopN where apexd looks for them.
    The kernel makes a node only once its device is used, so missing ones are made here."""
    # ponytail: shares every host loop device with the (already privileged) container;
    # a dedicated range needs loop numbers apexd doesn't pick itself
    entries = ["lxc.mount.entry = /dev/loop-control dev/loop-control none bind,create=file 0 0"]
    for n in range(LOOP_DEVICES):
        node = "/dev/loop{}".format(n)
        if not os.path.exists(node):
            os.mknod(node, 0o660 | stat.S_IFBLK, os.makedev(7, n))
        entries.append("lxc.mount.entry = {} dev/block/loop{} none bind,create=file 0 0".format(node, n))
    return entries


VKMS_DEVICE = "/sys/kernel/config/vkms/waydroid-manager"
_vkms_lock = threading.Lock()   # starts run in parallel


def render_for(inst):
    """(gpu mode, Android's DRM node or None: stock Waydroid's pick) of this start; refuses what
    can't run. In software (vkms) and NVIDIA modes the node is our vkms device's; for a picked
    GPU, its render node."""
    key = android_of(inst)
    try:
        mode, node = gpu.mode(key, inst.get("gpu"))
    except ValueError as e:
        raise RuntimeError(str(e))
    if mode == "software" and key and catalog.get(key, "software", True) is False:
        raise RuntimeError("Android {} can't render in software: pick a GPU in Graphics".format(key))
    if mode == "nvidia":
        if not (key and catalog.get(key, "nvidia")):
            raise RuntimeError("Android {} has no NVIDIA build; pick another GPU in Graphics".format(key))
        if not nvidia.available():
            raise RuntimeError("NVIDIA rendering needs NVIDIA's proprietary driver")
    return mode, (vkms_card() if mode in ("vkms", "nvidia") else node)


def vkms_card():
    """Our vkms device's node, made on first use: Android renders on the CPU into its buffers,
    which minigbm can map for any use. Its one output is disconnected, so no desktop shows it,
    and our udev rule (loaded before it appears) tags it for GNOME to leave alone."""
    d = VKMS_DEVICE
    with _vkms_lock:
        try:
            enabled = _read(d + "/enabled").strip() == "1"
        except OSError:
            enabled = False
        if not enabled:
            # an older vkms ignores create_default_dev and makes its default device, whose
            # connected output the desktop would show
            if not gpu.vkms_supported():
                raise RuntimeError("rendering on NVIDIA or in software (Android 14, 17) needs Linux 6.19 or "
                                   "newer: older kernels' vkms can't make the hidden device Android draws into")
            run(["udevadm", "control", "--reload"])
            run(["modprobe", "vkms", "create_default_dev=0"])
            # each step is skipped when done: this also completes a setup that failed partway
            for group in ("planes/plane", "crtcs/crtc", "encoders/encoder", "connectors/connector"):
                os.makedirs(os.path.join(d, group), exist_ok=True)
            for attr, value in (("planes/plane/type", "1"), ("connectors/connector/status", "2")):  # primary; disconnected
                with open(os.path.join(d, attr), "w") as f:
                    f.write(value)
            for target, link in (("crtcs/crtc", "planes/plane/possible_crtcs/crtc"),
                                 ("crtcs/crtc", "encoders/encoder/possible_crtcs/crtc"),
                                 ("encoders/encoder", "connectors/connector/possible_encoders/encoder")):
                if not os.path.lexists(os.path.join(d, link)):
                    os.symlink(os.path.join(d, target), os.path.join(d, link))
            with open(d + "/enabled", "w") as f:
                f.write("1")
    cards = glob.glob("/sys/devices/faux/waydroid-manager/drm/card*")
    if not cards:
        raise RuntimeError("software rendering needs the kernel's vkms module")
    node = "/dev/dri/" + os.path.basename(cards[0])
    # Android's apps open it with their own uids: open to all, like the render nodes stock opens
    # (16 and 17 do it themselves at boot, 13 doesn't). It has no output to drive.
    os.chmod(node, 0o666)
    return node


def ensure_videodev():
    """Android 17's ueventd aborts when /sys/class/video4linux is missing; loading videodev
    (no device needed) creates it."""
    if not os.path.isdir("/sys/class/video4linux"):
        run(["modprobe", "videodev"], check=False)
    if not os.path.isdir("/sys/class/video4linux"):
        raise RuntimeError("Android 17 needs the kernel's videodev module (/sys/class/video4linux)")


def sync_root(inst):
    """Root switch: Magisk Delta in the instance's own lower layer (removed when off)."""
    if inst.getbool("root") and not android_of(inst):
        log.warning("%s: root needs an Android version Waydroid Manager knows; starting without it", inst.id)
    elif inst.getbool("root"):
        magisk.install(inst)
    elif os.path.isdir(os.path.join(inst.dir, "overlay", magisk.MAGISK.lstrip("/"))):
        magisk.remove(inst)


def mount_rootfs(inst, images_dir, gpu_layers=()):
    """system.img + overlays, vendor.img + overlays, like stock mount_rootfs. Below the instance's
    own layer come the shared ones: GPU (NVIDIA's guest build), ARM translation, Google Play,
    and stock Waydroid's overlay when the image is stock's Android version (it was made for
    that). Each layer's system/ and vendor/ join the matching stack. Returns the ARM
    translation layer it mounted, or None."""
    rootfs = inst.rootfs
    sync_root(inst)
    umount_tree(rootfs)
    mount_image(os.path.join(images_dir, "system.img"), rootfs)
    sdk = stock.read_prop_file(rootfs + "/system/build.prop", "ro.build.version.sdk")
    arm = armtrans.layer(inst.get("arm_translation"), sdk)
    shared = list(gpu_layers) + [d for d in (arm, images.gapps_layer(inst.image_id)) if d]
    if os.path.isdir(paths.STOCK_OVERLAY) and runs_stock_android(inst):
        shared.append(paths.STOCK_OVERLAY)
    lowers = [os.path.join(inst.dir, "overlay")] + [d for d in shared if d == paths.STOCK_OVERLAY or
                                                    os.path.isdir(os.path.join(d, "system"))]
    mount_overlay(lowers + [rootfs], rootfs, os.path.join(inst.dir, "overlay_rw/system"),
                  os.path.join(inst.dir, "overlay_work/system"),
                  writable=inst.getbool("system_writable"))
    mount_image(os.path.join(images_dir, "vendor.img"), rootfs + "/vendor")
    vlowers = [d + "/vendor" for d in [os.path.join(inst.dir, "overlay")] + shared if os.path.isdir(d + "/vendor")]
    mount_overlay(vlowers + [rootfs + "/vendor"], rootfs + "/vendor",
                  os.path.join(inst.dir, "overlay_rw/vendor"), os.path.join(inst.dir, "overlay_work/vendor"),
                  writable=inst.getbool("system_writable"))
    for egl_path in ("/vendor/lib/egl", "/vendor/lib64/egl"):
        if os.path.isdir(egl_path):
            bind(egl_path, rootfs + egl_path)
    if is_mount("/odm"):
        bind("/odm", rootfs + "/odm_extra")
    elif os.path.isdir("/vendor/odm"):
        bind("/vendor/odm", rootfs + "/odm_extra")
    return arm


def detect_protocols(inst):
    sdk = stock.read_prop_file(inst.rootfs + "/system/build.prop", "ro.build.version.sdk")
    try:
        sdk = int(sdk)
    except ValueError:
        log.error("%s: could not read Android SDK level from system.img", inst.id)
        sdk = 0
    rpc, sm = stock.protocols_for_sdk(sdk, stock.known_sm_protocols())
    w = inst.cfg["waydroid"]
    if w.get("binder_protocol") != rpc or w.get("service_manager_protocol") != sm:
        w["binder_protocol"] = rpc
        w["service_manager_protocol"] = sm
        inst.save()


def one_per_key(lines):
    """One line per property, the last value winning in the first one's place: Android 13
    takes a repeated key's last line, 14 and newer its first."""
    out = {}
    for line in lines:
        if line.strip():
            out[line.partition("=")[0]] = line
    return list(out.values())


def host_timezone():
    try:
        return os.readlink("/etc/localtime").partition("zoneinfo/")[2]
    except OSError:
        return ""


def stock_props(inst, arm, mode):
    """Stock Waydroid's [properties] a start keeps (arm, mode: see write_props)."""
    kind = inst.get("arm_translation")
    stock_way = runs_stock_android(inst)
    out = {}
    for k, v in stock.load_stock_cfg()["properties"].items():
        # stock's graphics choice holds while #0 renders as stock does; elsewhere ours follow
        # the Graphics setting
        if k in GRAPHICS_PROPS and not (inst.index == 0 and graphics_key(inst, mode) == "gpu auto"):
            continue
        # its ARM translation (waydroid_script's) is in its overlay, made for stock's Android
        if k in armtrans.ALL_PROPS and (arm or kind == "none" or not stock_way):
            continue
        out[k] = v
    return out


def write_props(inst, session, arm, render=("gpu", None)):
    """Generate waydroid_base.prop and waydroid.prop for this start. arm: the mounted ARM
    translation layer (None: off, or none of ours: then stock's own stays on stock's Android,
    the image's own elsewhere). render: see render_for."""
    # Effective config: stock [properties], then ours (the image's needs), then the instance's own
    eff = configparser.ConfigParser(interpolation=None)
    eff.read_dict(inst.cfg)
    mode, node = render
    kind = inst.get("arm_translation")
    eff["properties"] = stock_props(inst, arm, mode)
    if arm:
        eff["properties"].update(armtrans.PROPS[kind])
    elif kind == "none":
        eff["properties"]["ro.dalvik.vm.native.bridge"] = "0"   # also switches off the image's own
    key = android_of(inst)
    if key:
        eff["properties"].update(catalog.get(key, "props", {}))
    if mode == "gpu" and node:
        eff["waydroid"]["drm_device"] = node        # stock's props follow the picked GPU
    if mode == "vkms":
        eff["properties"].update(gpu.VKMS_PROPS)
        eff["properties"].update(catalog.get(key, "software_props", {}))
    elif mode == "software":
        eff["properties"].update(gpu.SOFTWARE_PROPS)
    elif mode == "gpu" and gpu.nvidia_display():
        # The desktop can't import this GPU's buffers, so the window proxy shows linear ones as
        # shared memory: minigbm_gbm_mesa allocates linear when NVIDIA's layouts are all the
        # desktop offers besides it (the hwcomposer publishes them as waydroid.modifiers.*)
        eff["properties"].update(gpu.OTHER_GPU_PROPS)
    elif mode == "nvidia":
        eff["properties"].update(gpu.NVIDIA_PROPS)
        eff["properties"]["ro.hardware.gralloc"] = nvidia.GRALLOC[catalog.get(key, "nvidia")]
        eff["properties"].update(catalog.get(key, "nvidia_props", {}))
    if node:
        eff["properties"]["gralloc.gbm.device"] = node
    for k, v in inst.cfg["properties"].items():
        if inst.owner_uid != 0 and PROTECTED_PROP_RE.match(k):
            log.warning("%s: ignoring property %s (only root may set it)", inst.id, k)
            continue
        eff["properties"][k] = v
    eff_path = os.path.join(inst.dir, "effective.cfg")
    with open(eff_path, "w") as f:
        eff.write(f)
    a = stock_args(inst, eff_path)
    t = stock.tools()
    t.helpers.lxc.make_base_props(a)
    base = os.path.join(inst.dir, "waydroid_base.prop")
    with open(base) as f:
        props = [p for p in f.read().splitlines()
                 if not p.startswith(("waydroid.system_ota=", "waydroid.vendor_ota=", "waydroid.updater.disabled="))]
    props.append("waydroid.updater.disabled=true")
    _write(base, "\n".join(one_per_key(props)) + "\n")

    full = os.path.join(inst.dir, "waydroid.prop")
    t.helpers.images.make_prop(a, session, full)
    extra = ["waydroid.manager.instance=" + inst.id]
    # Device model: ro.product.waydroid.* win in Android's product property source order
    for k, v in devices.props_for(inst.get("device_model"), inst.cfg["properties"]).items():
        extra.append("{}={}".format(k, v))
    if inst.get("width") != "0":
        extra.append("persist.waydroid.width=" + inst.get("width"))
    if inst.get("height") != "0":
        extra.append("persist.waydroid.height=" + inst.get("height"))
    if host_timezone():
        extra.append("persist.sys.timezone=" + host_timezone())
    with open(full) as f:
        lines = f.read().splitlines()
    _write(full, "\n".join(one_per_key(lines + extra)) + "\n")
    bind_file(full, inst.rootfs + "/vendor/waydroid.prop")


# The renderers #0 shares with stock Waydroid's own rendering (its pick of GPU, or software)
STOCK_GRAPHICS = ("gpu auto", "software")


def graphics_key(inst, mode):
    """Which renderer the apps' shader caches of this start come from."""
    return "gpu " + inst.get("gpu") if mode == "gpu" else mode


def start(inst, net, hosts, session_in, uid, in_use, cpus_busy=list):
    """Bring the container up. session_in: validated dict from the session process.
    in_use: see images.gc. cpus_busy(): the other running or starting instances' (cpus limit,
    pinned CPUs)."""
    pw = pwd.getpwuid(uid)
    if inst.index == 0:
        _stock_stopped()
    images_dir = select_image(inst, in_use)
    if inst.index != 0 and catalog.get(inst.get("android"), "videodev"):
        ensure_videodev()
    if inst.index == 0 and inst.image_id != images.stock_image_id():
        # stock's data must never boot an older Android than stock's own
        raise RuntimeError("stock Waydroid's images are changing; try again in a minute")
    ensure_dirs(inst)
    a = stock_args(inst)
    binder.ensure_binderfs(a)
    binder.ensure_nodes(a, inst.index)
    render = render_for(inst)
    with _pin_lock:
        write_lxc_config(inst, net, cpus_busy(), render)
    net.ensure_up(hosts)
    set_device_permissions()

    # Sockets: validate the caller's and bind them at root-owned paths
    stage = paths.staging_dir(inst.id)
    umount_tree(stage)
    os.makedirs(stage, mode=0o755, exist_ok=True)
    wl = os.path.join(stage, "wayland-0")
    stage_socket(session_in["wayland_socket"], wl, uid)
    pulse = ""
    if session_in.get("pulse_socket"):
        try:
            pulse = os.path.join(stage, "pulse-native")
            stage_socket(session_in["pulse_socket"], pulse, uid)
        except (OSError, PermissionError) as e:
            log.warning("%s: no audio: %s", inst.id, e)
            pulse = ""
    venus = ""
    if render[0] == "nvidia":
        venus = os.path.join(stage, "venus")
        stage_dir(session_in.get("venus_dir", ""), venus, uid)
    _write(os.path.join(inst.lxc_dir, "config_session"),
           lxcconfig.session_entries(wl, pulse, inst.data_dir, venus))

    try:
        if inst.index == 0:
            mount_stock_data(inst)
        # Shader caches of another renderer crash Android 13's apps (storage.clear_shader_caches).
        # #0's data is also stock Waydroid's: unless it renders as stock does, they go each time
        key = graphics_key(inst, render[0])
        if inst.cfg["instance"].get("shader_caches") != key or inst.index == 0 and key not in STOCK_GRAPHICS:
            storage.clear_shader_caches(inst.data_dir)
            inst.cfg["instance"]["shader_caches"] = key
            inst.save()
        guest = nvidia.guest_layers(catalog.get(android_of(inst), "nvidia")) if render[0] == "nvidia" else []
        arm = mount_rootfs(inst, images_dir, guest)
        detect_protocols(inst)
        session = {
            "user_name": pw.pw_name, "user_id": str(uid), "group_id": str(pw.pw_gid),
            "waydroid_data": inst.data_dir,
            "background_start": "true" if session_in.get("background_start") == "true" else "false",
            "lcd_density": inst.get("dpi"),
        }
        write_props(inst, session, arm, render)
        r = run(["lxc-start", "-P", paths.LXC_PATH, "-n", inst.container, "-d",
                 "-o", os.path.join(inst.dir, "container.log"), "-l", "NOTICE", "--", "/init"], check=False)
        if r.returncode != 0:
            raise CommandError("lxc-start failed: " + (r.stderr or r.stdout).strip()[-500:])
        run(["lxc-wait", "-P", paths.LXC_PATH, "-n", inst.container, "-s", "RUNNING", "-t", "15"], check=False)
        if lxc_state(inst.id) != "RUNNING":
            raise RuntimeError("container failed to start; see {}".format(os.path.join(inst.dir, "container.log")))
        net.isolate(inst.veth)
    except Exception:
        cleanup(inst)
        raise


def stop(inst, keep_stock=False):
    if inst.index == 0 and lxc_state(inst.id) in ("RUNNING", "FROZEN"):
        # stock Waydroid's window doesn't turn with Android: pin its display again (watch_rotation)
        try:
            unfreeze(inst)   # the restore command must finish before frozen #0 is killed too
            attach(inst.id, ["/system/bin/sh", "-c", "wm fixed-to-user-rotation default 2>/dev/null || "
                             "wm set-fix-to-user-rotation enabled"], check=False, timeout=10,
                   env={"PATH": "/system/bin:/system/xbin:/vendor/bin"})
        except CommandError as e:
            log.warning("%s: rotation not pinned for stock Waydroid: %s", inst.id, e)
    if lxc_state(inst.id) != "STOPPED":
        run(["lxc-stop", "-P", paths.LXC_PATH, "-n", inst.container, "-k"], check=False)
        run(["lxc-wait", "-P", paths.LXC_PATH, "-n", inst.container, "-s", "STOPPED", "-t", "15"], check=False)
    cleanup(inst, keep_stock)


def cleanup(inst, keep_stock=False):
    """Unmount everything belonging to a stopped instance. #0 gives stock Waydroid its data
    back, unless keep_stock (a reboot) or its container is somehow still up."""
    umount_tree(inst.rootfs)
    umount_tree(paths.staging_dir(inst.id))
    if inst.index == 0:
        if lxc_state(inst.id) != "STOPPED":
            log.error("%s: the container didn't stop; stock Waydroid stays paused", inst.id)
            return
        if inst.cfg["instance"].get("shader_caches") not in STOCK_GRAPHICS and os.path.ismount(inst.data_dir):
            storage.clear_shader_caches(inst.data_dir)      # stock Waydroid starts on its own
        umount_tree(inst.data_dir)
        if not keep_stock:
            release_stock()


def freeze(inst):
    if lxc_state(inst.id) == "RUNNING":
        run(["lxc-freeze", "-P", paths.LXC_PATH, "-n", inst.container])
        run(["lxc-wait", "-P", paths.LXC_PATH, "-n", inst.container, "-s", "FROZEN", "-t", "10"], check=False)


def unfreeze(inst):
    if lxc_state(inst.id) == "FROZEN":
        run(["lxc-unfreeze", "-P", paths.LXC_PATH, "-n", inst.container])
        run(["lxc-wait", "-P", paths.LXC_PATH, "-n", inst.container, "-s", "RUNNING", "-t", "10"], check=False)


def wait_boot(inst, timeout=180):
    """Poll sys.boot_completed via lxc-attach (root, no binder)."""
    deadline = time.time() + timeout
    env = {"PATH": "/system/bin:/system/xbin:/vendor/bin", "ANDROID_ROOT": "/system", "ANDROID_DATA": "/data"}
    while time.time() < deadline:
        if lxc_state(inst.id) not in ("RUNNING",):
            return False
        r = attach(inst.id, ["/system/bin/getprop", "sys.boot_completed"], check=False, env=env)
        if r.stdout.strip() == "1":
            return True
        time.sleep(2)
    return False


# Waydroid pins the display to the user rotation (no sensors), so apps wanting the other orientation
# are letterboxed. In the full-UI window (active_apps "Waydroid") let it follow them, like a phone;
# an app's own window (single- or multi-window mode) doesn't turn, so there it stays pinned. Prints
# the rotation (Surface.ROTATION_*) every half second. Android 11 names the setting
# set-fix-to-user-rotation and has no "default" (its own is "enabled").
ROTATION_LOOP = """
last=
while :; do
  m=default old=enabled
  [ "$(getprop waydroid.active_apps)" = Waydroid ] && [ "$(getprop persist.waydroid.multi_windows)" != true ] \\
    && m=disabled old=disabled
  r=$(dumpsys window displays 2>/dev/null | grep -m1 -o 'mRotation=[0-3]')
  if [ -n "$r" ] && [ "$m" != "$last" ]; then
    wm fixed-to-user-rotation $m >/dev/null 2>&1 || wm set-fix-to-user-rotation $old >/dev/null 2>&1
    wm set-ignore-orientation-request false >/dev/null 2>&1
    last=$m
  fi
  [ -n "$r" ] && echo "${r#mRotation=}"
  sleep 0.5
done
"""


def watch_rotation(inst, fd):
    """Write Android's display rotation to fd (the session's pipe to its proxy), a line every half
    second, for as long as the pipe has a reader: across Android reboots too. Runs in a thread."""
    os.set_blocking(fd, False)          # a reader that falls behind loses lines, never blocks us
    gone = select.poll()
    gone.register(fd, select.POLLERR)   # a pipe whose reader closed
    cmd = ["lxc-attach", "-P", paths.LXC_PATH, "-n", inst.container, "--clear-env",
           "--set-var", "PATH=/system/bin:/system/xbin:/vendor/bin", "--", "/system/bin/sh", "-c", ROTATION_LOOP]
    try:
        while not gone.poll(0):
            if lxc_state(inst.id) in ("RUNNING", "FROZEN"):
                # its own pipe: lxc-attach chowns/chmods its stdio, never the caller's fd
                p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL)
                try:
                    for line in p.stdout:
                        try:
                            os.write(fd, line)
                        except BlockingIOError:
                            pass
                except OSError:             # EPIPE: the proxy is gone
                    return
                finally:
                    p.kill()
                    p.wait()
            time.sleep(2)
    finally:
        os.close(fd)


def load(iid):
    return Instance.load(iid)
