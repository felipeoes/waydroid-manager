# SPDX-License-Identifier: GPL-3.0-or-later
import os
import tempfile
import unittest
from unittest import mock

from waydroid_manager.session import desktop


def read(path):
    with open(path) as f:
        return f.read()


class StockLauncherTest(unittest.TestCase):
    def test_one_waydroid_icon(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(desktop.paths, "user_applications_dir",
                                                                   return_value=d):
            desktop.write_launcher("0", "")
            desktop.write_launcher("2", "Gone")             # an instance deleted meanwhile
            desktop.cleanup_launchers(["0", "1"])
            self.assertIn("Name=Waydroid\n", read(desktop.launcher_path("0")))
            self.assertFalse(os.path.exists(desktop.launcher_path("2")))
            stock = read(os.path.join(d, desktop.STOCK_LAUNCHER))
            self.assertIn("NoDisplay=true\n", stock)
            self.assertIn(desktop.MARK, stock)
            desktop.remove_launcher("0")        # removed from the grid: no Waydroid icon at all
            desktop.cleanup_launchers(["0"])
            self.assertFalse(os.path.exists(desktop.launcher_path("0")))
            self.assertTrue(os.path.exists(os.path.join(d, desktop.STOCK_LAUNCHER)))


class AppEntryTest(unittest.TestCase):
    APP = {"packageName": "org.example", "name": "Example", "categories": ["android.intent.category.LAUNCHER"]}

    def test_app_entries_never_include_the_instance_launcher(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(desktop.paths, "user_applications_dir", return_value=d):
            desktop.write_launcher("1", "One")
            desktop.write_app_entry("1", "One", self.APP, d)
            desktop.write_launcher("12", "Twelve")
            self.assertEqual([os.path.basename(p) for p in desktop.app_entries("1")],
                             ["waydroid-manager.1.org.example.desktop"])

    def test_unchanged_app_entries_are_not_rewritten(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(desktop.paths, "user_applications_dir", return_value=d):
            desktop.write_app_entry("1", "One", self.APP, d)
            path, = desktop.app_entries("1")
            os.utime(path, (0, 0))
            desktop.write_app_entry("1", "One", self.APP, d)
            self.assertEqual(os.stat(path).st_mtime, 0)          # the app grid isn't told to reload
            desktop.write_app_entry("1", "Renamed", self.APP, d)
            self.assertIn("Name=Example (Renamed)\n", read(path))
            self.assertEqual(os.listdir(d), [os.path.basename(path)])   # no temporary file left


if __name__ == "__main__":
    unittest.main()
