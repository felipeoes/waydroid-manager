# SPDX-License-Identifier: GPL-3.0-or-later
"""Without systemd: dnsmasq runs on its own, a bus name holds stock Waydroid back while #0 runs,
and sessions are found by their command line."""
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

from waydroid_manager import client, paths
from waydroid_manager.daemon import container, network


def no_systemctl(cmd, *a, **kw):
    if cmd[0] in ("systemctl", "systemd-run"):
        raise FileNotFoundError(cmd[0])
    return subprocess.CompletedProcess(cmd, 0, "", "")


class DnsmasqTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        for p in (mock.patch.object(paths, "RUN_DIR", self.tmp),
                  mock.patch.object(paths, "systemd_booted", return_value=False),
                  mock.patch.object(network, "run", side_effect=no_systemctl)):
            p.start()
            self.addCleanup(p.stop)
        self.net = network.Network.__new__(network.Network)
        self.net.cfg = mock.Mock(env=lambda: {"WDM_BRIDGE": "wdm0"})

    def fake_dnsmasq(self):
        """A process named dnsmasq, its pid in the pidfile; reaped in the background like the
        real one, which init reaps."""
        exe = os.path.join(self.tmp, "dnsmasq")
        shutil.copy(shutil.which("sleep"), exe)
        proc = subprocess.Popen([exe, "30"])
        self.addCleanup(proc.kill)
        threading.Thread(target=proc.wait, daemon=True).start()
        with open(os.path.join(self.tmp, "dnsmasq.pid"), "w") as f:
            f.write("{}\n".format(proc.pid))
        return proc

    def test_pidfile_counts_only_while_dnsmasq_has_that_pid(self):
        self.assertIsNone(network.dnsmasq_pid())
        with open(os.path.join(self.tmp, "dnsmasq.pid"), "w") as f:
            f.write(str(os.getpid()))           # a stale pid, reused by another program
        self.assertIsNone(network.dnsmasq_pid())
        proc = self.fake_dnsmasq()
        self.assertEqual(network.dnsmasq_pid(), proc.pid)
        self.assertTrue(self.net._dnsmasq_running())

    def test_stop_waits_for_it_to_go(self):
        self.fake_dnsmasq()
        self.net._dnsmasq_stop()
        self.assertIsNone(network.dnsmasq_pid())
        with mock.patch.object(network.os, "kill", side_effect=ProcessLookupError):
            self.fake_dnsmasq()
            self.net._dnsmasq_stop()        # gone between the pidfile check and the signal
            self.net._dnsmasq_reload()

    def test_reload_sends_hup(self):
        proc = self.fake_dnsmasq()
        self.net._dnsmasq_reload()
        proc.wait(5)
        self.assertEqual(proc.returncode, -signal.SIGHUP)

    def test_start_daemonizes_through_the_script(self):
        self.net._dnsmasq_start()
        network.run.assert_called_once_with(["sh", paths.NET_SCRIPT, "start-dnsmasq"], env={"WDM_BRIDGE": "wdm0"})


class StockHoldTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        self.was_active = os.path.join(tmp, "stock-was-active")
        self.bus = mock.Mock()
        self.owned = []         # name_has_owner's answers, then False
        self.bus.name_has_owner.side_effect = lambda _n: self.owned.pop(0) if self.owned else False
        self.service = mock.Mock(return_value=True)
        for p in (mock.patch.object(paths, "systemd_booted", return_value=False),
                  mock.patch.object(container, "STOCK_WAS_ACTIVE", self.was_active),
                  mock.patch.object(container, "run", side_effect=no_systemctl),
                  mock.patch.object(container, "_stock_service", self.service),
                  mock.patch.object(container.stockctl, "container_state", return_value="STOPPED"),
                  mock.patch.object(container.dbus, "SystemBus", return_value=self.bus),
                  mock.patch.object(container.dbus.service, "BusName"),
                  mock.patch.object(container.os, "kill")):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(container._stock_name.clear)

    def test_idle_stock_service_is_stopped_held_back_and_restarted(self):
        self.owned = [True, True]
        container.hold_stock()
        self.service.assert_called_once_with("stop")
        container.dbus.service.BusName.assert_called_once_with(container.STOCK_BUS_NAME, self.bus,
                                                               do_not_queue=True)
        self.assertTrue(os.path.exists(self.was_active))
        container.hold_stock()      # adopting a running #0 again: one hold
        self.assertEqual(container.dbus.service.BusName.call_count, 1)

        container.release_stock()
        self.assertEqual(container._stock_name, [])
        self.service.assert_called_with("start")
        self.assertFalse(os.path.exists(self.was_active))

    def test_unsupervised_service_gets_sigterm(self):
        self.owned = [True]
        self.service.return_value = False
        self.bus.get_object.return_value.GetConnectionUnixProcessID.return_value = 4321
        with mock.patch.object(container.dbus, "Interface", side_effect=lambda o, _i: o):
            container.hold_stock()
        container.os.kill.assert_called_once_with(4321, signal.SIGTERM)

    def test_stock_not_running_is_not_started_afterwards(self):
        container.hold_stock()
        self.service.assert_not_called()
        container.release_stock()
        self.service.assert_not_called()
        container.run.assert_not_called()

    def test_unsupervised_service_is_started_through_dbus(self):
        self.owned = [True]
        self.service.return_value = False
        with mock.patch.object(container.dbus, "Interface"):
            container.hold_stock()
        container.release_stock()
        self.assertEqual(container.run.call_args[0][0][-2:], ["string:" + container.STOCK_BUS_NAME, "uint32:0"])

    def test_running_stock_container_is_left_alone(self):
        self.owned = [True]
        container.stockctl.container_state.return_value = "RUNNING"
        with self.assertRaises(RuntimeError):
            container.hold_stock()
        self.service.assert_not_called()
        container.dbus.service.BusName.assert_not_called()

    def test_name_still_taken(self):
        container.dbus.service.BusName.side_effect = container.dbus.exceptions.NameExistsException("x")
        with self.assertRaisesRegex(RuntimeError, "didn't stop"):
            container.hold_stock()


@unittest.skipUnless(shutil.which("pgrep"), "needs pgrep")
class SessionTest(unittest.TestCase):
    def session(self, iid):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)",
                                 "-m", "waydroid_manager", "session", iid, "--background"])
        self.addCleanup(proc.kill)
        threading.Thread(target=proc.wait, daemon=True).start()
        return proc

    def test_found_and_stopped_by_command_line(self):
        with mock.patch.object(client.shutil, "which", return_value=None):     # no systemctl
            self.session("77")
            self.assertTrue(client.session_active("77"))
            self.assertFalse(client.session_active("7"))
            self.assertTrue(client.stop_session("77"))
            self.assertFalse(client.session_active("77"))
            self.assertFalse(client.stop_session("77"))


class DaemonCommandTest(unittest.TestCase):
    def test_per_init(self):
        with mock.patch.object(paths, "systemd_booted", return_value=True):
            self.assertEqual(client.daemon_command("start"), ["systemctl", "start", "waydroid-manager"])
        with mock.patch.object(paths, "systemd_booted", return_value=False), \
                mock.patch.object(client.os.path, "isfile", return_value=False), \
                mock.patch.object(client.os.path, "exists", side_effect=lambda p: p.startswith("/var/service/")):
            self.assertEqual(client.daemon_command("restart"), ["sv", "restart", "/var/service/waydroid-manager"])
        with mock.patch.object(paths, "systemd_booted", return_value=False), \
                mock.patch.object(client.os.path, "isfile", return_value=False), \
                mock.patch.object(client.os.path, "exists", return_value=False):
            self.assertIsNone(client.daemon_command("start"))


if __name__ == "__main__":
    unittest.main()
