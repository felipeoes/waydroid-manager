# SPDX-License-Identifier: GPL-3.0-or-later
import os
import tempfile
import types
import unittest
import zipfile
from unittest import mock

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
    def test_android_14_libndk_selects_a_layer(self):
        with mock.patch.object(armtrans.platform, "machine", return_value="x86_64"), \
                mock.patch.object(armtrans.layers, "layer", return_value="/libndk") as build:
            self.assertEqual(armtrans.layer("libndk", "34"), "/libndk")
        build.assert_called_once()
        self.assertEqual(build.call_args.args[1:3], armtrans.BUILDS["libndk", "34"])

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

    def test_install_patches_selinux_checks_and_ships_full_app(self):
        patches = magisk.PATCH["x86_64"]
        lib = bytearray(max(o + len(w) // 2 for o, w, _ in patches) + 4)
        patched = bytearray(lib)
        for offset, was, becomes in patches:
            lib[offset:offset + len(was) // 2] = bytes.fromhex(was)
            patched[offset:offset + len(becomes) // 2] = bytes.fromhex(becomes)
        with tempfile.TemporaryDirectory() as t:
            apk = os.path.join(t, "magisk.apk")
            with zipfile.ZipFile(apk, "w") as z:
                z.writestr("assets/stub.apk", "stub that downloads the app")
                z.writestr("assets/util_functions.sh", "")
                z.writestr("lib/x86_64/libmagisk.so", bytes(lib))
                z.writestr("lib/x86/libmagisk.so", "32-bit")
            with mock.patch.object(magisk, "ensure_apk", return_value=apk), mock.patch("os.chown"), \
                    mock.patch("platform.machine", return_value="x86_64"):
                magisk.install(types.SimpleNamespace(dir=os.path.join(t, "inst")))
            mdir = os.path.join(t, "inst/overlay/system/etc/init/magisk")
            with open(apk, "rb") as a, open(os.path.join(mdir, "stub.apk"), "rb") as s:
                self.assertEqual(a.read(), s.read())
            with open(os.path.join(mdir, "magisk"), "rb") as m:
                self.assertEqual(m.read(), bytes(patched))
            self.assertTrue(os.path.isfile(os.path.join(mdir, "magisk32")))
            self.assertTrue(os.path.isfile(os.path.join(mdir, "util_functions.sh")))

    def test_pinned_build_matches_patch(self):
        """The PATCH bytes belong to the pinned APK: a new pin needs new offsets."""
        apk = magisk.APK_CACHE
        if not os.access(apk, os.R_OK):
            self.skipTest("pinned APK not downloaded")
        with zipfile.ZipFile(apk) as z:
            for abi, patches in magisk.PATCH.items():
                lib = z.read("lib/{}/libmagisk.so".format(abi))
                for offset, was, _ in patches:
                    self.assertEqual(lib[offset:offset + len(was) // 2], bytes.fromhex(was), abi)


if __name__ == "__main__":
    unittest.main()
