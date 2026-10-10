# SPDX-License-Identifier: GPL-3.0-or-later
import os
import tempfile
import types
import unittest

from waydroid_manager.daemon import armtrans, magisk

from waydroid_manager.daemon.util import overlay_opts
from waydroid_manager.instance import RESTART_SETTINGS, setting_default, validate_setting


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

    def test_arm_translation_setting(self):
        self.assertIn("arm_translation", RESTART_SETTINGS)
        self.assertEqual(setting_default("arm_translation"), "houdini")
        self.assertEqual(validate_setting("arm_translation", " LibNDK "), "libndk")
        with self.assertRaises(ValueError):
            validate_setting("arm_translation", "qemu")


class ArmTranslationTest(unittest.TestCase):
    def test_no_layer_without_a_build(self):
        # neither may download anything
        self.assertIsNone(armtrans.layer("none", "33"))
        self.assertIsNone(armtrans.layer("houdini", "28"))

    def test_android_14_houdini_is_the_android_13_build(self):
        self.assertEqual(armtrans.BUILDS["houdini", "34"], armtrans.BUILDS["houdini", "33"])

    def test_every_build_has_props(self):
        for kind, sdk in armtrans.BUILDS:
            self.assertIn("arm64-v8a", armtrans.PROPS[kind]["ro.product.cpu.abilist"])


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
