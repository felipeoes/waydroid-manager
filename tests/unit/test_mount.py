# SPDX-License-Identifier: GPL-3.0-or-later
import unittest

from waydroid_multi.daemon.util import overlay_opts
from waydroid_multi.instance import RESTART_SETTINGS, validate_setting


class OverlayOptsTest(unittest.TestCase):
    def test_readonly_puts_upper_on_top(self):
        o = overlay_opts(["/a", "/b"], "/up", "/wk", False)
        self.assertEqual(o, "ro,lowerdir=/up:/a:/b,xino=off")

    def test_writable_uses_upper_and_work(self):
        o = overlay_opts(["/a", "/b"], "/up", "/wk", True)
        self.assertEqual(o, "lowerdir=/a:/b,upperdir=/up,workdir=/wk,xino=off")

    def test_setting(self):
        self.assertIn("system_writable", RESTART_SETTINGS)
        self.assertEqual(validate_setting("system_writable", "true"), "true")
        with self.assertRaises(Exception):
            validate_setting("system_writable", "maybe")

    def test_root_setting(self):
        self.assertIn("root", RESTART_SETTINGS)
        self.assertEqual(validate_setting("root", "false"), "false")
        with self.assertRaises(Exception):
            validate_setting("root", "yes please")


if __name__ == "__main__":
    unittest.main()
