# SPDX-License-Identifier: GPL-3.0-or-later
import os
import tempfile
import types
import unittest
import zipfile
from unittest import mock

from waydroid_manager.daemon import armtrans, magisk, util

from waydroid_manager.daemon.util import SELINUX_ROOTFS_CONTEXT, overlay_opts
from waydroid_manager.instance import RESTART_SETTINGS, setting_default, validate_setting


class OverlayOptsTest(unittest.TestCase):
    def test_readonly_puts_upper_on_top(self):
        o = overlay_opts(["/a", "/b"], "/up", "/wk", False)
        self.assertEqual(o, "ro,lowerdir=/up:/a:/b,xino=off")

    def test_writable_uses_upper_and_work(self):
        o = overlay_opts(["/a", "/b"], "/up", "/wk", True)
        self.assertEqual(o, "lowerdir=/a:/b,upperdir=/up,workdir=/wk,xino=off")

    def test_selinux_context_appended_when_given(self):
        o_ro = overlay_opts(["/a"], "/up", "/wk", False, context=SELINUX_ROOTFS_CONTEXT)
        o_rw = overlay_opts(["/a"], "/up", "/wk", True, context=SELINUX_ROOTFS_CONTEXT)
        self.assertEqual(o_ro, 'ro,lowerdir=/up:/a,xino=off,context="{}"'.format(SELINUX_ROOTFS_CONTEXT))
        self.assertEqual(o_rw, 'lowerdir=/a,upperdir=/up,workdir=/wk,xino=off,context="{}"'.format(
            SELINUX_ROOTFS_CONTEXT))

    def test_mount_image_and_overlay_apply_selinux_context_and_mount2(self):
        with tempfile.TemporaryDirectory() as t:
            up, wk, mnt = os.path.join(t, "up"), os.path.join(t, "wk"), os.path.join(t, "mnt")
            with mock.patch.object(util, "selinux_rootfs_context", return_value=SELINUX_ROOTFS_CONTEXT), \
                    mock.patch.object(util, "is_mount", return_value=True), \
                    mock.patch.object(util, "run") as run:
                util.mount_image("/img/system.img", mnt)
                self.assertEqual(run.call_args_list[0].args[0],
                                 ["mount", "-o", 'ro,context="{}"'.format(SELINUX_ROOTFS_CONTEXT),
                                  "/img/system.img", mnt])
                util.mount_overlay(["/lo"], mnt, up, wk, writable=False)
                call = run.call_args_list[1]
                self.assertEqual(call.args[0],
                                 ["mount", "-t", "overlay", "-o",
                                  'ro,lowerdir={}:/lo,xino=off,context="{}"'.format(up, SELINUX_ROOTFS_CONTEXT),
                                  "overlay", mnt])
                self.assertEqual(call.kwargs.get("env"), {"LIBMOUNT_FORCE_MOUNT2": "always"})

            with mock.patch.object(util, "selinux_rootfs_context", return_value=None), \
                    mock.patch.object(util, "is_mount", return_value=True), \
                    mock.patch.object(util, "run") as run:
                util.mount_image("/img/system.img", mnt)
                self.assertEqual(run.call_args_list[0].args[0], ["mount", "-o", "ro", "/img/system.img", mnt])
                util.mount_overlay(["/lo"], mnt, up, wk, writable=False)
                self.assertEqual(run.call_args_list[1].args[0],
                                 ["mount", "-t", "overlay", "-o",
                                  "ro,lowerdir={}:/lo,xino=off".format(up), "overlay", mnt])
                self.assertEqual(run.call_args_list[1].kwargs.get("env"), {"LIBMOUNT_FORCE_MOUNT2": "always"})

    def test_selinux_mode_and_binder_query(self):
        with mock.patch("builtins.open", mock.mock_open(read_data="1\n")):
            self.assertEqual(util.selinux_mode(), "enforcing")
        with mock.patch("builtins.open", mock.mock_open(read_data="0\n")):
            self.assertEqual(util.selinux_mode(), "permissive")
        with mock.patch("builtins.open", side_effect=FileNotFoundError):
            self.assertIsNone(util.selinux_mode())
            self.assertIsNone(util.selinux_binder_allowed())

        files = {
            "/sys/fs/selinux/class/binder/index": "77\n",
            "/sys/fs/selinux/class/binder/perms/call": "2\n",
        }
        def fake_open(path, *_a, **_kw):
            return mock.mock_open(read_data=files[path])()

        for resp, expected in ((b"a ffffffff 0 ffffffff 2 0\x00", True), (b"0 ffffffff 0 ffffffff 3 0\x00", False)):
            with mock.patch.object(util, "selinux_mode", return_value="enforcing"), \
                    mock.patch.object(util, "selinux_context_valid", return_value=True), \
                    mock.patch("builtins.open", side_effect=fake_open), \
                    mock.patch.object(util.os, "open", return_value=99), \
                    mock.patch.object(util.os, "write", return_value=10), \
                    mock.patch.object(util.os, "read", return_value=resp), \
                    mock.patch.object(util.os, "close"):
                self.assertIs(util.selinux_binder_allowed(), expected)

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
        def lib_for(abi):
            patches = magisk.PATCH[abi]
            lib = bytearray(max(o + len(w) // 2 for o, w, _ in patches) + 4)
            patched = bytearray(lib)
            for offset, was, becomes in patches:
                lib[offset:offset + len(was) // 2] = bytes.fromhex(was)
                patched[offset:offset + len(becomes) // 2] = bytes.fromhex(becomes)
            return lib, patched
        lib, patched = lib_for("x86_64")
        lib32, patched32 = lib_for("x86")
        with tempfile.TemporaryDirectory() as t:
            apk = os.path.join(t, "magisk.apk")
            with zipfile.ZipFile(apk, "w") as z:
                z.writestr("assets/stub.apk", "stub that downloads the app")
                z.writestr("assets/util_functions.sh", "")
                z.writestr("lib/x86_64/libmagisk.so", bytes(lib))
                z.writestr("lib/x86/libmagisk.so", bytes(lib32))
            with mock.patch.object(magisk, "ensure_apk", return_value=apk), mock.patch("os.chown"), \
                    mock.patch("platform.machine", return_value="x86_64"):
                magisk.install(types.SimpleNamespace(dir=os.path.join(t, "inst")))
            mdir = os.path.join(t, "inst/overlay/system/etc/init/magisk")
            with open(apk, "rb") as a, open(os.path.join(mdir, "stub.apk"), "rb") as s:
                self.assertEqual(a.read(), s.read())
            with open(os.path.join(mdir, "magisk"), "rb") as m:
                self.assertEqual(m.read(), bytes(patched))
            with open(os.path.join(mdir, "magisk32"), "rb") as m:
                self.assertEqual(m.read(), bytes(patched32))
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
