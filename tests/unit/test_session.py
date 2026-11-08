# SPDX-License-Identifier: GPL-3.0-or-later
"""Session startup and toolbar action delegation."""
import os
import tempfile
import time
import unittest
from unittest import mock

import dbus

from waydroid_manager.instance import Instance
from waydroid_manager.session import main, services


class ProxyStartupTest(unittest.TestCase):
    def test_restart_delegates_to_the_independent_settings_app(self):
        session = main.Session.__new__(main.Session)
        session.iid = "1"
        with mock.patch.object(main, "open_settings") as settings:
            session.handle_proxy_event("action restart")
            settings.assert_called_once_with("1", restart=True)

    def test_stock_pointer_mode_follows_its_images(self):
        session = main.Session.__new__(main.Session)
        session.iid = "0"
        session.inst = Instance.new("0", 0, 1000, "", {}, {})   # config defaults to Android 13
        session.daemon = mock.Mock()
        for android in ("11", "13"):
            with self.subTest(android=android), tempfile.TemporaryDirectory() as runtime, \
                    tempfile.TemporaryFile() as stdout, \
                    mock.patch.object(main.paths, "user_runtime_dir", return_value=runtime), \
                    mock.patch.object(main, "color_scheme", return_value="dark"), \
                    mock.patch.object(main.GLib, "io_add_watch"), \
                    mock.patch.object(main.subprocess, "Popen") as popen:
                stdout.write(b"ready\n")
                stdout.seek(0)
                popen.return_value.stdout = stdout
                session.daemon.get.return_value = {"android": android}
                try:
                    session.start_proxy("/wayland-0")
                    self.assertEqual("--logical-pointer" in popen.call_args.args[0], android == "11")
                finally:
                    os.close(session.rotation_w)


class NestedWestonTest(unittest.TestCase):
    """On an X11 desktop the device's window is a Weston of its own."""

    def session(self):
        session = main.Session.__new__(main.Session)
        session.iid = "3"
        session.inst = Instance.new("3", 3, 1000, "", {}, {})
        return session

    def test_size_fits_the_x_screen(self):
        out = mock.Mock(stdout="screen #0:\n  dimensions:    1389x824 pixels (367x218 millimeters)\n")
        with mock.patch.object(main.subprocess, "run", return_value=out):
            self.assertEqual(main.nested_size(720, 1280), (417, 741))
            self.assertEqual(main.nested_size(400, 300), (400, 300))   # never enlarged
            self.assertEqual(main.nested_size(0, 0), (1250, 741))      # matching the display: 90% of it
        with mock.patch.object(main.subprocess, "run", side_effect=FileNotFoundError):
            self.assertEqual(main.nested_size(720, 1280), (720, 1280))
            self.assertEqual(main.nested_size(0, 0), (1280, 720))

    def test_starts_weston_and_returns_its_socket(self):
        session = self.session()
        with tempfile.TemporaryDirectory() as xdg, tempfile.TemporaryDirectory() as cache, \
                mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": xdg}), \
                mock.patch.object(main.shutil, "which", return_value="/usr/bin/weston"), \
                mock.patch.object(main.paths, "user_cache_dir", return_value=cache), \
                mock.patch.object(main, "nested_size", return_value=(400, 700)), \
                mock.patch.object(main.x11, "Display") as display, \
                mock.patch.object(main.subprocess, "Popen") as popen:
            # another device's, one starting with ours, ours
            display.return_value.weston_windows.side_effect = [{0x10}, {0x10, 0x18, 0x20}]
            display.return_value.window_pid.side_effect = {0x18: 99, 0x20: popen.return_value.pid}.get
            sock = os.path.join(xdg, "waydroid-manager-3-weston")
            open(sock + ".lock", "w").close()                   # left by a Weston that died
            popen.return_value.poll.side_effect = lambda: open(sock, "w").close()
            self.assertEqual(session.start_nested(), sock)
            argv = popen.call_args.args[0]
            self.assertIn("--socket=waydroid-manager-3-weston", argv)
            self.assertIn("--backend=x11", argv)
            self.assertIn("--width=400", argv)
            self.assertFalse(os.path.exists(sock + ".lock"))
            self.assertEqual(session.xwin, 0x20)
            display.return_value.set_title.assert_called_once_with(0x20, session.inst.name + " · #3")

    def test_fullscreen_reported_once_the_window_manager_made_it(self):
        session = self.session()
        r, session.rotation_w = os.pipe()
        session.x11, session.xwin = mock.Mock(), 0x20
        session.x11.is_fullscreen.side_effect = [False, True]
        far = time.monotonic() + 60
        self.assertTrue(session.report_fullscreen(False, far))           # not yet: polled again
        self.assertFalse(session.report_fullscreen(False, far))
        self.assertEqual(os.read(r, 100), b"fullscreen 1\n")
        os.close(r)
        os.close(session.rotation_w)

    def test_window_turns_with_android(self):
        """Weston's window is the device's: Android turned for a landscape app, it takes that shape
        (the proxy fills it), unless it has it already, is fullscreen or sizes Android (0x0)."""
        session = self.session()
        size = {"width": "720", "height": "1280"}
        session.inst = mock.Mock(get=size.get)
        session.x11, session.xwin = mock.Mock(), 0x20
        session.x11.is_fullscreen.return_value = False
        tb = main.TOOLBAR_W
        with mock.patch.object(main, "nested_size", side_effect=lambda w, h: (w, h)) as fit:
            session.x11.size.return_value = (500 + tb, 909)
            session.handle_proxy_event("rotation 1")
            session.x11.resize.assert_called_once_with(0x20, 909 + tb, 500)
            fit.assert_called_once_with(909 + tb, 500)                     # kept on the screen
            session.x11.resize.reset_mock()
            session.x11.size.return_value = (909 + tb, 500)
            session.handle_proxy_event("rotation 3")                       # landscape already
            session.handle_proxy_event("rotation 2")                       # upside down: portrait
            session.x11.resize.assert_called_once_with(0x20, 500 + tb, 909)
            session.x11.resize.reset_mock()
            session.x11.is_fullscreen.return_value = True
            session.handle_proxy_event("rotation 0")
            session.x11.is_fullscreen.return_value = False
            size.update(width="0", height="0")
            session.handle_proxy_event("rotation 0")
            session.x11.resize.assert_not_called()

    def test_frames_go_through_shared_memory_under_weston(self):
        session = self.session()
        gpus = [main.gpu.Gpu("0000:21:00.0", "GPU 0", "nvidia", "/dev/dri/renderD129")]
        with mock.patch.object(main.gpu, "gpus", return_value=gpus), \
                mock.patch.object(main.gpu, "host_gpu", return_value=None):
            render = {"mode": "nvidia", "cpu_buffers": ""}
            self.assertEqual(session.cpu_buffers(render), "")              # a Wayland desktop
            session.weston = mock.Mock()
            self.assertEqual(session.cpu_buffers(render), "/dev/dri/renderD129")
            self.assertEqual(session.cpu_buffers({"mode": "vkms", "cpu_buffers": "shared"}), "shared")
            self.assertEqual(session.cpu_buffers({"mode": "software", "cpu_buffers": ""}), "")

    def test_without_weston_says_what_to_install(self):
        with mock.patch.object(main.shutil, "which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "install weston"):
                self.session().start_nested()


class AppEntrySyncTest(unittest.TestCase):
    def test_package_changes_are_synced_once(self):
        session = main.Session.__new__(main.Session)
        session.iid = "1"
        with mock.patch.object(main.GLib, "timeout_add_seconds", return_value=7) as timer:
            for _ in range(30):              # a Play Store update of 30 apps
                session.queue_app_sync()
            timer.assert_called_once_with(2, session.sync_app_entries)

    def test_entries_go_when_the_setting_is_off(self):
        session = main.Session.__new__(main.Session)
        session.iid = "1"
        inst = Instance.new("1", 1, 1000, "", {}, {})
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(main.Instance, "load", return_value=inst), \
                mock.patch.object(main.desktop.paths, "user_applications_dir", return_value=d):
            main.desktop.write_launcher("1", "One")
            stale = os.path.join(d, "waydroid-manager.1.org.example.desktop")
            open(stale, "w").close()
            session.sync_app_entries()
            self.assertFalse(os.path.exists(stale))
            self.assertTrue(os.path.exists(main.desktop.launcher_path("1")))
            self.assertEqual(session.apps_state, (False, inst.name))


class ShutdownTest(unittest.TestCase):
    def session(self):
        session = main.Session.__new__(main.Session)
        session.iid = "1"
        session.stopping = False
        session.daemon = mock.Mock()
        session.loop = mock.Mock()
        return session

    def test_a_second_signal_keeps_the_handler_and_stops_once(self):
        session = self.session()
        self.assertTrue(session.on_signal())
        self.assertTrue(session.on_signal())
        session.daemon.stop.assert_called_once_with("1")

    def test_a_proxy_that_dies_stops_the_windowless_instance(self):
        session = self.session()
        session.proxy = mock.Mock()
        session.proxy_fd = os.open(os.devnull, os.O_RDONLY)
        try:
            with mock.patch.object(main, "notify"), \
                    mock.patch.object(main.Instance, "load", return_value=Instance.new("1", 1, 1000, "", {}, {})), \
                    self.assertLogs(main.log, "WARNING"):
                self.assertFalse(session.on_proxy(session.proxy_fd, None))
        finally:
            os.close(session.proxy_fd)
        session.daemon.stop.assert_called_once_with("1")

    def test_a_proxy_ended_by_our_stop_stops_nothing_more(self):
        session = self.session()
        session.stopping = True
        session.proxy = mock.Mock()
        session.proxy_fd = os.open(os.devnull, os.O_RDONLY)
        try:
            with self.assertLogs(main.log, "WARNING"):
                self.assertFalse(session.on_proxy(session.proxy_fd, None))
        finally:
            os.close(session.proxy_fd)
        session.daemon.stop.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class NotificationTest(unittest.TestCase):
    def test_null_texts_reach_the_desktop_as_empty_strings(self):
        """Android sends null for a notification without a title, text or action label."""
        sent = []

        def notify(*args):
            # what dbus-python does to the call: a None string raises TypeError there
            dbus.lowlevel.MethodCallMessage("org.freedesktop.Notifications", "/org/freedesktop/Notifications",
                                            "org.freedesktop.Notifications", "Notify").append(
                signature="susssasa{sv}i", *args)
            sent.append(args)
            return 7
        n = services.Notifications.__new__(services.Notifications)
        n.inst_id, n.inst_name, n.I = "1", "Phone", mock.Mock(ID_NONE=0)
        n.proxy = mock.Mock(Notify=notify)
        nid = n._notify(0, None, None, None, None, [("open", None)], None, None, False, -1, False, False, 1)
        self.assertEqual(nid, 7)
        label, _, _, summary, body, actions = sent[0][:6]
        self.assertEqual((summary, body, actions), ("", "", ["open", ""]))
        self.assertNotIn("None", label)
