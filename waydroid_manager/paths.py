# SPDX-License-Identifier: GPL-3.0-or-later
"""Filesystem locations and fixed names used by waydroid-manager."""
import os

PKG_DIR = os.path.dirname(os.path.realpath(__file__))
# Repo checkout and installed layout both keep data/ next to the package.
DATA_DIR = os.environ.get("WAYDROID_MANAGER_DATA", os.path.join(os.path.dirname(PKG_DIR), "data"))

STATE_DIR = "/var/lib/waydroid-manager"
INSTANCES_DIR = STATE_DIR + "/instances"
LXC_PATH = STATE_DIR + "/lxc"
IMAGES_DIR = STATE_DIR + "/images"
RUN_DIR = "/run/waydroid-manager"
NET_ENV_FILE = RUN_DIR + "/net.env"
DHCP_HOSTS_FILE = RUN_DIR + "/dhcp-hosts"
CONFIG_FILE = "/etc/waydroid-manager/daemon.conf"

STOCK_WORK = "/var/lib/waydroid"
STOCK_CFG = STOCK_WORK + "/waydroid.cfg"
STOCK_OVERLAY = STOCK_WORK + "/overlay"
STOCK_HOST_PERMS = STOCK_WORK + "/host-permissions"

NET_SCRIPT = os.path.join(DATA_DIR, "waydroid-manager-net.sh")
POSTSTOP_SCRIPT = os.path.join(DATA_DIR, "waydroid-manager-poststop.sh")
NET_UP_SCRIPT = os.path.join(DATA_DIR, "waydroid-manager-netup.sh")


DBUS_NAME = "io.github.waydroidmanager.Manager"
DBUS_PATH = "/io/github/waydroidmanager/Manager"
DBUS_IFACE = "io.github.waydroidmanager.Manager1"


def instance_dir(iid):
    return os.path.join(INSTANCES_DIR, iid)


def container_name(iid):
    return "wdm-" + iid


def lxc_dir(iid):
    return os.path.join(LXC_PATH, container_name(iid))


def staging_dir(iid):
    return os.path.join(RUN_DIR, "instances", iid)


def user_runtime_dir(iid, xdg_runtime_dir=None):
    base = xdg_runtime_dir or os.environ.get("XDG_RUNTIME_DIR") or "/run/user/{}".format(os.getuid())
    return os.path.join(base, "waydroid-manager", iid)


def user_cache_dir():
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "waydroid-manager")


def user_applications_dir():
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "applications")
