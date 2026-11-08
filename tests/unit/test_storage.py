# SPDX-License-Identifier: GPL-3.0-or-later
import io
import os
import subprocess
import tempfile
import time
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

    def pm(self, script, src):
        """_pm with lxc-attach replaced by sh -c script; the child is always reaped."""
        procs = []
        popen = subprocess.Popen

        def fake(cmd, **kw):
            procs.append(popen(["sh", "-c", script], **kw))
            return procs[-1]
        with mock.patch("waydroid_manager.daemon.util.android_attach_env", return_value={}), \
                mock.patch("subprocess.Popen", fake):
            try:
                return storage._pm(types.SimpleNamespace(id="1", container="wdm-1"), ["install"], src=src)
            finally:
                self.assertIsNotNone(procs[0].returncode)

    def test_pm_streams_its_source_and_returns_the_output(self):
        out = self.pm("wc -c; echo Success >&2", io.BytesIO(b"x" * (3 << 20)))
        self.assertEqual(out.split(), [str(3 << 20), "Success"])

    def test_pm_kills_and_reaps_child_when_source_fails(self):
        src = mock.Mock()
        src.read.side_effect = zipfile.BadZipFile("corrupt member")
        with self.assertRaises(zipfile.BadZipFile):
            self.pm("exec sleep 30", src)

    def test_hung_pm_frees_the_caller_although_its_pipes_stay_open(self):
        # like lxc-attach: what holds the pipes (sleep) outlives the process _pm kills (sh)
        t = time.monotonic()
        with mock.patch.object(storage, "PM_TIMEOUT", 0.5), self.assertRaises(RuntimeError):
            self.pm("exec 3<&0; sleep 5 <&3 & wait", io.BytesIO(b"x" * (8 << 20)))
        self.assertLess(time.monotonic() - t, 3)


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


if __name__ == "__main__":
    unittest.main()
