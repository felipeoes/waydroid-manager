# SPDX-License-Identifier: GPL-3.0-or-later
import os
import tempfile
import unittest

from waydroid_multi import lxcconfig
from waydroid_multi.instance import (Instance, mac_for_index, validate_id, validate_prop,
                                     validate_setting)
from waydroid_multi.netconfig import NetConfig, dhcp_hosts_text
from waydroid_multi.registry import allocate_index

# Shapes of the stock snippets (config_base + config_3 + config_4)
BASE_162 = """# Waydroid LXC Config

lxc.rootfs.path = /var/lib/waydroid/rootfs
lxc.arch = LXCARCH
lxc.autodev = 0
lxc.mount.auto = cgroup:ro sys:ro proc
lxc.console.path = none
lxc.include = /var/lib/waydroid/lxc/waydroid/config_nodes
lxc.include = /var/lib/waydroid/lxc/waydroid/config_session
lxc.hook.post-stop = /dev/null
"""
BASE_163 = BASE_162.replace("lxc.hook.post-stop = /dev/null", "lxc.hook.post-stop = LXCPOSTSTOP")
CONFIG_3 = """lxc.uts.name = waydroid
lxc.apparmor.profile = unconfined
lxc.seccomp.profile = /var/lib/waydroid/lxc/waydroid/waydroid.seccomp
lxc.no_new_privs = 1
lxc.init.cmd = /init
lxc.net.0.type = veth
lxc.net.0.flags = up
lxc.net.0.link = waydroid0
lxc.net.0.name = eth0
lxc.net.0.hwaddr = 00:16:3e:f9:d3:03
lxc.net.0.mtu = 1500
"""
CONFIG_4 = "lxc.pty.max = 10\nlxc.seccomp.allow_nesting = 1\n"


def build(base, **kw):
    args = dict(rootfs="/var/lib/waydroid-multi/instances/t1/rootfs",
                lxc_dir="/var/lib/waydroid-multi/lxc/wdm-t1", bridge="wdmulti0",
                mac="02:57:44:4d:00:01", veth="wdm1v", uts_name="waydroid-t1", arch="x86_64",
                apparmor_profile="lxc-waydroid", poststop_hook="/x/poststop.sh",
                netup_hook="/x/netup.sh", limits=["lxc.cgroup2.memory.high = 4G"])
    args.update(kw)
    return lxcconfig.build_config([base, CONFIG_3, CONFIG_4], **args)


def values(text, key):
    return [l.split("=", 1)[1].strip() for l in text.splitlines() if l.split("=", 1)[0].strip() == key]


class LxcConfigTest(unittest.TestCase):
    def check_common(self, text):
        self.assertEqual(values(text, "lxc.rootfs.path"), ["/var/lib/waydroid-multi/instances/t1/rootfs"])
        self.assertEqual(values(text, "lxc.include"), ["/var/lib/waydroid-multi/lxc/wdm-t1/config_nodes",
                                                       "/var/lib/waydroid-multi/lxc/wdm-t1/config_session"])
        self.assertEqual(values(text, "lxc.seccomp.profile"), ["/var/lib/waydroid-multi/lxc/wdm-t1/waydroid.seccomp"])
        self.assertEqual(values(text, "lxc.net.0.link"), ["wdmulti0"])
        self.assertEqual(values(text, "lxc.net.0.hwaddr"), ["02:57:44:4d:00:01"])
        self.assertEqual(values(text, "lxc.net.0.veth.pair"), ["wdm1v"])
        self.assertEqual(values(text, "lxc.net.0.script.up"), ["/x/netup.sh"])
        self.assertEqual(values(text, "lxc.uts.name"), ["waydroid-t1"])
        self.assertEqual(values(text, "lxc.arch"), ["x86_64"])
        self.assertEqual(values(text, "lxc.apparmor.profile"), ["lxc-waydroid"])
        self.assertEqual(values(text, "lxc.hook.post-stop"), ["/x/poststop.sh"])
        self.assertIn("lxc.cgroup2.memory.high = 4G", text)
        self.assertNotIn("/var/lib/waydroid/", text)
        self.assertNotIn("waydroid0", text)

    def test_stock_162(self):
        self.check_common(build(BASE_162))

    def test_stock_163_poststop_placeholder(self):
        self.check_common(build(BASE_163))

    def test_apparmor_unloaded_falls_back_to_unconfined(self):
        self.assertEqual(values(build(BASE_162, apparmor_profile=None), "lxc.apparmor.profile"), ["unconfined"])

    def test_fail_closed_on_unknown_stock_path(self):
        with self.assertRaises(lxcconfig.ConfigError):
            build(BASE_162 + "lxc.mount.entry = /var/lib/waydroid/foo bar none bind 0 0\n")

    def test_fail_closed_on_placeholder(self):
        with self.assertRaises(lxcconfig.ConfigError):
            build(BASE_162 + "lxc.hook.start = LXCSTARTHOOK\n")

    def test_cgroup_limits(self):
        self.assertEqual(lxcconfig.cgroup_limits("2", "0-3", "4G"),
                         ["lxc.cgroup2.cpu.max = 200000 100000", "lxc.cgroup2.cpuset.cpus = 0-3",
                          "lxc.cgroup2.memory.high = 4G"])
        self.assertEqual(lxcconfig.cgroup_limits("1.5"), ["lxc.cgroup2.cpu.max = 150000 100000"])
        self.assertEqual(lxcconfig.cgroup_limits(), [])

    def test_session_entries(self):
        text = lxcconfig.session_entries("/run/waydroid-multi/instances/t1/wayland-0", "",
                                         "/var/lib/waydroid-multi/instances/t1/data")
        self.assertIn("lxc.mount.entry = /run/waydroid-multi/instances/t1/wayland-0 run/xdg/wayland-0 none rbind,create=file 0 0", text)
        self.assertIn("lxc.mount.entry = /var/lib/waydroid-multi/instances/t1/data data none rbind 0 0", text)
        self.assertNotIn("pulse", text)
        with self.assertRaises(lxcconfig.ConfigError):
            lxcconfig.session_entries("/run/a b", "", "/data")


class InstanceTest(unittest.TestCase):
    def test_ids_are_numbers(self):
        for ok in ("1", "2", "99", "240"):
            self.assertEqual(validate_id(ok), ok)
        for bad in ("", "0", "default", "241", "01", "t1", "-1", "1a", "../x"):
            with self.assertRaises(ValueError):
                validate_id(bad)
        # 0.1 name-based ids are only accepted for migration
        self.assertEqual(validate_id("game_2", legacy=True), "game_2")
        with self.assertRaises(ValueError):
            validate_id("default", legacy=True)

    def test_settings_validation(self):
        self.assertEqual(validate_setting("memory", "4gb"), "4G")
        self.assertEqual(validate_setting("cpus", "2.0"), "2")
        self.assertEqual(validate_setting("window_labels", "off"), "false")
        self.assertEqual(validate_setting("close_action", "FREEZE"), "freeze")
        self.assertEqual(validate_setting("zoom", "75%"), "75")
        self.assertEqual(validate_setting("zoom", "AUTO"), "auto")
        self.assertEqual(validate_setting("device_model", "pixel_7"), "pixel_7")
        for k, v in (("width", "-1"), ("cpuset", "0-3;rm"), ("memory", "lots"), ("close_action", "explode"),
                     ("name", "a\nb"), ("bogus", "1"), ("cpus", ""), ("memory", ""), ("memory", "100M"),
                     ("zoom", "10"), ("device_model", "nokia3310")):
            with self.assertRaises(ValueError):
                validate_setting(k, v)

    def test_resource_defaults_follow_host_memory(self):
        from unittest import mock
        from waydroid_multi import instance
        with mock.patch.object(instance, "host_memory_bytes", return_value=30 * 1024 ** 3):
            self.assertEqual(instance.default_memory(), "4G")
        with mock.patch.object(instance, "host_memory_bytes", return_value=4 * 1024 ** 3):
            self.assertEqual(instance.default_memory(), "2G")
        with mock.patch.object(instance.os, "cpu_count", return_value=1):
            self.assertEqual(instance.default_cpus(), "1")

    def test_empty_legacy_values_read_as_defaults(self):
        inst = Instance.new("5", 5, 1000, "1-1", {}, {})
        inst.cfg["instance"]["memory"] = ""
        inst.cfg["instance"]["cpus"] = ""
        from waydroid_multi.instance import default_cpus, default_memory
        self.assertEqual(inst.get("memory"), default_memory())
        self.assertEqual(inst.get("cpus"), default_cpus())
        self.assertEqual(inst.get("name"), "Instance 5")

    def test_props(self):
        self.assertEqual(validate_prop("ro.hardware.egl", "mesa"), "mesa")
        for k, v in (("bad key", "x"), ("ro.x", "a\nb")):
            with self.assertRaises(ValueError):
                validate_prop(k, v)

    def test_new_instance_layout_and_roundtrip(self):
        inst = Instance.new("3", 3, 1000, "100-200", {"arch": "x86_64", "vendor_type": "MAINLINE"}, {"a.b": "1"})
        self.assertEqual(inst.binder("binder"), "binderfs/wdm3-binder")
        self.assertEqual(inst.binder("hwbinder"), "binderfs/wdm3-hwbinder")
        self.assertEqual(inst.mac, mac_for_index(3))
        self.assertEqual(inst.veth, "wdm3v")
        self.assertEqual(inst.get("close_action"), "stop")
        self.assertEqual(inst.get("name"), "Instance 3")
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "instance.cfg")
            inst.set("width", "1280")
            inst.save(p)
            again = Instance.load("3", p)
            self.assertEqual(again.get("width"), "1280")
            self.assertEqual(again.index, 3)
            self.assertEqual(again.cfg["properties"]["a.b"], "1")
            self.assertEqual(oct(os.stat(p).st_mode & 0o777), "0o644")

    def test_macs_unique_and_never_bridge(self):
        macs = {mac_for_index(i) for i in range(1, 241)}
        self.assertEqual(len(macs), 240)
        self.assertNotIn("02:57:44:4d:00:00", macs)  # the bridge's own MAC


class NetConfigTest(unittest.TestCase):
    def test_addresses(self):
        n = NetConfig("wdmulti0", "192.168.241.0/24")
        self.assertEqual(str(n.gateway), "192.168.241.1")
        self.assertEqual(str(n.ip_for_index(1)), "192.168.241.11")
        self.assertEqual(str(n.ip_for_index(240)), "192.168.241.250")
        env = n.env()
        self.assertEqual(env["WDM_BRIDGE"], "wdmulti0")
        self.assertEqual(env["WDM_NFT_TABLE"], "waydroid_multi")
        self.assertEqual(env["WDM_DHCP_START"], "192.168.241.0")

    def test_rejects_bad_config(self):
        for args in (("averyveryverylongbridge", "192.168.241.0/24"), ("br", "192.168.241.0/28"),
                     ("br", "192.168.241.5/24")):
            with self.assertRaises(ValueError):
                NetConfig(*args)

    def test_overlap_detection(self):
        n = NetConfig("wdmulti0", "192.168.241.0/24")
        routes = [{"dst": "default", "dev": "eth0"}, {"dst": "192.168.240.0/24", "dev": "waydroid0"},
                  {"dst": "192.168.241.0/24", "dev": "wdmulti0"}, {"dst": "192.168.0.0/16", "dev": "tun0"}]
        self.assertEqual(n.overlapping_routes(routes), ["192.168.0.0/16 dev tun0"])

    def test_hosts_file(self):
        self.assertEqual(dhcp_hosts_text([("02:..:02", "10.0.0.12"), ("02:..:01", "10.0.0.11")]),
                         "02:..:01,10.0.0.11\n02:..:02,10.0.0.12\n")


class RegistryTest(unittest.TestCase):
    def test_lowest_free_number_is_reused(self):
        self.assertEqual(allocate_index([]), 1)
        self.assertEqual(allocate_index([1, 2, 3]), 4)
        self.assertEqual(allocate_index([2, 3, 14]), 1)       # #1 was deleted: reuse it
        self.assertEqual(allocate_index([1, 2, 3, 14]), 4)

    def test_limit(self):
        with self.assertRaises(RuntimeError):
            allocate_index(range(1, 241))


class DevicesTest(unittest.TestCase):
    def test_preset_props(self):
        from waydroid_multi import devices
        p = devices.props_for("galaxy_s24_ultra")
        self.assertEqual(p["ro.product.waydroid.model"], "SM-S928B")
        self.assertEqual(set(p), {"ro.product.waydroid." + f for f in devices.FIELDS})
        self.assertEqual(devices.props_for("waydroid"), {})
        custom = devices.props_for("custom", {"ro.product.waydroid.model": "X1", "ro.product.waydroid.brand": "",
                                              "other": "y"})
        self.assertEqual(custom, {"ro.product.waydroid.model": "X1"})


if __name__ == "__main__":
    unittest.main()
