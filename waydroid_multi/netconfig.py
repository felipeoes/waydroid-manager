# SPDX-License-Identifier: GPL-3.0-or-later
"""Network parameters for the shared waydroid-multi bridge."""
import configparser
import ipaddress
import json
import re
import subprocess

from . import paths
from .instance import MAX_INDEX

DEFAULTS = {
    "bridge": "wdmulti0",
    "subnet": "192.168.241.0/24",
    "use_nft": "false",
    "isolate_instances": "true",
}


class NetConfig:
    def __init__(self, bridge, subnet, use_nft=False, isolate=True):
        if len(bridge) > 15:
            raise ValueError("bridge name must be at most 15 characters")
        self.bridge = bridge
        self.network = ipaddress.ip_network(subnet, strict=True)
        if self.network.version != 4:
            raise ValueError("only IPv4 subnets are supported")
        if self.network.num_addresses < MAX_INDEX + 16:
            raise ValueError("subnet {} is too small; use a /24 or larger".format(subnet))
        self.use_nft = use_nft
        self.isolate = isolate

    @classmethod
    def load(cls, path=None):
        cfg = configparser.ConfigParser(interpolation=None)
        cfg.read(path or paths.CONFIG_FILE)
        sec = cfg["network"] if "network" in cfg else {}
        get = lambda k: sec.get(k, DEFAULTS[k])  # noqa: E731
        return cls(get("bridge"), get("subnet"),
                   get("use_nft").lower() == "true",
                   get("isolate_instances").lower() == "true")

    @property
    def gateway(self):
        return self.network.network_address + 1

    def ip_for_index(self, index):
        if not 0 <= index <= MAX_INDEX:
            raise ValueError("index out of range")
        return self.network.network_address + 10 + index

    def env(self):
        """Environment for data/waydroid-multi-net.sh."""
        return {
            "WDM_BRIDGE": self.bridge,
            "WDM_ADDR": str(self.gateway),
            "WDM_NETMASK": str(self.network.netmask),
            "WDM_NETWORK": str(self.network),
            "WDM_DHCP_START": str(self.network.network_address),
            "WDM_VARRUN": paths.RUN_DIR,
            "WDM_HOSTSFILE": paths.DHCP_HOSTS_FILE,
            "WDM_LEASEFILE": "/var/lib/misc/dnsmasq.{}.leases".format(self.bridge),
            "WDM_USE_NFT": "true" if self.use_nft else "false",
            "WDM_NFT_TABLE": "waydroid_multi",
        }

    def env_file_text(self):
        return "".join("{}={}\n".format(k, v) for k, v in sorted(self.env().items()))

    def overlapping_routes(self, routes=None):
        """Routes (other than our own bridge) that overlap the subnet."""
        if routes is None:
            try:
                out = subprocess.run(["ip", "-j", "-4", "route", "show", "table", "all"],
                                     capture_output=True, text=True, check=True).stdout
                routes = json.loads(out or "[]")
            except (OSError, subprocess.CalledProcessError, ValueError):
                return []
        bad = []
        for r in routes:
            dst = r.get("dst", "")
            if not dst or dst == "default" or r.get("dev") == self.bridge or r.get("type") in ("local", "broadcast"):
                continue
            try:
                net = ipaddress.ip_network(dst if "/" in dst else dst + "/32", strict=False)
            except ValueError:
                continue
            if net.overlaps(self.network):
                bad.append("{} dev {}".format(dst, r.get("dev", "?")))
        return bad


def dhcp_hosts_text(entries):
    """entries: iterable of (mac, ip) -> dnsmasq --dhcp-hostsfile content."""
    return "".join("{},{}\n".format(mac, ip) for mac, ip in sorted(entries))


HOSTS_BEGIN = "# BEGIN waydroid-multi (instance names for adb; managed, do not edit)"
HOSTS_END = "# END waydroid-multi"
HOSTS_ENTRY_RE = re.compile(r"^[0-9a-fA-F.:]+ waydroid-[a-z0-9-]+$")   # also what uninstall.sh's sed removes


def host_names(instances):
    """[(id, display name)] -> {id: "waydroid-<name>"}, unique (a clash gets "-<id>")."""
    names, used = {}, set()
    for iid, name in sorted(instances, key=lambda i: int(i[0])):
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40].strip("-")
        host = "waydroid-" + (slug or iid)
        while host in used:
            host += "-" + iid
        used.add(host)
        names[iid] = host
    return names


def etc_hosts_text(text, entries):
    """/etc/hosts with our block replaced by entries [(ip, host)] (removed when there are none).
    Lines outside the block are kept as they are."""
    lines = text.splitlines()
    if HOSTS_BEGIN not in lines and not entries:
        return text
    while HOSTS_BEGIN in lines:
        start = end = lines.index(HOSTS_BEGIN)
        if HOSTS_END in lines[start:]:
            end = lines.index(HOSTS_END, start)
        else:   # END was deleted: take only our entries, never the user's lines after them
            while end + 1 < len(lines) and HOSTS_ENTRY_RE.match(lines[end + 1]):
                end += 1
        if start and not lines[start - 1].strip():
            start -= 1   # the blank line we put before the block
        del lines[start:end + 1]
    if entries:
        if lines and lines[-1].strip():
            lines.append("")
        lines += [HOSTS_BEGIN] + ["{} {}".format(ip, host) for ip, host in sorted(entries)] + [HOSTS_END]
    return "\n".join(lines) + "\n" if lines else ""
