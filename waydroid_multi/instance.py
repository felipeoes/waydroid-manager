# SPDX-License-Identifier: GPL-3.0-or-later
"""Instance model: identity, settings schema and the on-disk instance.cfg.

instance.cfg uses the same format as stock waydroid.cfg so the stock helpers
can read it through ``args.config``:

  [waydroid]    stock-compatible keys (binder nodes, protocols, images, ...)
  [properties]  per-instance Android property overrides
  [instance]    waydroid-multi settings (kept out of [waydroid] because the
                stock config loader deletes unknown path-like keys there)
"""
import configparser
import os
import re
import time

from . import devices, paths

ID_RE = re.compile(r"^(0|[1-9][0-9]{0,2})$")        # instance ids are their numbers (#0: stock Waydroid)
LEGACY_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,30}$")  # 0.1 slug ids, migrated at daemon start
PROP_KEY_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,96}$")
# Android properties only root may set. They would give the instance owner root inside
# Android (adb root, a debuggable build), and Android runs in a privileged container
# (as with stock Waydroid), so root there is close to root on the host.
PROTECTED_PROP_RE = re.compile(
    r"^(ro\.debuggable|ro\.force\.debuggable|ro\.secure|ro\.adb\..*|service\.adb\..*|persist\.adb\..*"
    r"|persist\.service\.adb\..*|persist\.sys\.root_access|ro\.boot\..*|ro\.bootmode|ro\.kernel\..*"
    r"|ctl\..*|sys\.powerctl|selinux\..*|ro\.build\.selinux|security\..*)$")
CPUSET_RE = re.compile(r"^[0-9]+(-[0-9]+)?(,[0-9]+(-[0-9]+)?)*$")
MEM_RE = re.compile(r"^[0-9]+[KMG]?$")
MAX_INDEX = 240
ACTIONS = ("stop", "freeze", "none")


def validate_id(iid, legacy=False):
    iid = "" if iid is None else str(iid)
    if ID_RE.match(iid) and int(iid) <= MAX_INDEX:
        return iid
    if legacy and LEGACY_ID_RE.match(iid) and iid != "default":
        return iid
    raise ValueError("invalid instance id '{}': instances are numbered 0-{}".format(iid, MAX_INDEX))


def host_memory_bytes():
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return 8 * 1024 ** 3


def default_cpus():
    return str(min(2, os.cpu_count() or 2))


def default_memory():
    # 4 GB per instance, but small hosts (<= 6 GB RAM) get 2 GB
    return "4G" if host_memory_bytes() > 6 * 1024 ** 3 else "2G"


def _bool(v):
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return "true"
    if s in ("0", "false", "no", "off"):
        return "false"
    raise ValueError("expected a boolean, got '{}'".format(v))


def _uint(maxv):
    def f(v):
        n = int(str(v).strip())
        if n < 0 or n > maxv:
            raise ValueError("expected 0..{}, got {}".format(maxv, n))
        return str(n)
    return f


def _cpus(v):
    s = str(v).strip()
    try:
        n = float(s)
    except ValueError:
        raise ValueError("cpus must be a number of cores, e.g. 2")
    if n < 0.25 or n > 1024:
        raise ValueError("cpus must be at least 0.25 cores")
    return ("%g" % n)


def _cpuset(v):
    s = str(v).strip().lower()
    if s and s != "all" and not CPUSET_RE.match(s):
        raise ValueError("cpuset must look like 0-3,8, or be all")
    return s


def _memory(v):
    s = str(v).strip().upper()
    if s.endswith("B"):
        s = s[:-1]
    if not MEM_RE.match(s):
        raise ValueError("memory must look like 4G or 3072M")
    mult = {"K": 1024, "M": 1024 ** 2, "G": 1024 ** 3}.get(s[-1], 1)
    if int(s.rstrip("KMG")) * mult < 512 * 1024 ** 2:
        raise ValueError("memory must be at least 512M")
    return s


def _zoom(v):
    s = str(v).strip().lower().rstrip("%")
    if s in ("", "auto"):
        return "auto"
    n = int(round(float(s)))
    if n < 25 or n > 200:
        raise ValueError("zoom must be auto or 25-200 (%)")
    return str(n)


def _device(v):
    s = str(v).strip()
    if s not in devices.PRESETS:
        raise ValueError("unknown device model '{}' (see 'waydroid-multi devices')".format(s))
    return s


def _action(v):
    s = str(v).strip().lower()
    if s not in ACTIONS:
        raise ValueError("expected one of {}".format(", ".join(ACTIONS)))
    return s


def _name(v):
    s = str(v).strip()
    if not s or len(s) > 64 or any(c in s for c in "\n\r\t"):
        raise ValueError("display name must be 1-64 printable characters")
    return s


# User-editable settings: key -> (validator, default, description).
# A callable default is evaluated when read (host-dependent defaults).
SETTINGS = {
    "name": (_name, None, "display name"),
    "width": (_uint(16384), "1280", "Android screen width in pixels"),
    "height": (_uint(16384), "720", "Android screen height in pixels"),
    "dpi": (_uint(1000), "240", "screen density"),
    "cpus": (_cpus, default_cpus, "CPU limit in cores, e.g. 2"),
    "cpuset": (_cpuset, "", "pin to host CPUs, e.g. 0-3; all = not pinned; empty = as many as cpus, the "
               "least used at start (unless cpus covers every host CPU)"),
    "memory": (_memory, default_memory, "memory limit, e.g. 4G"),
    "device_model": (_device, "waydroid", "device model preset (see 'waydroid-multi devices')"),
    "zoom": (_zoom, "auto", "window zoom in % (25-200) or auto (fit the screen)"),
    "close_action": (_action, "stop", "what closing the window does: stop|freeze|none"),
    "idle_action": (_action, "freeze", "what Android idle-suspend does: freeze|stop|none"),
    "window_labels": (_bool, "true", "label windows per instance (Wayland proxy)"),
    "window_frame": (_bool, "true", "title bar, toolbar and resizing (needs window_labels)"),
    "root": (_bool, "false", "Root: Magisk Delta in the instance (restart)"),
    "system_writable": (_bool, "false", "Android system partition is writable (restart)"),
    "desktop_apps": (_bool, "false", "create desktop entries for this instance's apps"),
}

# Settings that only take effect at the next start
RESTART_SETTINGS = {"width", "height", "dpi", "cpus", "cpuset", "memory", "device_model",
                    "window_labels", "window_frame", "system_writable", "root"}


def setting_default(key):
    d = SETTINGS[key][1]
    return d() if callable(d) else d


def validate_setting(key, value):
    if key not in SETTINGS:
        raise ValueError("unknown setting '{}' (known: {})".format(key, ", ".join(sorted(SETTINGS))))
    return SETTINGS[key][0](value)


def validate_prop(key, value, trusted=False):
    """trusted: the caller is root (may set protected properties)."""
    if not PROP_KEY_RE.match(key):
        raise ValueError("invalid property name '{}'".format(key))
    if not trusted and PROTECTED_PROP_RE.match(key):
        raise ValueError("property '{}' can only be set by root".format(key))
    # no '%': stock Waydroid's config parser would read it as an interpolation
    if any(c in value for c in "\n\r\0%"):
        raise ValueError("property values must be single-line, without '%'")
    return value


def mac_for_index(index):
    # Locally administered, unicast ("WDM" in the middle bytes); :00:00 is the bridge's
    if index == 0:
        return "02:57:44:4d:ff:00"
    return "02:57:44:4d:{:02x}:{:02x}".format(index // 256, index % 256)


def veth_for_index(index):
    return "wdm{}v".format(index)


def binder_nodes(index):
    return {k: "wdm{}-{}".format(index, k) for k in ("binder", "vndbinder", "hwbinder")}


class Instance:
    """An instance as stored in /var/lib/waydroid-multi/instances/<id>/instance.cfg."""

    def __init__(self, iid, cfg=None, legacy=False):
        self.id = validate_id(iid, legacy=legacy)
        self.cfg = cfg or configparser.ConfigParser(interpolation=None)
        for sec in ("waydroid", "properties", "instance"):
            if sec not in self.cfg:
                self.cfg[sec] = {}

    # -- paths --------------------------------------------------------------
    @property
    def dir(self):
        return paths.instance_dir(self.id)

    @property
    def cfg_path(self):
        return os.path.join(self.dir, "instance.cfg")

    @property
    def data_dir(self):
        return os.path.join(self.dir, "data")

    @property
    def rootfs(self):
        return os.path.join(self.dir, "rootfs")

    @property
    def container(self):
        return paths.container_name(self.id)

    @property
    def lxc_dir(self):
        return paths.lxc_dir(self.id)

    # -- identity -----------------------------------------------------------
    @property
    def index(self):
        return int(self.cfg["instance"]["index"])

    @property
    def owner_uid(self):
        return int(self.cfg["instance"]["owner_uid"])

    @property
    def name(self):
        return self.cfg["instance"].get("name") or "Instance {}".format(self.id)

    @property
    def mac(self):
        return self.cfg["instance"].get("mac") or mac_for_index(self.index)

    @property
    def veth(self):
        return veth_for_index(self.index)

    @property
    def image_id(self):
        return self.cfg["instance"].get("image_id", "")

    def get(self, key):
        if key == "name":
            return self.name
        if key in SETTINGS:
            # empty values from older versions ("unlimited") read as the default
            return self.cfg["instance"].get(key) or setting_default(key)
        return self.cfg["instance"].get(key, "")

    def getbool(self, key):
        return self.get(key) == "true"

    def set(self, key, value):
        self.cfg["instance"][key] = validate_setting(key, value)

    def settings(self):
        return {k: self.get(k) for k in SETTINGS}

    def binder(self, kind="binder"):
        """Binder node relative to /dev, as stored in [waydroid]."""
        return self.cfg["waydroid"][kind]

    # -- persistence --------------------------------------------------------
    @classmethod
    def new(cls, iid, index, owner_uid, image_id, waydroid_section, properties):
        inst = cls(iid)
        w = inst.cfg["waydroid"]
        for k, v in waydroid_section.items():
            w[k] = v
        for kind, node in binder_nodes(index).items():
            w[kind] = "binderfs/" + node
        w["images_path"] = os.path.join(paths.IMAGES_DIR, image_id) if image_id else w.get("images_path", "")
        for k, v in properties.items():
            inst.cfg["properties"][k] = v
        i = inst.cfg["instance"]
        i["index"] = str(index)
        i["owner_uid"] = str(owner_uid)
        i["created"] = str(int(time.time()))
        i["image_id"] = image_id
        i["mac"] = mac_for_index(index)
        return inst

    @classmethod
    def load(cls, iid, path=None, legacy=False):
        cfg = configparser.ConfigParser(interpolation=None)
        p = path or os.path.join(paths.instance_dir(validate_id(iid, legacy=legacy)), "instance.cfg")
        if not cfg.read(p):
            raise FileNotFoundError("instance #{} does not exist".format(iid))
        return cls(iid, cfg, legacy=legacy)

    def save(self, path=None):
        p = path or self.cfg_path
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w") as f:
            self.cfg.write(f)
        os.chmod(tmp, 0o644)  # the CLI reads binder names/protocols from it
        os.replace(tmp, p)

    def info(self):
        """Flat string dict for D-Bus / CLI display."""
        d = {"id": self.id, "index": str(self.index), "owner_uid": str(self.owner_uid),
             "mac": self.mac, "image_id": self.image_id,
             "binder": self.binder("binder"),
             "binder_protocol": self.cfg["waydroid"].get("binder_protocol", ""),
             "service_manager_protocol": self.cfg["waydroid"].get("service_manager_protocol", "")}
        d.update(self.settings())
        for k, v in self.cfg["properties"].items():
            d["prop:" + k] = v
        return d


def _dirs():
    try:
        return os.listdir(paths.INSTANCES_DIR)
    except FileNotFoundError:
        return []


def list_ids():
    ids = [n for n in _dirs() if ID_RE.match(n) and os.path.isfile(os.path.join(paths.INSTANCES_DIR, n, "instance.cfg"))]
    return sorted(ids, key=int)


def legacy_ids():
    """Instance dirs from 0.1 that still use a name as their id."""
    return sorted(n for n in _dirs() if not ID_RE.match(n) and LEGACY_ID_RE.match(n)
                  and os.path.isfile(os.path.join(paths.INSTANCES_DIR, n, "instance.cfg")))
