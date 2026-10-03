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

from . import paths

ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,30}$")
PROP_KEY_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,96}$")
CPUSET_RE = re.compile(r"^[0-9]+(-[0-9]+)?(,[0-9]+(-[0-9]+)?)*$")
MEM_RE = re.compile(r"^[0-9]+[KMG]?$")
MAX_INDEX = 240
ACTIONS = ("stop", "freeze", "none")


def validate_id(iid):
    if iid == "default" or not ID_RE.match(iid or ""):
        raise ValueError("invalid instance id '{}': use 1-31 chars of a-z, 0-9, _ "
                         "starting with a letter ('default' is reserved)".format(iid))
    return iid


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
    if s in ("", "0"):
        return ""
    n = float(s)
    if n <= 0 or n > 1024:
        raise ValueError("cpus must be a positive number of cores")
    return ("%g" % n)


def _cpuset(v):
    s = str(v).strip()
    if s and not CPUSET_RE.match(s):
        raise ValueError("cpuset must look like 0-3,8")
    return s


def _memory(v):
    s = str(v).strip().upper()
    if s.endswith("B"):
        s = s[:-1]
    if s in ("", "0"):
        return ""
    if not MEM_RE.match(s):
        raise ValueError("memory must look like 4G, 3072M")
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


# User-editable settings: key -> (validator, default, description)
SETTINGS = {
    "name": (_name, None, "display name"),
    "width": (_uint(16384), "0", "window width in pixels (0 = default)"),
    "height": (_uint(16384), "0", "window height in pixels (0 = default)"),
    "dpi": (_uint(1000), "0", "screen density (0 = default)"),
    "cpus": (_cpus, "", "CPU quota in cores, e.g. 2 or 1.5 (empty = unlimited)"),
    "cpuset": (_cpuset, "", "pin to host CPUs, e.g. 0-3 (empty = any)"),
    "memory": (_memory, "", "soft memory limit, e.g. 4G (empty = unlimited)"),
    "close_action": (_action, "stop", "what closing the window does: stop|freeze|none"),
    "idle_action": (_action, "freeze", "what Android idle-suspend does: freeze|stop|none"),
    "window_labels": (_bool, "true", "label windows per instance (Wayland proxy)"),
    "desktop_apps": (_bool, "false", "create desktop entries for this instance's apps"),
}

# Settings that only take effect at the next start
RESTART_SETTINGS = {"width", "height", "dpi", "cpus", "cpuset", "memory", "window_labels"}


def validate_setting(key, value):
    if key not in SETTINGS:
        raise ValueError("unknown setting '{}' (known: {})".format(key, ", ".join(sorted(SETTINGS))))
    return SETTINGS[key][0](value)


def validate_prop(key, value):
    if not PROP_KEY_RE.match(key):
        raise ValueError("invalid property name '{}'".format(key))
    if any(c in value for c in "\n\r\0"):
        raise ValueError("property values must be single-line")
    return value


def mac_for_index(index):
    # Locally administered, unicast ("WDM" in the middle bytes)
    return "02:57:44:4d:{:02x}:{:02x}".format(index // 256, index % 256)


def veth_for_index(index):
    return "wdm{}v".format(index)


def binder_nodes(index):
    return {k: "wdm{}-{}".format(index, k) for k in ("binder", "vndbinder", "hwbinder")}


class Instance:
    """An instance as stored in /var/lib/waydroid-multi/instances/<id>/instance.cfg."""

    def __init__(self, iid, cfg=None):
        self.id = validate_id(iid)
        self.cfg = cfg or configparser.ConfigParser()
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
        return self.cfg["instance"].get("name") or self.id

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
        if key in SETTINGS:
            default = SETTINGS[key][1]
            if key == "name":
                default = self.id
            return self.cfg["instance"].get(key, default)
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
    def load(cls, iid, path=None):
        cfg = configparser.ConfigParser()
        p = path or os.path.join(paths.instance_dir(validate_id(iid)), "instance.cfg")
        if not cfg.read(p):
            raise FileNotFoundError("instance '{}' does not exist".format(iid))
        return cls(iid, cfg)

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


def list_ids():
    try:
        names = sorted(os.listdir(paths.INSTANCES_DIR))
    except FileNotFoundError:
        return []
    return [n for n in names if ID_RE.match(n) and os.path.isfile(os.path.join(paths.INSTANCES_DIR, n, "instance.cfg"))]
