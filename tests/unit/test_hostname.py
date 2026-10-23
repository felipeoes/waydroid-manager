# SPDX-License-Identifier: GPL-3.0-or-later
"""Apps can resolve their container hostname without waiting on external DNS."""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from waydroid_manager.daemon import container


class HostnameTest(unittest.TestCase):
    def test_mounts_own_address_first_and_preserves_hosts(self):
        original = b"127.0.0.1 localhost\n::1 ip6-localhost\n10.0.0.99 waydroid-1\n10.0.0.20 custom\n"
        with tempfile.TemporaryDirectory() as tmp:
            inst = SimpleNamespace(id="1", dir=tmp, rootfs=tmp + "/rootfs")
            hosts = Path(inst.rootfs, "system/etc/hosts")
            hosts.parent.mkdir(parents=True)
            hosts.write_bytes(original)
            mounted = []

            def bind(src, dst):
                self.assertEqual(os.stat(dst), hosts.stat())
                self.assertEqual(os.stat(src).st_mode & 0o777, 0o644)
                mounted.append(Path(src).read_bytes())

            with mock.patch.object(container, "bind_mount", side_effect=bind):
                container.write_hosts(inst, "192.168.241.11")
            self.assertEqual(mounted, [b"192.168.241.11 waydroid-1\n" + original])
            self.assertEqual(hosts.read_bytes(), original)

    def test_rejects_guest_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            hosts = Path(tmp, "system/etc/hosts")
            hosts.parent.mkdir(parents=True)
            hosts.symlink_to("/etc/hosts")
            with self.assertRaises(OSError):
                container.write_hosts(SimpleNamespace(id="1", dir=tmp, rootfs=tmp), "192.168.241.11")

    def test_rejects_oversized_hosts_without_truncating(self):
        with tempfile.TemporaryDirectory() as tmp:
            hosts = Path(tmp, "system/etc/hosts")
            hosts.parent.mkdir(parents=True)
            with hosts.open("wb") as f:
                f.truncate((16 << 20) + 1)
            with self.assertRaisesRegex(ValueError, "hosts.*too large"):
                container.write_hosts(SimpleNamespace(id="1", dir=tmp, rootfs=tmp), "192.168.241.11")
            self.assertEqual(hosts.stat().st_size, (16 << 20) + 1)


if __name__ == "__main__":
    unittest.main()
