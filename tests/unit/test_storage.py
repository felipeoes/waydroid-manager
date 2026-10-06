# SPDX-License-Identifier: GPL-3.0-or-later
import io
import os
import tempfile
import types
import unittest
import zipfile
from unittest import mock

from waydroid_manager.daemon import storage


def xapk(**members):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in members.items():
            z.writestr(name.replace("__", "/"), data)
    buf.seek(0)
    return buf


class XapkInstallTest(unittest.TestCase):
    def run_pm(self, src, fail_on=None):
        calls = []

        def pm(inst, args, src=None, check=True):
            calls.append((args[0], args[1:], src.read() if src else None))
            if args[0] == fail_on:
                raise RuntimeError("install failed: boom")
            return "Success: created install session [42]" if args[0] == "install-create" else "Success"
        with mock.patch.object(storage, "_pm", pm):
            try:
                storage._install_xapk(types.SimpleNamespace(id="1"), src)
            except RuntimeError:
                pass
        return calls

    def test_apks_go_into_one_session(self):
        calls = self.run_pm(xapk(**{"base.apk": b"BASE", "config.arm64_v8a.apk": b"ARM", "manifest.json": b"{}",
                                    "Android__obb__x.obb": b"OBB"}))
        self.assertEqual(calls, [("install-create", ["-r", "-S", "7"], None),
                                 ("install-write", ["-S", "4", "42", "0.apk", "-"], b"BASE"),
                                 ("install-write", ["-S", "3", "42", "1.apk", "-"], b"ARM"),
                                 ("install-commit", ["42"], None)])

    def test_failed_write_abandons_the_session(self):
        calls = self.run_pm(xapk(**{"base.apk": b"BASE"}), fail_on="install-write")
        self.assertEqual([c[0] for c in calls], ["install-create", "install-write", "install-abandon"])

    def test_not_a_zip(self):
        with self.assertRaisesRegex(RuntimeError, "not an XAPK"):
            storage._install_xapk(types.SimpleNamespace(id="1"), io.BytesIO(b"nope"))


if __name__ == "__main__":
    unittest.main()


class ShaderCacheTest(unittest.TestCase):
    def test_apps_shader_caches_go_and_nothing_through_a_symlink(self):
        with tempfile.TemporaryDirectory() as d:
            def put(rel):
                os.makedirs(os.path.dirname(os.path.join(d, rel)), exist_ok=True)
                open(os.path.join(d, rel), "w").close()
            put("data/data/app/code_cache/com.android.skia.shaders_cache")
            put("data/data/app/code_cache/kept")
            put("data/user_de/0/launcher/code_cache/com.android.opengl.shaders_cache")
            put("data/user/10/app/code_cache/com.android.skia.shaders_cache")
            put("outside/com.android.skia.shaders_cache")
            os.symlink("../data", os.path.join(d, "data/user/0"))
            os.makedirs(os.path.join(d, "data/data/evil"))
            os.symlink(os.path.join(d, "outside"), os.path.join(d, "data/data/evil/code_cache"))
            storage.clear_shader_caches(os.path.join(d, "data"))
            left = sorted(os.path.relpath(os.path.join(r, f), d) for r, _, fs in os.walk(d) for f in fs)
            self.assertEqual(left, ["data/data/app/code_cache/kept", "outside/com.android.skia.shaders_cache"])
