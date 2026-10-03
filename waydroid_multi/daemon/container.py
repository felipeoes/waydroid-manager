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
import shutil
import time

from .. import devices, lxcconfig, paths, stock
from ..instance import PROTECTED_PROP_RE, Instance
from . import binder, images
from .util import (CommandError, apparmor_profile_loaded, attach, bind, bind_file, chown_tree_top,
                   is_mount, log, lxc_state, mount_image, mount_overlay, run, stage_socket, umount_tree)

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


def select_image(inst, in_use=()):
    """Make the instance use the current image set; returns its directory."""
    cur = images.ensure_synced(in_use)
    if inst.image_id != cur:
        if inst.image_id:
            log.info("%s: switching image set %s -> %s", inst.id, inst.image_id, cur)
        wipe_overlay_rw(inst)
        inst.cfg["instance"]["image_id"] = cur
        inst.cfg["waydroid"]["images_path"] = images.image_dir(cur)
        stock_cfg = stock.load_stock_cfg()["waydroid"]
        for k in ("system_datetime", "vendor_datetime", "system_ota", "vendor_ota"):
            if k in stock_cfg:
                inst.cfg["waydroid"][k] = stock_cfg[k]
        inst.save()
    d = images.image_dir(cur)
    if not os.path.isfile(os.path.join(d, "system.img")):
        raise RuntimeError("image set {} is missing; run 'waydroid-multi images sync'".format(cur))
    return d


def write_lxc_config(inst, net):
    a = stock_args(inst)
    limits = lxcconfig.cgroup_limits(inst.get("cpus"), inst.get("cpuset"), inst.get("memory"))
    text = lxcconfig.build_config(
        stock.lxc_snippets(),
        rootfs=inst.rootfs, lxc_dir=inst.lxc_dir, bridge=net.cfg.bridge, mac=inst.mac,
        veth=inst.veth, uts_name="waydroid-" + inst.id.replace("_", "-"), arch=platform.machine(),
        apparmor_profile=stock.tools().helpers.lxc.LXC_APPARMOR_PROFILE
        if apparmor_profile_loaded(stock.tools().helpers.lxc.LXC_APPARMOR_PROFILE) else None,
        poststop_hook=paths.POSTSTOP_SCRIPT,
        netup_hook=paths.NET_UP_SCRIPT if net.cfg.isolate else None, limits=limits)
    _write(os.path.join(inst.lxc_dir, "config"), text)
    shutil.copy(stock.seccomp_profile(), os.path.join(inst.lxc_dir, "waydroid.seccomp"))
    nodes = stock.tools().helpers.lxc.generate_nodes_lxc_config(a)
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


def mount_rootfs(inst, images_dir):
    """system.img + overlays, vendor.img + overlays, like stock mount_rootfs."""
    rootfs = inst.rootfs
    umount_tree(rootfs)
    mount_image(os.path.join(images_dir, "system.img"), rootfs)
    lowers = [os.path.join(inst.dir, "overlay")]
    if os.path.isdir(paths.STOCK_OVERLAY):
        lowers.append(paths.STOCK_OVERLAY)
    mount_overlay(lowers + [rootfs], rootfs, os.path.join(inst.dir, "overlay_rw/system"),
                  os.path.join(inst.dir, "overlay_work/system"))
    mount_image(os.path.join(images_dir, "vendor.img"), rootfs + "/vendor")
    vlowers = [os.path.join(inst.dir, "overlay/vendor")]
    if os.path.isdir(paths.STOCK_OVERLAY + "/vendor"):
        vlowers.append(paths.STOCK_OVERLAY + "/vendor")
    mount_overlay(vlowers + [rootfs + "/vendor"], rootfs + "/vendor",
                  os.path.join(inst.dir, "overlay_rw/vendor"), os.path.join(inst.dir, "overlay_work/vendor"))
    for egl_path in ("/vendor/lib/egl", "/vendor/lib64/egl"):
        if os.path.isdir(egl_path):
            bind(egl_path, rootfs + egl_path)
    if is_mount("/odm"):
        bind("/odm", rootfs + "/odm_extra")
    elif os.path.isdir("/vendor/odm"):
        bind("/vendor/odm", rootfs + "/odm_extra")


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


def write_props(inst, session):
    """Generate waydroid_base.prop and waydroid.prop for this start."""
    # Effective config: stock [properties] overridden by the instance's own
    eff = configparser.ConfigParser(interpolation=None)
    eff.read_dict(inst.cfg)
    stock_props = stock.load_stock_cfg()["properties"]
    eff["properties"] = {}
    for k, v in stock_props.items():
        eff["properties"][k] = v
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
    _write(base, "\n".join(props) + "\n")

    full = os.path.join(inst.dir, "waydroid.prop")
    t.helpers.images.make_prop(a, session, full)
    extra = ["waydroid.multi.instance=" + inst.id]
    # Device model: ro.product.waydroid.* win in Android's product property source order
    for k, v in devices.props_for(inst.get("device_model"), inst.cfg["properties"]).items():
        extra.append("{}={}".format(k, v))
    if inst.get("width") != "0":
        extra.append("persist.waydroid.width=" + inst.get("width"))
    if inst.get("height") != "0":
        extra.append("persist.waydroid.height=" + inst.get("height"))
    with open(full, "a") as f:
        f.write("\n".join(extra) + "\n")
    bind_file(full, inst.rootfs + "/vendor/waydroid.prop")


def start(inst, net, hosts, session_in, uid, images_in_use=()):
    """Bring the container up. session_in: validated dict from the session process."""
    pw = pwd.getpwuid(uid)
    images_dir = select_image(inst, images_in_use)
    ensure_dirs(inst)
    a = stock_args(inst)
    binder.ensure_binderfs(a)
    binder.ensure_nodes(a, inst.index)
    write_lxc_config(inst, net)
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
    _write(os.path.join(inst.lxc_dir, "config_session"),
           lxcconfig.session_entries(wl, pulse, inst.data_dir))

    try:
        mount_rootfs(inst, images_dir)
        detect_protocols(inst)
        session = {
            "user_name": pw.pw_name, "user_id": str(uid), "group_id": str(pw.pw_gid),
            "waydroid_data": inst.data_dir,
            "background_start": "true" if session_in.get("background_start") == "true" else "false",
            "lcd_density": inst.get("dpi"),
        }
        write_props(inst, session)
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


def stop(inst):
    if lxc_state(inst.id) != "STOPPED":
        run(["lxc-stop", "-P", paths.LXC_PATH, "-n", inst.container, "-k"], check=False)
        run(["lxc-wait", "-P", paths.LXC_PATH, "-n", inst.container, "-s", "STOPPED", "-t", "15"], check=False)
    cleanup(inst)


def cleanup(inst):
    """Unmount everything belonging to a stopped instance."""
    umount_tree(inst.rootfs)
    umount_tree(paths.staging_dir(inst.id))


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


def load(iid):
    return Instance.load(iid)
