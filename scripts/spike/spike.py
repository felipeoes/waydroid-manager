#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Feasibility spike: bring up an extra Waydroid instance next to the stock one.

Run as root:
  spike.py images                         copy stock images into the image store
  spike.py up NAME INDEX --uid UID [--socket PATH] [--width W --height H] [--memory-high 4G --cpus 0-3]
  spike.py down NAME
  spike.py status NAME
  spike.py net-up | net-down
"""
import argparse
import configparser
import glob
import os
import platform
import pwd
import re
import shutil
import subprocess
import sys
import time

STOCK = os.path.dirname(os.path.realpath(shutil.which("waydroid") or "/usr/lib/waydroid/waydroid.py"))
sys.path.insert(0, STOCK)
import tools  # noqa: E402  (stock waydroid package)
import tools.config  # noqa: E402
import tools.helpers  # noqa: E402
import logging  # noqa: E402
tools.helpers.logging.add_verbose_log_level()
logging.basicConfig(level=logging.INFO)

ROOT = "/var/lib/waydroid-multi"
LXCPATH = ROOT + "/lxc"
IMAGES = ROOT + "/images"
RUN = "/run/waydroid-multi"
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
NETSH = REPO + "/data/waydroid-multi-net.sh"
BRIDGE = "wdmulti0"
SUBNET_PREFIX = "192.168.241."


def sh(*cmd, check=True, **kw):
    print("+", " ".join(cmd))
    return subprocess.run(cmd, check=check, **kw)


def stock_args(work, config):
    a = argparse.Namespace()
    a.cache = {}
    a.work = work
    a.config = config
    a.log = work + "/spike.log"
    a.sudo_timer = False
    a.timeout = 1800
    a.details_to_stdout = True
    a.verbose = False
    a.quiet = False
    return a


def stock_cfg():
    cfg = configparser.ConfigParser()
    cfg.read("/var/lib/waydroid/waydroid.cfg")
    return cfg


def image_id(cfg):
    return "{}-{}".format(cfg["waydroid"]["system_datetime"], cfg["waydroid"]["vendor_datetime"])


def cmd_images(_):
    cfg = stock_cfg()
    dst = os.path.join(IMAGES, image_id(cfg))
    if os.path.isdir(dst):
        print("image store already has", dst)
        return
    os.makedirs(dst + ".tmp", exist_ok=True)
    for f in ("system.img", "vendor.img"):
        sh("cp", "--sparse=always", os.path.join(cfg["waydroid"]["images_path"], f), dst + ".tmp/" + f)
    os.rename(dst + ".tmp", dst)
    print("images ->", dst)


def net_env():
    env = dict(os.environ)
    env.update({"WDM_BRIDGE": BRIDGE, "WDM_ADDR": SUBNET_PREFIX + "1",
                "WDM_NETWORK": SUBNET_PREFIX + "0/24", "WDM_DHCP_START": SUBNET_PREFIX + "0"})
    return env


def cmd_net_up(_):
    sh("sh", NETSH, "setup", env=net_env())
    if subprocess.run(["systemctl", "is-active", "-q", "wdm-spike-dnsmasq"]).returncode != 0:
        sh("systemd-run", "--unit=wdm-spike-dnsmasq", "--collect",
           "--setenv=WDM_BRIDGE=" + BRIDGE, "--setenv=WDM_ADDR=" + SUBNET_PREFIX + "1",
           "--setenv=WDM_NETWORK=" + SUBNET_PREFIX + "0/24", "--setenv=WDM_DHCP_START=" + SUBNET_PREFIX + "0",
           "sh", NETSH, "run-dnsmasq")


def cmd_net_down(_):
    sh("systemctl", "stop", "wdm-spike-dnsmasq", check=False)
    sh("sh", NETSH, "teardown", env=net_env(), check=False)


def mac_for(index):
    return "02:57:44:4d:{:02x}:{:02x}".format(index // 256, index % 256)


def add_dhcp_host(index):
    hosts = RUN + "/dhcp-hosts"
    os.makedirs(RUN, exist_ok=True)
    lines = []
    if os.path.exists(hosts):
        lines = [l for l in open(hosts).read().splitlines() if l and not l.startswith(mac_for(index))]
    lines.append("{},{}{}".format(mac_for(index), SUBNET_PREFIX, 10 + index))
    with open(hosts, "w") as f:
        f.write("\n".join(lines) + "\n")
    sh("sh", NETSH, "reload", env=net_env(), check=False)


def gen_lxc_config(name, index, inst):
    lxc_dir = os.path.join(LXCPATH, "wdm-" + name)
    os.makedirs(lxc_dir, exist_ok=True)
    cfgdir = os.path.join(tools.config.tools_src, "data/configs")
    lxc_ver = int(subprocess.run(["lxc-info", "--version"], capture_output=True, text=True).stdout[0])
    parts = [open(cfgdir + "/config_base").read()]
    for ver in range(3, 5):
        p = "{}/config_{}".format(cfgdir, ver)
        if lxc_ver >= ver and os.path.exists(p):
            parts.append(open(p).read())
    text = "\n".join(parts)
    lines = []
    for line in text.splitlines():
        key = line.split("=", 1)[0].strip()
        val = line.split("=", 1)[1].strip() if "=" in line else ""
        if key == "lxc.rootfs.path":
            line = "lxc.rootfs.path = " + inst + "/rootfs"
        elif key == "lxc.include":
            line = "lxc.include = " + os.path.join(lxc_dir, os.path.basename(val))
        elif key == "lxc.seccomp.profile":
            line = "lxc.seccomp.profile = " + lxc_dir + "/waydroid.seccomp"
        elif key == "lxc.net.0.link":
            line = "lxc.net.0.link = " + BRIDGE
        elif key == "lxc.net.0.hwaddr":
            line = "lxc.net.0.hwaddr = " + mac_for(index)
        elif key == "lxc.uts.name":
            line = "lxc.uts.name = waydroid-" + name
        elif key in ("lxc.apparmor.profile", "lxc.aa_profile"):
            if tools.helpers.lxc.get_apparmor_status(stock_args(inst, inst + "/instance.cfg")):
                line = key + " = " + tools.helpers.lxc.LXC_APPARMOR_PROFILE
        elif key == "lxc.hook.post-stop" and "LXCPOSTSTOP" in val:
            line = "lxc.hook.post-stop = /dev/null"
        line = line.replace("LXCARCH", platform.machine())
        lines.append(line)
    lines.append("lxc.net.0.veth.pair = wdm{}v".format(index))
    lines.append("lxc.net.0.script.up = " + REPO + "/scripts/spike/isolate-port.sh")
    return lxc_dir, lines


def mount_rootfs(a, inst, images_dir, cfg):
    m = tools.helpers.mount
    rootfs = inst + "/rootfs"
    m.mount(a, images_dir + "/system.img", rootfs, umount=True)
    lowers = [inst + "/overlay", tools.config.defaults["overlay"], rootfs]
    m.mount_overlay(a, [d for d in lowers if os.path.isdir(d)], rootfs,
                    upper_dir=inst + "/overlay_rw/system", work_dir=inst + "/overlay_work/system")
    m.mount(a, images_dir + "/vendor.img", rootfs + "/vendor")
    vlowers = [inst + "/overlay/vendor", tools.config.defaults["overlay"] + "/vendor", rootfs + "/vendor"]
    m.mount_overlay(a, [d for d in vlowers if os.path.isdir(d)], rootfs + "/vendor",
                    upper_dir=inst + "/overlay_rw/vendor", work_dir=inst + "/overlay_work/vendor")
    for egl_path in ["/vendor/lib/egl", "/vendor/lib64/egl"]:
        if os.path.isdir(egl_path):
            m.bind(a, egl_path, rootfs + egl_path)


def umount_tree(path):
    path = os.path.realpath(path)
    mps = []
    with open("/proc/mounts") as f:
        for line in f:
            mp = line.split()[1]
            if mp == path or mp.startswith(path + "/"):
                mps.append(mp)
    for mp in sorted(mps, reverse=True):
        sh("umount", mp, check=False)


def sdk_protocols(rootfs):
    sdk = 0
    with open(rootfs + "/system/build.prop") as f:
        for line in f:
            if line.startswith("ro.build.version.sdk="):
                sdk = int(line.split("=", 1)[1])
    if sdk < 28:
        return "aidl", "aidl"
    if sdk < 30:
        return "aidl2", "aidl2"
    if sdk < 31:
        return "aidl3", "aidl3"
    if sdk < 33:
        return "aidl4", "aidl3"
    return "aidl3", "aidl3"


def lxc_state(name):
    r = subprocess.run(["lxc-info", "-P", LXCPATH, "-n", "wdm-" + name, "-sH"], capture_output=True, text=True)
    return r.stdout.strip() or "STOPPED"


def cmd_up(o):
    name, index, uid = o.name, o.index, o.uid
    pw = pwd.getpwuid(uid)
    inst = os.path.join(ROOT, "instances", name)
    for d in ("rootfs", "overlay/vendor", "overlay_rw/system", "overlay_rw/vendor",
              "overlay_work/system", "overlay_work/vendor"):
        os.makedirs(os.path.join(inst, d), exist_ok=True)
    data = inst + "/data"
    if not os.path.isdir(data):
        os.makedirs(data)
        os.chown(data, uid, pw.pw_gid)
        os.chmod(data, 0o771)

    scfg = stock_cfg()
    images_dir = os.path.join(IMAGES, image_id(scfg))
    cfg = configparser.ConfigParser()
    cfg["waydroid"] = dict(scfg["waydroid"])
    cfg["properties"] = dict(scfg["properties"]) if "properties" in scfg else {}
    for k in ("binder", "vndbinder", "hwbinder"):
        cfg["waydroid"][k] = "binderfs/wdm{}-{}".format(index, k)
    cfg["waydroid"]["images_path"] = images_dir
    cfgpath = inst + "/instance.cfg"
    a = stock_args(inst, cfgpath)
    a.vendor_type = cfg["waydroid"]["vendor_type"]
    a.images_path = images_dir
    a.system_ota = cfg["waydroid"]["system_ota"]
    a.vendor_ota = cfg["waydroid"]["vendor_ota"]
    a.BINDER_DRIVER = cfg["waydroid"]["binder"]
    a.VNDBINDER_DRIVER = cfg["waydroid"]["vndbinder"]
    a.HWBINDER_DRIVER = cfg["waydroid"]["hwbinder"]

    # Binder: make sure stock nodes exist first, then add ours to the same binderfs
    tools.helpers.drivers.probeBinderDriver(a)
    nodes = ["wdm{}-{}".format(index, k) for k in ("binder", "vndbinder", "hwbinder")]
    tools.helpers.drivers.allocBinderNodes(a, nodes)
    for n in nodes:
        os.chmod("/dev/binderfs/" + n, 0o666)

    # Network
    cmd_net_up(o)
    add_dhcp_host(index)

    # Rootfs + protocols
    mount_rootfs(a, inst, images_dir, cfg)
    bp, smp = sdk_protocols(inst + "/rootfs")
    cfg["waydroid"]["binder_protocol"] = bp
    cfg["waydroid"]["service_manager_protocol"] = smp
    with open(cfgpath, "w") as f:
        cfg.write(f)
    os.chmod(cfgpath, 0o644)

    # LXC config
    lxc_dir, lines = gen_lxc_config(name, index, inst)
    if o.memory_high:
        lines.append("lxc.cgroup2.memory.high = " + o.memory_high)
    if o.cpus:
        lines.append("lxc.cgroup2.cpuset.cpus = " + o.cpus)
    with open(lxc_dir + "/config", "w") as f:
        f.write("\n".join(lines) + "\n")
    shutil.copy(os.path.join(tools.config.tools_src, "data/configs/waydroid.seccomp"), lxc_dir + "/waydroid.seccomp")
    nodes_cfg = tools.helpers.lxc.generate_nodes_lxc_config(a)
    with open(lxc_dir + "/config_nodes", "w") as f:
        f.write("\n".join(nodes_cfg) + "\n")

    # Session config
    xdg_runtime = "/run/user/{}".format(uid)
    sock = o.socket or os.path.join(xdg_runtime, "wayland-0")
    session = {
        "user_name": pw.pw_name, "user_id": str(uid), "group_id": str(pw.pw_gid),
        "host_user": pw.pw_dir, "pid": str(os.getpid()),
        "xdg_data_home": pw.pw_dir + "/.local/share", "xdg_runtime_dir": xdg_runtime,
        "wayland_display": sock, "pulse_runtime_path": xdg_runtime + "/pulse",
        "state": "STOPPED", "lcd_density": str(o.dpi or 0), "background_start": "false",
        "waydroid_user_state": inst, "waydroid_data": data,
    }
    sess = ["lxc.mount.entry = tmpfs /run/xdg none create=dir 0 0",
            "lxc.mount.entry = {} run/xdg/wayland-0 none rbind,create=file 0 0".format(os.path.realpath(sock)),
            "lxc.mount.entry = {}/native run/xdg/pulse/native none rbind,create=file 0 0".format(session["pulse_runtime_path"]),
            "lxc.mount.entry = {} data none rbind 0 0".format(data)]
    with open(lxc_dir + "/config_session", "w") as f:
        f.write("\n".join(sess) + "\n")

    # Props
    tools.helpers.lxc.make_base_props(a)
    base = inst + "/waydroid_base.prop"
    props = [p for p in open(base).read().splitlines()
             if not p.startswith(("waydroid.system_ota=", "waydroid.vendor_ota="))]
    props.append("waydroid.updater.disabled=true")
    with open(base, "w") as f:
        f.write("\n".join(props) + "\n")
    tools.helpers.images.make_prop(a, session, inst + "/waydroid.prop")
    extra = []
    if o.width:
        extra.append("persist.waydroid.width={}".format(o.width))
    if o.height:
        extra.append("persist.waydroid.height={}".format(o.height))
    if extra:
        with open(inst + "/waydroid.prop", "a") as f:
            f.write("\n".join(extra) + "\n")
    tools.helpers.mount.bind_file(a, inst + "/waydroid.prop", inst + "/rootfs/vendor/waydroid.prop")

    # Device node permissions, as stock set_permissions()
    for p in ["/dev/ashmem", "/dev/sw_sync", "/dev/ion"] + glob.glob("/dev/dri/renderD*") + glob.glob("/dev/dma_heap/*"):
        if os.path.exists(p):
            subprocess.run(["chmod", "777", p], check=False)

    sh("lxc-start", "-P", LXCPATH, "-n", "wdm-" + name, "-d", "-o", inst + "/container.log", "-l", "INFO",
       "--", "/init")
    for _ in range(20):
        if lxc_state(name) == "RUNNING":
            break
        time.sleep(0.5)
    print("state:", lxc_state(name), " ip:", SUBNET_PREFIX + str(10 + index))


def cmd_down(o):
    name = o.name
    inst = os.path.join(ROOT, "instances", name)
    if lxc_state(name) != "STOPPED":
        sh("lxc-stop", "-P", LXCPATH, "-n", "wdm-" + name, "-k", check=False)
    umount_tree(inst + "/rootfs")
    others = [d for d in glob.glob(LXCPATH + "/wdm-*") if lxc_state(os.path.basename(d)[4:]) != "STOPPED"]
    if not others:
        cmd_net_down(o)


def cmd_status(o):
    print(lxc_state(o.name))


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("images")
    sub.add_parser("net-up")
    sub.add_parser("net-down")
    up = sub.add_parser("up")
    up.add_argument("name")
    up.add_argument("index", type=int)
    up.add_argument("--uid", type=int, required=True)
    up.add_argument("--socket")
    up.add_argument("--width", type=int)
    up.add_argument("--height", type=int)
    up.add_argument("--dpi", type=int)
    up.add_argument("--memory-high")
    up.add_argument("--cpus")
    for c in ("down", "status"):
        s = sub.add_parser(c)
        s.add_argument("name")
    o = p.parse_args()
    {"images": cmd_images, "net-up": cmd_net_up, "net-down": cmd_net_down,
     "up": cmd_up, "down": cmd_down, "status": cmd_status}[o.cmd](o)


if __name__ == "__main__":
    main()
