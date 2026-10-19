# SPDX-License-Identifier: GPL-3.0-or-later
"""The ANGLE image-external patch only changes pinned builds, on a copy mounted over the guest file."""
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from waydroid_manager.daemon import container


class AnglePatchTest(unittest.TestCase):
    def test_only_a_pinned_build_gets_a_patched_copy(self):
        original = bytes(range(256)) * 16
        sha = hashlib.sha256(original).hexdigest()
        edits = ((0x10, "e9fd7e0b"), (0x800, "0f1f00"))
        patched = bytearray(original)
        patched[0x10:0x14] = bytes.fromhex("e9fd7e0b")
        patched[0x800:0x803] = bytes.fromhex("0f1f00")
        for data in (original, original + b"another build"):
            with self.subTest(pinned=data == original), tempfile.TemporaryDirectory() as tmp:
                inst = SimpleNamespace(id="5", dir=tmp, rootfs=tmp + "/rootfs")
                path = Path(inst.rootfs, "system/lib64/libGLESv2_angle.so")
                path.parent.mkdir(parents=True)
                path.write_bytes(data)
                mounted = []

                def bind(src, dst):
                    self.assertEqual(os.stat(dst), path.stat())
                    self.assertEqual(os.stat(src).st_mode & 0o777, 0o644)
                    mounted.append(Path(src).read_bytes())

                with mock.patch.object(container, "ANGLE_PATCHES", {sha: edits}), \
                        mock.patch.object(container, "bind_mount", side_effect=bind):
                    container.fix_angle(inst)
                self.assertEqual(mounted, [bytes(patched)] if data == original else [])
                self.assertEqual(path.read_bytes(), data)
                self.assertFalse(Path(tmp, "libGLESv2_angle.so.tmp").exists())

    def test_image_without_angle_is_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(tmp + "/rootfs/system/lib64")
            with mock.patch.object(container, "bind_mount") as bind:
                container.fix_angle(SimpleNamespace(id="5", dir=tmp, rootfs=tmp + "/rootfs"))
            bind.assert_not_called()


if __name__ == "__main__":
    unittest.main()
