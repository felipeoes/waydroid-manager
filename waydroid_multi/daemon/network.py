# SPDX-License-Identifier: GPL-3.0-or-later
"""The shared waydroid-multi bridge + dnsmasq.

Bridge setup and dnsmasq always (re)start together: recreating the bridge
under a running dnsmasq breaks its DHCP socket. dnsmasq runs as a transient
systemd unit so restarting the daemon does not take networking down.
"""
import os
import threading

from .. import paths
from ..netconfig import NetConfig, dhcp_hosts_text
from .util import log, run

DNSMASQ_UNIT = "waydroid-multi-dnsmasq.service"


class Network:
    def __init__(self, cfg=None):
        self.cfg = cfg or NetConfig.load()
        self.lock = threading.Lock()

    def _env(self):
        return self.cfg.env()

    def is_up(self):
        return (os.path.exists(os.path.join(paths.RUN_DIR, "network_up"))
                and os.path.isdir("/sys/class/net/" + self.cfg.bridge)
                and run(["systemctl", "is-active", "-q", DNSMASQ_UNIT], check=False).returncode == 0)

    def write_hosts(self, entries):
        os.makedirs(paths.RUN_DIR, exist_ok=True)
        tmp = paths.DHCP_HOSTS_FILE + ".tmp"
        with open(tmp, "w") as f:
            f.write(dhcp_hosts_text(entries))
        os.replace(tmp, paths.DHCP_HOSTS_FILE)

    def ensure_up(self, hosts):
        """hosts: iterable of (mac, ip) for every known instance."""
        with self.lock:
            self.write_hosts(hosts)
            if self.is_up():
                run(["systemctl", "kill", "-s", "HUP", "--kill-whom=main", DNSMASQ_UNIT], check=False)
                return
            bad = self.cfg.overlapping_routes()
            if bad:
                raise RuntimeError("subnet {} overlaps existing routes ({}); set another subnet in {}"
                                   .format(self.cfg.network, ", ".join(bad), paths.CONFIG_FILE))
            log.info("bringing up network %s on %s", self.cfg.network, self.cfg.bridge)
            run(["systemctl", "stop", DNSMASQ_UNIT], check=False)
            run(["systemctl", "reset-failed", DNSMASQ_UNIT], check=False)
            run(["sh", paths.NET_SCRIPT, "setup"], env=self._env())
            cmd = ["systemd-run", "--unit=" + DNSMASQ_UNIT, "--collect",
                   "--description=waydroid-multi DHCP/DNS for " + self.cfg.bridge,
                   "-p", "Restart=on-failure"]
            for k, v in sorted(self._env().items()):
                cmd.append("--setenv={}={}".format(k, v))
            cmd += ["sh", paths.NET_SCRIPT, "run-dnsmasq"]
            run(cmd)

    def reload_hosts(self, hosts):
        with self.lock:
            self.write_hosts(hosts)
            if self.is_up():
                run(["systemctl", "kill", "-s", "HUP", "--kill-whom=main", DNSMASQ_UNIT], check=False)

    def ensure_down(self):
        with self.lock:
            log.info("taking down network %s", self.cfg.bridge)
            run(["systemctl", "stop", DNSMASQ_UNIT], check=False)
            run(["sh", paths.NET_SCRIPT, "teardown", "force"], env=self._env(), check=False)

    def ip_for(self, inst):
        return str(self.cfg.ip_for_index(inst.index))

    def isolate(self, veth):
        if self.cfg.isolate and os.path.isdir("/sys/class/net/" + veth):
            run(["bridge", "link", "set", "dev", veth, "isolated", "on"], check=False)
