# SPDX-License-Identifier: GPL-3.0-or-later
import os
import tempfile
import unittest
from unittest import mock

from waydroid_multi.session import desktop


class StockLauncherTest(unittest.TestCase):
    def test_one_waydroid_icon(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(desktop.paths, "user_applications_dir",
                                                                   return_value=d):
            with open(desktop.launcher_path("0"), "w") as f:   # 0.4's #0 launcher
                f.write("[Desktop Entry]\nName=Stock Waydroid (Waydroid)\n")
            desktop.cleanup_launchers(["0", "1"])
            self.assertIn("Name=Waydroid\n", open(desktop.launcher_path("0")).read())
            stock = open(os.path.join(d, desktop.STOCK_LAUNCHER)).read()
            self.assertIn("NoDisplay=true\n", stock)
            self.assertIn(desktop.MARK, stock)
            desktop.remove_launcher("0")        # removed from the grid: no Waydroid icon at all
            desktop.cleanup_launchers(["0"])
            self.assertFalse(os.path.exists(desktop.launcher_path("0")))
            self.assertTrue(os.path.exists(os.path.join(d, desktop.STOCK_LAUNCHER)))


if __name__ == "__main__":
    unittest.main()
