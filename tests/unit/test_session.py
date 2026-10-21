# SPDX-License-Identifier: GPL-3.0-or-later
"""Session startup and toolbar action delegation."""
import os
import tempfile
import unittest
from unittest import mock

from waydroid_manager.instance import Instance
from waydroid_manager.session import main


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
