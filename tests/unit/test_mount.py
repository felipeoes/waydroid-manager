# SPDX-License-Identifier: GPL-3.0-or-later
import os
import tempfile
import types
import unittest

from waydroid_multi.daemon import magisk

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
        with self.assertRaises(ValueError):
            validate_setting("system_writable", "maybe")

    def test_root_setting(self):
        self.assertIn("root", RESTART_SETTINGS)
        self.assertEqual(validate_setting("root", "false"), "false")
        with self.assertRaises(ValueError):
            validate_setting("root", "yes please")


class MagiskRemoveTest(unittest.TestCase):
    def test_does_not_follow_symlinked_parent_out_of_upper(self):
        with tempfile.TemporaryDirectory() as t:
            host = os.path.join(t, "host")
            os.makedirs(os.path.join(host, "init/magisk"))
            upper = os.path.join(t, "inst/overlay_rw/system/system")
            os.makedirs(upper)
            os.symlink(host, os.path.join(upper, "etc"))
            own = os.path.join(t, "inst/overlay/system/etc/init/magisk")
            os.makedirs(own)
            magisk.remove(types.SimpleNamespace(dir=os.path.join(t, "inst")))
            self.assertTrue(os.path.isdir(os.path.join(host, "init/magisk")))
            self.assertFalse(os.path.exists(own))


if __name__ == "__main__":
    unittest.main()
