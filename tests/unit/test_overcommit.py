# SPDX-License-Identifier: GPL-3.0-or-later
"""The host's heuristic overcommit becomes Android's 1; a strict 2 is left alone."""
import os
import tempfile
import unittest
from unittest import mock

from waydroid_manager.daemon import container


class OvercommitTest(unittest.TestCase):
    def check(self, before, after):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "overcommit_memory")
            with open(path, "w") as f:
                f.write(before + "\n")
            with mock.patch.object(container, "OVERCOMMIT", path):
                container.ensure_overcommit()
            self.assertEqual(container._read(path).strip(), after)

    def test_heuristic_becomes_always(self):
        self.check("0", "1")

    def test_strict_and_always_stay(self):
        self.check("2", "2")
        self.check("1", "1")

    def test_unwritable_is_only_logged(self):
        with mock.patch.object(container, "OVERCOMMIT", "/nonexistent/overcommit_memory"):
            container.ensure_overcommit()


if __name__ == "__main__":
    unittest.main()
