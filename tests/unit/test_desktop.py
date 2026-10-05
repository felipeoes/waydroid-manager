# SPDX-License-Identifier: GPL-3.0-or-later
import os
import tempfile
import unittest
from unittest import mock

from waydroid_manager.session import desktop


class StockLauncherTest(unittest.TestCase):
    def test_one_waydroid_icon(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(desktop.paths, "user_applications_dir",
                                                                   return_value=d):
            desktop.write_launcher("0", "")
            desktop.write_launcher("2", "Gone")             # an instance deleted meanwhile
            desktop.cleanup_launchers(["0", "1"])
            self.assertIn("Name=Waydroid\n", open(desktop.launcher_path("0")).read())
            self.assertFalse(os.path.exists(desktop.launcher_path("2")))
            stock = open(os.path.join(d, desktop.STOCK_LAUNCHER)).read()
            self.assertIn("NoDisplay=true\n", stock)
            self.assertIn(desktop.MARK, stock)
            desktop.remove_launcher("0")        # removed from the grid: no Waydroid icon at all
            desktop.cleanup_launchers(["0"])
            self.assertFalse(os.path.exists(desktop.launcher_path("0")))
            self.assertTrue(os.path.exists(os.path.join(d, desktop.STOCK_LAUNCHER)))


if __name__ == "__main__":
    unittest.main()
