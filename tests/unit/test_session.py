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


if __name__ == "__main__":
    unittest.main()
