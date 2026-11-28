# SPDX-License-Identifier: GPL-3.0-or-later
"""Full UI startup restores Android's bars after an individual app was launched."""
import argparse
import io
import unittest
from unittest import mock

from waydroid_manager import cli, gpu, netconfig, paths
from waydroid_manager.daemon import util as daemon_util


class FullUITest(unittest.TestCase):
    def test_start_and_show_restore_full_ui_unless_background(self):
        for command, background in ((cli.cmd_start, False), (cli.cmd_show, False), (cli.cmd_start, True)):
            for state in ("STOPPED", "RUNNING", "FROZEN"):
                with self.subTest(command=command.__name__, background=background, state=state), \
                        mock.patch.object(cli, "daemon") as daemon, \
                        mock.patch.object(cli, "start_session"), \
                        mock.patch.object(cli, "wait_state", return_value="RUNNING"), \
                        mock.patch.object(cli, "platform_service") as platform, \
                        mock.patch.object(cli, "statusbar_service", return_value=None):
                    daemon.return_value.get.return_value = {"state": state, "ip": "192.168.241.11"}
                    command(argparse.Namespace(id="1", background=background, wait=False, timeout=180))
                    if background:
                        platform.assert_not_called()
                    else:
                        platform.assert_called_once_with("1", timeout=180 if state == "STOPPED" else 15)
                        platform.return_value.setprop.assert_called_once_with("waydroid.active_apps", "Waydroid")
                        platform.return_value.settingsPutString.assert_called_once_with(2, "policy_control", "null*")


class DoctorSelinuxTest(unittest.TestCase):
    def _run_doctor(self, mode, has_ctx, binder_ok):
        buf = io.StringIO()
        fake_daemon = mock.Mock()
        fake_daemon.return_value.info.return_value = {
            "version": "1.6.1", "warnings": "", "image": "img1",
            "subnet": "192.168.241.0/24", "firewall": "",
        }
        with mock.patch("sys.stdout", buf), \
                mock.patch("waydroid_manager.stock.version", return_value="1.6.3"), \
                mock.patch("waydroid_manager.stock.check_version", return_value=None), \
                mock.patch("waydroid_manager.stock.known_sm_protocols", return_value={"aidl6"}), \
                mock.patch.object(cli.os.path, "exists", return_value=True), \
                mock.patch.object(cli.os.path, "isfile", return_value=True), \
                mock.patch.object(cli.shutil, "which", return_value="/usr/bin/x"), \
                mock.patch.dict(cli.os.environ, {"WAYLAND_DISPLAY": "wayland-0"}, clear=False), \
                mock.patch("waydroid_manager.gpu.host_gpu", return_value="/dev/dri/renderD128"), \
                mock.patch("waydroid_manager.gpu.nvidia_display", return_value=False), \
                mock.patch("waydroid_manager.gpu.gpus", return_value=[]), \
                mock.patch("waydroid_manager.gpu.vkms_supported", return_value=True), \
                mock.patch.object(cli.subprocess, "run", return_value=mock.Mock(returncode=0)), \
                mock.patch.object(daemon_util, "selinux_mode", return_value=mode), \
                mock.patch.object(daemon_util, "selinux_context_valid", return_value=has_ctx), \
                mock.patch.object(daemon_util, "selinux_binder_allowed", return_value=binder_ok), \
                mock.patch.object(cli, "Daemon", fake_daemon), \
                mock.patch.object(netconfig.NetConfig, "overlapping_routes", return_value=[]):
            with self.assertRaises(SystemExit) as cm:
                cli.cmd_doctor(argparse.Namespace())
        return cm.exception.code, buf.getvalue()

    def test_enforcing_with_policies_passes(self):
        rc, out = self._run_doctor("enforcing", True, True)
        self.assertEqual(rc, 0)
        self.assertIn("[ok]   SELinux enforcing (waydroid-selinux policy loaded)", out)
        self.assertIn("[ok]   SELinux enforcing (Android 14-17 APEX binder allowed)", out)

    def test_enforcing_missing_waydroid_selinux_fails(self):
        rc, out = self._run_doctor("enforcing", False, True)
        self.assertEqual(rc, 1)
        self.assertIn("[!!]   SELinux is enforcing, but waydroid-selinux policy is missing", out)

    def test_enforcing_missing_apex_binder_policy_fails(self):
        rc, out = self._run_doctor("enforcing", True, False)
        self.assertEqual(rc, 1)
        self.assertIn("[!!]   SELinux blocks Android 14-17 APEX binder calls", out)
        self.assertIn("waydroid-manager.cil", out)

    def test_permissive_does_not_fail(self):
        rc, out = self._run_doctor("permissive", False, False)
        self.assertEqual(rc, 0)
        self.assertIn("[ok]   SELinux permissive", out)

    def test_disabled_prints_no_selinux_lines(self):
        rc, out = self._run_doctor(None, False, None)
        self.assertEqual(rc, 0)
        self.assertNotIn("SELinux", out)

    def test_netconfig_and_software_props(self):
        env = netconfig.NetConfig("wdm0", "192.168.241.0/24").env()
        self.assertEqual(env["WDM_DNSMASQ_DIR"], paths.DNSMASQ_RUN_DIR)
        self.assertEqual(env["WDM_HOSTSFILE"], paths.DNSMASQ_RUN_DIR + "/dhcp-hosts")
        self.assertEqual(gpu.SOFTWARE_PROPS["ro.waydroid.override_props"], "0")


if __name__ == "__main__":
    unittest.main()
