# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure functions that generate an instance's LXC configuration.

The base config is built from the *stock* Waydroid snippets (so new options
upstream adds are picked up automatically) with key-based rewrites of every
value that must be unique per instance. Generation fails closed: if a stock
path or an unexpanded placeholder survives, an error is raised instead of
producing a config that would touch the stock instance.
"""
import collections
import math
import os
import re

PLACEHOLDER_RE = re.compile(r"\bLXC[A-Z_]{2,}\b")
STOCK_PATH = "/var/lib/waydroid/"


class ConfigError(Exception):
    pass


def _split(line):
    if "=" not in line or line.lstrip().startswith("#"):
        return None, None
    key, _, val = line.partition("=")
    return key.strip(), val.strip()


def build_config(snippets, *, rootfs, lxc_dir, bridge, mac, veth, uts_name, arch,
                 apparmor_profile=None, poststop_hook=None, netup_hook=None, limits=()):
    """Return the main LXC config text for one instance.

    snippets: texts of stock data/configs/config_* in load order.
    """
    out = []
    seen = set()
    for line in "\n".join(snippets).splitlines():
        key, val = _split(line)
        if key is None:
            out.append(line.replace("LXCARCH", arch))
            continue
        seen.add(key)
        if key == "lxc.rootfs.path":
            line = "lxc.rootfs.path = " + rootfs
        elif key == "lxc.include":
            line = "lxc.include = " + os.path.join(lxc_dir, os.path.basename(val))
        elif key == "lxc.seccomp.profile":
            line = "lxc.seccomp.profile = " + os.path.join(lxc_dir, "waydroid.seccomp")
        elif key in ("lxc.net.0.link", "lxc.network.link"):
            line = key + " = " + bridge
        elif key in ("lxc.net.0.hwaddr", "lxc.network.hwaddr"):
            line = key + " = " + mac
        elif key in ("lxc.uts.name", "lxc.utsname"):
            line = key + " = " + uts_name
        elif key in ("lxc.apparmor.profile", "lxc.aa_profile"):
            line = key + " = " + (apparmor_profile or "unconfined")
        elif key == "lxc.hook.post-stop":
            line = key + " = " + (poststop_hook or "/dev/null")
        line = line.replace("LXCARCH", arch)
        out.append(line)

    if "lxc.net.0.type" in seen:
        out.append("lxc.net.0.veth.pair = " + veth)
        if netup_hook:
            out.append("lxc.net.0.script.up = " + netup_hook)
    out.extend(limits)
    text = "\n".join(out).rstrip("\n") + "\n"
    check(text)
    return text


def check(text):
    """Fail closed if a stock path or placeholder survived."""
    for line in text.splitlines():
        key, val = _split(line)
        if key is None:
            continue
        if STOCK_PATH in val:
            raise ConfigError("stock path left in generated config: " + line)
        m = PLACEHOLDER_RE.search(val)
        if m:
            raise ConfigError("unexpanded placeholder {} in: {}".format(m.group(0), line))


def parse_cpus(text):
    """'0-3,8' -> [0, 1, 2, 3, 8]"""
    out = []
    for part in text.strip().split(","):
        if part:
            a, _, b = part.partition("-")
            out += range(int(a), int(b or a) + 1)
    return out


def pick_cpus(cpus, host_cpus, busy=(), cores=None):
    """cpuset for a `cpus` limit with no cpuset of its own, or "" to leave it unpinned.

    A quota alone lets Android run threads on every host CPU: it spends the quota in a few ms
    and then the whole container, UI included, stalls for the rest of the period (GNOME: "not
    responding"); a shorter period does not help. So pin to as many CPUs, the least loaded by
    the other instances (`busy`: their (cpus limit, pinned CPUs) pairs, [] = not pinned):
    one thread per physical core first (`cores`: CPU -> its core's CPUs), CPU 0 (interrupts)
    last."""
    n = math.ceil(float(cpus))
    if not host_cpus or n >= len(host_cpus):
        return ""
    load = collections.Counter()
    for limit, pin in busy:
        pin = [c for c in pin if c in host_cpus] or host_cpus  # its limit spread over its CPUs
        for c in pin:
            load[c] += min(float(limit or len(pin)), len(pin)) / len(pin)
    cores = cores or {}

    def key(c):
        core = cores.get(c, [c])
        return sum(load[s] for s in core), load[c], core.index(c), c == 0, c
    return ",".join(str(c) for c in sorted(sorted(host_cpus, key=key)[:n]))


CPUSET_KEY = "lxc.cgroup2.cpuset.cpus"


def config_cpuset(lines):
    """CPUs a config's cpuset line pins to ([] = not pinned)."""
    for line in lines:
        key, val = _split(line)
        if key == CPUSET_KEY:
            return parse_cpus(val)
    return []


def cgroup_limits(cpus="", cpuset="", memory="", period=100000):
    lines = []
    if cpus:
        quota = max(1000, int(float(cpus) * period))
        lines.append("lxc.cgroup2.cpu.max = {} {}".format(quota, period))
    if cpuset:
        lines.append(CPUSET_KEY + " = " + cpuset)
    if memory:
        lines.append("lxc.cgroup2.memory.high = " + memory)
    return lines


def _entry(src, dst, fstype="none", options="rbind,create=file 0 0"):
    for p in (src, dst):
        if any(c in p for c in " \t\n\r"):
            raise ConfigError("path contains whitespace: {!r}".format(p))
    return "lxc.mount.entry = {} {} {} {}".format(src, dst, fstype, options)


def session_entries(wayland_src, pulse_src, data_dir, venus_src="",
                    xdg_runtime="/run/xdg", wayland_name="wayland-0"):
    """Mount entries for config_session (mirrors stock generate_session_lxc_config). venus_src:
    the NVIDIA renderer's socket directory, read-only at /dev/venus (sockets stay connectable)."""
    lines = ["lxc.mount.entry = tmpfs {} none create=dir 0 0".format(xdg_runtime)]
    lines.append(_entry(wayland_src, os.path.join(xdg_runtime, wayland_name).lstrip("/")))
    if pulse_src:
        lines.append(_entry(pulse_src, os.path.join(xdg_runtime, "pulse", "native").lstrip("/")))
    lines.append(_entry(data_dir, "data", options="rbind 0 0"))
    if venus_src:
        lines.append(_entry(venus_src, "dev/venus", options="rbind,ro,create=dir 0 0"))
    return "\n".join(lines) + "\n"
