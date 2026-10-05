# SPDX-License-Identifier: GPL-3.0-or-later
import os
import tempfile
import unittest
from unittest import mock

from waydroid_multi.session import desktop


class StockLauncherTest(unittest.TestCase):
    def test_instance0_overrides_stock_waydroid_icon(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(desktop.paths, "user_applications_dir",
                                                                   return_value=d):
            open(os.path.join(d, "waydroid-multi.0.desktop"), "w").close()   # 0.4's own #0 icon
            desktop.cleanup_launchers(["0", "1"])
            self.assertEqual(sorted(os.listdir(d)), ["Waydroid.desktop"])
            self.assertTrue(desktop.has_launcher("0"))
            desktop.remove_launcher("0")   # hidden, so stock's entry stays hidden too
            self.assertFalse(desktop.has_launcher("0"))
            self.assertTrue(os.path.exists(desktop.launcher_path("0")))
            desktop.cleanup_launchers(["0"])   # a hidden one is the user's choice: kept hidden
            self.assertFalse(desktop.has_launcher("0"))
            desktop.write_launcher("0", "Stock Waydroid")
            text = open(desktop.launcher_path("0")).read()
            self.assertIn("Name=Waydroid\n", text)
            self.assertIn("StartupWMClass=Waydroid\n", text)
            self.assertIn(desktop.MARK, text)


if __name__ == "__main__":
    unittest.main()
