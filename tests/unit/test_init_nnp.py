# SPDX-License-Identifier: GPL-3.0-or-later
"""Android 16+ init's prctl(PR_SET_NO_NEW_PRIVS) is disabled for the mount only, and only when unambiguous."""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from waydroid_manager.daemon import container

CALL = container.INIT_NNP


class InitNoNewPrivsTest(unittest.TestCase):
    def test_only_a_single_call_is_patched(self):
        self.assertEqual(container.init_without_nnp(b"ab" + CALL + b"cd"), b"ab\xbf\0" + CALL[2:] + b"cd")
        self.assertIsNone(container.init_without_nnp(b"init before Android 16"))
        self.assertIsNone(container.init_without_nnp(CALL + CALL))

    def test_mounts_an_executable_copy_and_leaves_the_guest_file(self):
        original = b"\0" * 100 + CALL + b"\0" * 100
        with tempfile.TemporaryDirectory() as tmp:
            inst = SimpleNamespace(id="5", dir=tmp, rootfs=tmp + "/rootfs")
            path = Path(inst.rootfs, "system/bin/init")
            path.parent.mkdir(parents=True)
            path.write_bytes(original)
            mounted = []

            def bind(src, dst):
                self.assertEqual(os.stat(dst), path.stat())
                self.assertEqual(os.stat(src).st_mode & 0o777, 0o755)
                mounted.append(Path(src).read_bytes())

            with mock.patch.object(container, "bind_mount", side_effect=bind):
                container.fix_init(inst)
            self.assertEqual(mounted, [container.init_without_nnp(original)])
            self.assertEqual(path.read_bytes(), original)

    def test_older_init_and_missing_init_are_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(container, "bind_mount") as bind:
            container.fix_init(SimpleNamespace(id="5", dir=tmp, rootfs=tmp))
            Path(tmp, "system/bin").mkdir(parents=True)
            Path(tmp, "system/bin/init").write_bytes(b"init before Android 16")
            container.fix_init(SimpleNamespace(id="5", dir=tmp, rootfs=tmp))
            bind.assert_not_called()

    def test_guest_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(container, "bind_mount") as bind:
            Path(tmp, "system/bin").mkdir(parents=True)
            Path(tmp, "system/bin/init").symlink_to("/etc/passwd")
            with self.assertRaises(OSError):
                container.fix_init(SimpleNamespace(id="5", dir=tmp, rootfs=tmp))
            bind.assert_not_called()


if __name__ == "__main__":
    unittest.main()
