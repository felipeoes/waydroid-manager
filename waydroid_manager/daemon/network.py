# SPDX-License-Identifier: GPL-3.0-or-later
"""The shared waydroid-manager bridge + dnsmasq.

Bridge setup and dnsmasq always (re)start together: recreating the bridge
under a running dnsmasq breaks its DHCP socket. dnsmasq runs as a transient
systemd unit so restarting the daemon does not take networking down.
"""
import json
import os
import shutil
import threading

from .. import paths
from ..netconfig import NetConfig, dhcp_hosts_text, etc_hosts_text, firewall_blockers, opening_rules
from .util import log, run

DNSMASQ_UNIT = "waydroid-manager-dnsmasq.service"


def firewall_consent():
    """The admin's answer to opening the host's own firewall for devices: allow, deny or ask."""
    try:
        with open(paths.FIREWALL_CONSENT_FILE) as f:
            answer = f.read().strip()
    except OSError:
        return "ask"
    return answer if answer in ("allow", "deny") else "ask"


def set_firewall_consent(answer):
    os.makedirs(paths.STATE_DIR, exist_ok=True)
    tmp = paths.FIREWALL_CONSENT_FILE + ".tmp"
    with open(tmp, "w") as f:
        f.write(answer + "\n")
    os.replace(tmp, paths.FIREWALL_CONSENT_FILE)


def nft_ruleset():
    """`nft -j list ruleset` as JSON; None without nft."""
    if not shutil.which("nft"):
        return None
    r = run(["nft", "-j", "list", "ruleset"], check=False)
    try:
        return json.loads(r.stdout) if r.returncode == 0 else None
    except ValueError:
        return None


class Network:
    def __init__(self, cfg=None):
        self.cfg = cfg or NetConfig.load()
        self.lock = threading.Lock()
        self.blockers = []      # the host's firewall chains that cut devices off, as of the last start

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
                self.sync_firewall()    # a reload of the host's firewall drops our rules there
                return
            bad = self.cfg.overlapping_routes()
            if bad:
                raise RuntimeError("subnet {} overlaps existing routes ({}); set another subnet in {}"
                                   .format(self.cfg.network, ", ".join(bad), paths.CONFIG_FILE))
            log.info("bringing up network %s on %s", self.cfg.network, self.cfg.bridge)
            run(["systemctl", "stop", DNSMASQ_UNIT], check=False)
            run(["systemctl", "reset-failed", DNSMASQ_UNIT], check=False)
            with open(paths.NET_ENV_FILE, "w") as f:  # lets uninstall.sh tear down a custom setup
                f.write(self.cfg.env_file_text())
            run(["sh", paths.NET_SCRIPT, "setup"], env=self._env())
            cmd = ["systemd-run", "--unit=" + DNSMASQ_UNIT, "--collect",
                   "--description=waydroid-manager DHCP/DNS for " + self.cfg.bridge,
                   "-p", "Restart=on-failure"]
            for k, v in sorted(self._env().items()):
                cmd.append("--setenv={}={}".format(k, v))
            cmd += ["sh", paths.NET_SCRIPT, "run-dnsmasq"]
            run(cmd)
            self.sync_firewall()

    def sync_firewall(self):
        """Our rules in the host's own blocking chains: taken out, then put back if the admin allowed
        it (SetHostFirewall). Otherwise the chains are noted in blockers, for GetInfo."""
        ruleset = nft_ruleset()
        if ruleset is None:
            self.blockers = []
            return
        run(["sh", paths.NET_SCRIPT, "close-firewall"], check=False)
        try:
            with open(os.path.join(paths.RUN_DIR, "network_up")) as f:
                backend = f.read().strip()      # where net.sh put our own rules
        except OSError:
            backend = ""
        blockers = firewall_blockers(ruleset, backend)
        if blockers and firewall_consent() == "allow":
            log.info("opening the host's firewall for %s: %s", self.cfg.bridge, blockers)
            for cmd in opening_rules(blockers, self.cfg.bridge):
                run(cmd, check=False)
            blockers = []
        elif blockers:
            log.warning("the host's firewall blocks devices' network: %s", blockers)
        self.blockers = blockers

    def write_names(self, entries):
        """Our block in /etc/hosts: [(ip, "waydroid-<name>")], so adb shows instances by name.
        Rewritten in place: a new file would lose its owner, ACLs and SELinux label, and can't
        replace a bind-mounted /etc/hosts."""
        try:
            with open("/etc/hosts", "r+") as f:
                old = f.read()
                new = etc_hosts_text(old, entries)
                if new != old:
                    f.seek(0)
                    f.write(new)
                    f.truncate()
        except OSError as e:
            log.warning("cannot update /etc/hosts with instance names: %s", e)

    def reload_hosts(self, hosts):
        with self.lock:
            self.write_hosts(hosts)
            if self.is_up():
                run(["systemctl", "kill", "-s", "HUP", "--kill-whom=main", DNSMASQ_UNIT], check=False)

    def ensure_down(self):
        with self.lock:
            log.info("taking down network %s", self.cfg.bridge)
            run(["systemctl", "stop", DNSMASQ_UNIT], check=False)
            self.blockers = []
            run(["sh", paths.NET_SCRIPT, "teardown", "force"], env=self._env(), check=False)

    def ip_for(self, inst):
        return str(self.cfg.ip_for_index(inst.index))

    def isolate(self, veth):
        if self.cfg.isolate and os.path.isdir("/sys/class/net/" + veth):
            run(["bridge", "link", "set", "dev", veth, "isolated", "on"], check=False)
