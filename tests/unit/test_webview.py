# SPDX-License-Identifier: GPL-3.0-or-later
import hashlib
import os
import shutil
import struct
import subprocess
import tempfile
import unittest
import zipfile
import zlib
from unittest.mock import patch

from waydroid_manager.daemon import webview


def manifest(use32bitabi):
    """A binary AndroidManifest.xml of one element with android:use32bitAbi (resource map only:
    the patch doesn't read strings)."""
    resmap = struct.pack("<HHI", 0x0180, 8, 12) + struct.pack("<I", 0x01010515)
    attr = struct.pack("<III", 0xFFFFFFFF, 0, 0xFFFFFFFF) + struct.pack("<HBBI", 8, 0, 0x12, use32bitabi)
    ext = struct.pack("<IIHHHHHH", 0xFFFFFFFF, 0, 20, 20, 1, 0, 0, 0)
    elem = struct.pack("<HHIII", 0x0102, 16, 16 + len(ext) + len(attr), 1, 0xFFFFFFFF) + ext + attr
    return struct.pack("<HHI", 0x0003, 8, 8 + len(resmap) + len(elem)) + resmap + elem


class ClearUse32bitAbiTest(unittest.TestCase):
    def test_true_becomes_false(self):
        self.assertEqual(webview.clear_use32bitabi(manifest(0xFFFFFFFF)), manifest(0))

    def test_false_is_left_alone(self):
        self.assertIsNone(webview.clear_use32bitabi(manifest(0)))


class FastUserAgentTest(unittest.TestCase):
    def test_unknown_build_is_unchanged(self):
        self.assertIsNone(webview.fast_user_agent(b"unknown dex"))

    def test_enable_async_path_and_repair_dex_checksums(self):
        dex = b"dex\n035\0" + bytes(104) + b"\x0a\x04" + bytes(16)
        with patch.object(webview, "_UA_DEX_SHA256", hashlib.sha256(dex).hexdigest()), \
                patch.object(webview, "_UA_OFFSET", 112):
            out = webview.fast_user_agent(dex)
        self.assertEqual(out[112:114], b"\x12\x14")  # const/4 v4, 1
        self.assertEqual(out[32:112], dex[32:112])
        self.assertEqual(out[114:], dex[114:])
        self.assertEqual(out[12:32], hashlib.sha1(out[32:]).digest())
        self.assertEqual(struct.unpack_from("<I", out, 8)[0], zlib.adler32(out[12:]))

    def test_known_hash_with_wrong_instruction_is_rejected(self):
        dex = bytes(128)
        with patch.object(webview, "_UA_DEX_SHA256", hashlib.sha256(dex).hexdigest()), \
                patch.object(webview, "_UA_OFFSET", 112):
            with self.assertRaises(ValueError):
                webview.fast_user_agent(dex)


@unittest.skipUnless(shutil.which("openssl"), "needs openssl")
class PatchApkTest(unittest.TestCase):
    def test_dex_fix_also_applies_to_already_64bit_webview(self):
        dex = b"dex\n035\0" + bytes(104) + b"\x0a\x04" + bytes(16)
        with tempfile.TemporaryDirectory() as tmp:
            src, dst = os.path.join(tmp, "in.apk"), os.path.join(tmp, "out.apk")
            with zipfile.ZipFile(src, "w") as z:
                z.writestr("AndroidManifest.xml", manifest(0))
                z.writestr("classes.dex", dex)
            with patch.object(webview, "_UA_DEX_SHA256", hashlib.sha256(dex).hexdigest()), \
                    patch.object(webview, "_UA_OFFSET", 112):
                self.assertTrue(webview.patch_apk(src, dst))
            with zipfile.ZipFile(dst) as z:
                self.assertEqual(z.read("AndroidManifest.xml"), manifest(0))
                self.assertEqual(z.read("classes.dex")[112:114], b"\x12\x14")

    def test_aligned_and_signed(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, dst = os.path.join(tmp, "in.apk"), os.path.join(tmp, "out.apk")
            with zipfile.ZipFile(src, "w") as z:
                z.writestr("AndroidManifest.xml", manifest(0xFFFFFFFF), zipfile.ZIP_DEFLATED)
                z.writestr("META-INF/CERT.RSA", b"old signature")
                z.writestr("lib/x86_64/libwebviewchromium.so", b"\x7fELF" * 1000)
            self.assertTrue(webview.patch_apk(src, dst))
            with zipfile.ZipFile(dst) as z:
                self.assertEqual(z.read("AndroidManifest.xml"), manifest(0))
                self.assertNotIn("META-INF/CERT.RSA", z.namelist())
                so = z.getinfo("lib/x86_64/libwebviewchromium.so")
            with open(dst, "rb") as f:
                data = f.read()
            n, e = struct.unpack_from("<HH", data, so.header_offset + 26)
            self.assertEqual((so.header_offset + 30 + n + e) % 16384, 0)
            self.check_v2(data, tmp)

    def check_v2(self, data, tmp):
        """The v2 signature verifies against the APK as it was before the block went in."""
        cd_size, cd_off = struct.unpack_from("<II", data, len(data) - 22 + 12)
        self.assertEqual(data[cd_off - 16:cd_off], b"APK Sig Block 42")
        block_size, = struct.unpack_from("<Q", data, cd_off - 24)
        start = cd_off - block_size - 8
        _, block_id = struct.unpack_from("<QI", data, start + 8)
        self.assertEqual(block_id, 0x7109871A)

        def lp(off):
            n, = struct.unpack_from("<I", data, off)
            return data[off + 4:off + 4 + n], off + 4 + n

        signer, _ = lp(start + 20 + 4)          # sequence of signers -> the first
        signed, rest = lp(start + 20 + 4 + 4)
        sigs, rest = lp(rest)
        pub, _ = lp(rest)
        sig = sigs[4 + 4 + 4:]                  # length, algorithm, length
        digest = signed[4 + 4 + 4 + 4:4 + 4 + 4 + 4 + 32]
        unsigned = data[:start] + data[cd_off:len(data) - 6] + struct.pack("<I", start) + data[-2:]
        with open(os.path.join(tmp, "unsigned"), "wb") as f:
            f.write(unsigned)
        with open(os.path.join(tmp, "unsigned"), "rb") as f:
            size = len(unsigned)
            self.assertEqual(webview._digest(f, ((0, start), (start, size - 22), (size - 22, size))), digest)
        for name, b in (("pub.der", pub), ("sig", sig), ("signed", signed)):
            with open(os.path.join(tmp, name), "wb") as f:
                f.write(b)
        r = subprocess.run(["openssl", "dgst", "-sha256", "-keyform", "DER", "-verify", os.path.join(tmp, "pub.der"),
                            "-signature", os.path.join(tmp, "sig"), os.path.join(tmp, "signed")], capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(signer.startswith(struct.pack("<I", len(signed))))


if __name__ == "__main__":
    unittest.main()
