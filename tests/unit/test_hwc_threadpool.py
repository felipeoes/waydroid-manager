# SPDX-License-Identifier: GPL-3.0-or-later
"""The display startup backport only changes the pinned HWC and never follows guest links."""
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from waydroid_manager.daemon import container


class HwcStartupTest(unittest.TestCase):
    def test_only_the_known_binary_gets_a_copy_with_the_call_removed(self):
        original = b"\0" * 0x3d57d + bytes.fromhex("e8 4e 0c 03 00") + b"\0" * (493760 - 0x3d582)
        sha = hashlib.sha256(original).hexdigest()
        for data in (original, original + b"new version", b"already fixed"):
            with self.subTest(known=data == original), tempfile.TemporaryDirectory() as tmp:
                inst = SimpleNamespace(id="5", dir=tmp, rootfs=tmp + "/rootfs")
                path = Path(inst.rootfs, "vendor/lib64/hw/hwcomposer.waydroid.so")
                path.parent.mkdir(parents=True)
                path.write_bytes(data)
                mounted = []

                def bind(src, dst):
                    self.assertEqual(os.stat(dst), path.stat())
                    self.assertEqual(os.stat(src).st_mode & 0o777, 0o644)
                    self.assertEqual(os.stat(src).st_nlink, 1)
                    mounted.append(Path(src).read_bytes())

                with mock.patch.object(container, "HWC_THREADPOOL_CALLS", {sha: 0x3d57d}), \
                        mock.patch.object(container, "bind_mount", side_effect=bind):
                    container.fix_hwc(inst)
                self.assertEqual(mounted, [original[:0x3d57d] + b"\x90" * 5 + original[0x3d582:]]
                                 if data == original else [])
                self.assertEqual(path.read_bytes(), data)
                self.assertFalse(Path(tmp, "hwcomposer.waydroid.so.tmp").exists())
                if data == original:
                    self.assertEqual(Path(tmp, "hwcomposer.waydroid.so").read_bytes(), mounted[0])

    def test_nvidia_gets_both_fixes_without_changing_other_bytes(self):
        original = bytes(650000)
        sha = hashlib.sha256(original).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            inst = SimpleNamespace(id="5", dir=tmp, rootfs=tmp + "/rootfs")
            path = Path(inst.rootfs, "vendor/lib64/hw/hwcomposer.waydroid.so")
            path.parent.mkdir(parents=True)
            path.write_bytes(original)
            with mock.patch.object(container, "HWC_NVIDIA_SHA", sha), \
                    mock.patch.object(container, "HWC_THREADPOOL_CALLS", {sha: 0x4e010}), \
                    mock.patch.object(container, "bind_mount") as bind:
                container.fix_hwc(inst)
            bind.assert_called_once()
            expected = bytearray(original)
            expected[0x4e010:0x4e015] = b"\x90" * 5
            expected[0x61131:0x61131 + len(container.HWC_NVIDIA_ROUNDTRIP)] = container.HWC_NVIDIA_ROUNDTRIP
            self.assertEqual(Path(tmp, "hwcomposer.waydroid.so").read_bytes(), expected)
            self.assertEqual(path.read_bytes(), original)

    def test_missing_library_is_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(container, "bind_mount") as bind:
            container.fix_hwc(SimpleNamespace(rootfs=tmp))
            bind.assert_not_called()

    def test_guest_symlinks_are_rejected(self):
        for component in ("vendor", "vendor/lib64/hw/hwcomposer.waydroid.so"):
            with self.subTest(component=component), tempfile.TemporaryDirectory() as tmp, \
                    mock.patch.object(container, "bind_mount") as bind:
                target = Path(tmp, component)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to("/etc/passwd")
                with self.assertRaises(OSError):
                    container.fix_hwc(SimpleNamespace(rootfs=tmp))
                bind.assert_not_called()

    def test_guest_fifo_is_rejected_without_blocking(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(container, "bind_mount") as bind:
            path = Path(tmp, "vendor/lib64/hw/hwcomposer.waydroid.so")
            path.parent.mkdir(parents=True)
            os.mkfifo(path)
            with self.assertRaisesRegex(RuntimeError, "not a regular file"):
                container.fix_hwc(SimpleNamespace(rootfs=tmp))
            bind.assert_not_called()


if __name__ == "__main__":
    unittest.main()
