# SPDX-License-Identifier: GPL-3.0-or-later
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

from waydroid_manager import stock, stockctl
from waydroid_manager.daemon import images


class EnsureSyncedTest(unittest.TestCase):
    def run_case(self, current, stock_id, busy):
        calls = []
        with mock.patch.object(images, "current_id", side_effect=lambda: current[0]), \
                mock.patch.object(images, "stock_image_id", return_value=stock_id), \
                mock.patch.object(images, "stock_busy", return_value=busy), \
                mock.patch.object(images, "_sync", side_effect=lambda: (calls.append("sync"),
                                                                      current.__setitem__(0, stock_id))[1] or stock_id), \
                mock.patch.object(images, "gc", side_effect=lambda in_use: calls.append(("gc", tuple(in_use())))):
            result = images.ensure_synced(lambda: ["old"])
        return result, calls

    def test_up_to_date_does_nothing(self):
        self.assertEqual(self.run_case(["1-1"], "1-1", False), ("1-1", []))

    def test_new_stock_images_are_synced_and_old_ones_collected(self):
        res, calls = self.run_case(["1-1"], "2-2", False)
        self.assertEqual(res, "2-2")
        self.assertEqual(calls, ["sync", ("gc", ("old",))])

    def test_busy_stock_keeps_current_set(self):
        self.assertEqual(self.run_case(["1-1"], "2-2", True), ("1-1", []))

    def test_first_sync_ignores_busy(self):
        res, calls = self.run_case([""], "2-2", True)
        self.assertEqual((res, calls[0]), ("2-2", "sync"))


class StockStateTest(unittest.TestCase):
    def test_reads_cgroup(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(stockctl, "CGROUP", os.path.join(d, "missing")):
                self.assertEqual(stockctl.state(), "STOPPED")
            with open(os.path.join(d, "cgroup.events"), "w") as f:
                f.write("populated 1\nfrozen 0\n")
            with mock.patch.object(stockctl, "CGROUP", d):
                self.assertEqual(stockctl.state(), "RUNNING")
            with open(os.path.join(d, "cgroup.events"), "w") as f:
                f.write("populated 1\nfrozen 1\n")
            with mock.patch.object(stockctl, "CGROUP", d):
                self.assertEqual(stockctl.state(), "FROZEN")


class GbinderTest(unittest.TestCase):
    def test_shipped_library_loaded_before_gbinder(self):
        fake = types.ModuleType("gbinder")
        with tempfile.NamedTemporaryFile() as lib, mock.patch.object(stock.paths, "GBINDER_LIB", lib.name), \
                mock.patch.object(stock.ctypes, "CDLL") as cdll, mock.patch.dict(sys.modules, {"gbinder": fake}):
            self.assertIs(stock.load_gbinder(), fake)
            cdll.assert_called_once_with(lib.name, mode=stock.ctypes.RTLD_GLOBAL)
        with mock.patch.object(stock.paths, "GBINDER_LIB", "/nonexistent/libgbinder.so.1"), \
                mock.patch.object(stock.ctypes, "CDLL") as cdll, mock.patch.dict(sys.modules, {"gbinder": fake}):
            stock.load_gbinder()                    # a checkout: the system library
            cdll.assert_not_called()

    def test_protocols_of_the_shipped_library(self):
        with tempfile.NamedTemporaryFile() as lib:
            lib.write(b"\0aidl4\0aidl5\0aidl6\0")
            lib.flush()
            with mock.patch.object(stock.paths, "GBINDER_LIB", lib.name):
                self.assertEqual(stock.known_sm_protocols()[-2:], ("aidl5", "aidl6"))
        self.assertEqual(stock.protocols_for_sdk(37, ("aidl3", "aidl6")), ("aidl3", "aidl6"))
        self.assertEqual(stock.protocols_for_sdk(35, ("aidl3",)), ("aidl3", "aidl3"))   # unknown: fallback


if __name__ == "__main__":
    unittest.main()
