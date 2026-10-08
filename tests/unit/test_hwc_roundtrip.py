# SPDX-License-Identifier: GPL-3.0-or-later
"""Reproduce the binary patch and exercise its queue isolation against real libwayland."""
import ctypes
import platform
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from waydroid_manager.daemon.container import HWC_NVIDIA_ROUNDTRIP

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(platform.machine() == "x86_64" and all(shutil.which(t) for t in
                     ("as", "ld", "objcopy", "gcc")), "needs x86_64 binutils and gcc")
class HwcRoundtripTest(unittest.TestCase):
    def test_assembly_reproduces_the_pinned_patch(self):
        with tempfile.TemporaryDirectory() as tmp:
            obj, elf, binary = (str(Path(tmp, f)) for f in ("patch.o", "patch.elf", "patch.bin"))
            for command in (["as", "--64", "scripts/patches/hwc-roundtrip.S", "-o", obj],
                            ["ld", "-e", "roundtrip", "-T", "scripts/patches/hwc-roundtrip.ld", obj, "-o", elf],
                            ["objcopy", "-O", "binary", "--only-section=.text", elf, binary]):
                subprocess.run(command, cwd=ROOT, check=True, capture_output=True)
            self.assertEqual(Path(binary).read_bytes(), HWC_NVIDIA_ROUNDTRIP)
            self.assertLess(len(HWC_NVIDIA_ROUNDTRIP), 0x6530c - 0x65131)

    def test_roundtrips_finish_with_a_concurrent_default_dispatcher(self):
        try:
            client = ctypes.CDLL("libwayland-client.so.0")
            ctypes.CDLL("libwayland-server.so.0")
        except OSError:
            self.skipTest("needs libwayland client and server")
        if not hasattr(client, "wl_proxy_get_queue"):
            self.skipTest("test harness needs libwayland's wl_proxy_get_queue")
        with tempfile.TemporaryDirectory() as tmp:
            binary = str(Path(tmp, "roundtrip"))
            subprocess.run(["gcc", "tests/fixtures/hwc_roundtrip.S", "tests/fixtures/hwc_roundtrip.c",
                            "-Wl,-l:libwayland-client.so.0", "-Wl,-l:libwayland-server.so.0", "-pthread",
                            "-o", binary], cwd=ROOT, check=True, capture_output=True)
            subprocess.run([binary], check=True, capture_output=True, timeout=15)
