# SPDX-License-Identifier: GPL-3.0-or-later
import io
import json
import os
import sqlite3
import tarfile
import tempfile
import unittest
import zipfile
from unittest import mock

from waydroid_manager import catalog, gpu
from waydroid_manager.daemon import container, gapps, images, layers, nvidia, storage, util
from waydroid_manager.instance import CREATE_ONLY, Instance, validate_setting


class CatalogTest(unittest.TestCase):
    def test_versions(self):
        self.assertEqual(list(catalog.VERSIONS), ["11", "13", "14", "15", "16", "17"])
        self.assertEqual(catalog.key_for_sdk(33), "13")
        self.assertIsNone(catalog.key_for_sdk(29))      # stock Waydroid on Android 10
        self.assertEqual(catalog.label("17"), "Android 17 (experimental)")
        for key, v in catalog.VERSIONS.items():
            self.assertTrue(("ota" in v) != ("zip" in v), key)    # exactly one source
            self.assertIn(v.get("gapps", "image"), ("image", "mtg14", "gms_apex"), key)
            self.assertIn(v.get("software", True), (True, "vkms"), key)

    def test_android_is_chosen_at_creation(self):
        self.assertEqual(validate_setting("android", "16"), "16")
        with self.assertRaises(ValueError):
            validate_setting("android", "12")
        self.assertIn("android", CREATE_ONLY)
        self.assertEqual(Instance.new("5", 5, 1000, "", {}, {}).get("android"), catalog.DEFAULT)


class ImageStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(images.paths, "IMAGES_DIR", self.tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        mock.patch.object(images, "CURRENT", os.path.join(self.tmp.name, "current")).start()
        self.addCleanup(mock.patch.stopall)

    def make_set(self, sid, **cfg):
        d = os.path.join(self.tmp.name, sid)
        os.makedirs(d)
        for f in ("system.img", "vendor.img"):
            open(os.path.join(d, f), "w").close()
        if cfg:
            images._write_cfg(d, **cfg)

    def test_latest_and_gc(self):
        self.make_set("100-101", android="16", built="100")
        self.make_set("200-201", android="16", built="200")
        self.make_set("300-301", stock="true", built="300")     # stock's, for #0
        os.makedirs(os.path.join(self.tmp.name, "400-401.tmp"))  # an install in progress
        os.symlink("300-301", images.CURRENT)
        self.assertEqual(images.latest("16"), "200-201")
        self.assertEqual(images.latest("17"), "")
        images.gc(["100-101"])        # a stopped device still on the older build keeps it
        self.assertEqual(images.available(), ["100-101", "300-301"])

    def test_install_describes_a_set_before_it_appears(self):
        def fetch(tmp, sources, v, key):
            self.assertEqual(images.available(), [])
            os.makedirs(tmp)
            for f in ("system.img", "vendor.img"):
                open(os.path.join(tmp, f), "w").close()
        with mock.patch.object(images, "_build", return_value=("sid", "20260101", [])), \
                mock.patch.object(images, "_fetch", side_effect=fetch), \
                mock.patch.object(images, "probe", return_value=(36, 36)):
            self.assertEqual(images.install("16"), "sid")
        self.assertEqual(images.read_cfg("sid")["android"], "16")
        self.assertEqual(os.listdir(self.tmp.name), ["sid"])

    def test_stock_set_of_the_same_build_is_adopted(self):
        chan = catalog.VERSIONS["13"]["ota"][0]
        self.make_set("1-2", sdk="33", built="1", stock="true", channel=chan)
        with mock.patch.object(images, "_build", return_value=("1-2", "1", [])), \
                mock.patch.object(images, "_fetch") as fetch, \
                mock.patch.object(images, "probe", return_value=(33, 33)):
            self.assertEqual(images.install("13"), "1-2")
        fetch.assert_not_called()
        self.assertEqual(images.read_cfg("1-2"), dict(android="13", sdk="33", built="1", channel=chan, stock="true"))

    def test_newest_build_of_a_channel(self):
        # two builds with one datetime (Android 17's vendor channel): the file name's date decides
        listing = {"response": [
            {"version": "24.0", "datetime": 7, "filename": "lineage-24.0-20260926-vendor.zip", "url": "a", "id": "x"},
            {"version": "24.0", "datetime": 7, "filename": "lineage-24.0-20260927-vendor.zip", "url": "b", "id": "y"},
            {"version": "23.2", "datetime": 9, "filename": "lineage-23.2-20261001-vendor.zip", "url": "c", "id": "z"}]}
        with mock.patch.object(images.urllib.request, "urlopen",
                               return_value=io.BytesIO(json.dumps(listing).encode())):
            self.assertEqual(images._newest("chan", "24.0")["url"], "b")

    def test_set_ids(self):
        with mock.patch.object(images, "_newest", side_effect=[
                {"datetime": 1790512621, "url": "s", "id": "sha-s"}, {"datetime": 1790542319, "url": "v", "id": "sha-v"}]):
            # the same id stock's set has when stock runs that build: it isn't downloaded twice
            self.assertEqual(images._build("13"), ("1790512621-1790542319", "1790512621",
                                                   [("s", "sha-s"), ("v", "sha-v")]))
        sid, built, sources = images._build("14")
        self.assertEqual((sid, built), (catalog.VERSIONS["14"]["zip"][1][:16], "20260125"))

    def test_gapps_layers(self):
        self.make_set("aa", android="17")
        self.make_set("bb", android="13")
        self.assertEqual(images.gapps_layer("aa"), os.path.join(self.tmp.name, "aa", "gapps"))
        self.assertIsNone(images.gapps_layer("bb"))
        with mock.patch.object(gapps, "mtg_layer", return_value="/mtg"):
            self.make_set("cc", android="15")
            self.assertEqual(images.gapps_layer("cc"), "/mtg")


class LayerTest(unittest.TestCase):
    def test_mindthegapps_without_setupwizard_and_libs_extracted(self):
        with tempfile.TemporaryDirectory() as d:
            apk = io.BytesIO()
            with zipfile.ZipFile(apk, "w") as z:
                z.writestr("lib/x86_64/libgms.so", b"x")
                z.writestr("lib/arm64-v8a/libgms.so", b"arm")
            zpath = os.path.join(d, "mtg.zip")
            with zipfile.ZipFile(zpath, "w") as z:
                z.writestr("system/product/priv-app/GmsCore/GmsCore.apk", apk.getvalue())
                z.writestr("system/system_ext/priv-app/SetupWizard/SetupWizard.apk", b"apk")
                z.writestr("META-INF/com/google/android/update-binary", b"sh")
            out = os.path.join(d, "layer")
            gapps._unpack_mtg(zpath, out)
            app = os.path.join(out, "system/product/priv-app/GmsCore")
            self.assertTrue(os.path.isfile(os.path.join(app, "lib/x86_64/libgms.so")))
            self.assertFalse(os.path.exists(os.path.join(app, "lib/arm64-v8a")))
            self.assertFalse(os.path.exists(os.path.join(out, "system/system_ext/priv-app/SetupWizard")))
            self.assertFalse(os.path.exists(os.path.join(out, "META-INF")))

    def test_sourceforge_master_mirror(self):
        self.assertEqual(util.mirror("https://sourceforge.net/projects/waydroid/files/images/a/b.zip/download"),
                         "https://master.dl.sourceforge.net/project/waydroid/images/a/b.zip")
        self.assertEqual(util.mirror("https://github.com/x/y.zip"), "https://github.com/x/y.zip")


class PropsTest(unittest.TestCase):
    def test_one_line_per_key(self):
        # Android 14+ take a repeated key's first line, 13 its last: ours must be the only one
        self.assertEqual(container.one_per_key(["ro.hardware.vulkan=radeon", "a=1", "", "ro.hardware.vulkan=virtio"]),
                         ["ro.hardware.vulkan=virtio", "a=1"])

    def test_stock_version(self):
        with mock.patch.object(images, "current_id", return_value="x"), \
                mock.patch.object(images, "read_cfg", return_value={"sdk": "33"}):
            self.assertEqual(container.stock_android(), "13")
            self.assertEqual(container.android_of(Instance.new("0", 0, 1000, "", {}, {})), "13")
        inst = Instance.new("4", 4, 1000, "", {}, {})
        inst.set("android", "16")       # create-only is enforced by the daemon, not the model
        self.assertEqual(container.android_of(inst), "16")


class GpuTest(unittest.TestCase):
    NV = gpu.Gpu("0000:01:00.0", "GPU 0: NVIDIA GeForce RTX 5060 Ti", "nvidia", "/dev/dri/renderD128")
    AMD = gpu.Gpu("0000:11:00.0", "GPU 1: AMD Radeon Graphics", "amdgpu", "/dev/dri/renderD129")

    def modes(self, android, nvidia_desktop, igpu, settings=("auto", "software")):
        exists = os.path.exists
        with mock.patch.multiple(gpu, nvidia_display=lambda: nvidia_desktop,
                                 host_gpu=lambda: self.AMD.node if igpu else None,
                                 gpus=lambda: [self.NV, self.AMD]), \
                mock.patch.object(gpu.os.path, "exists",
                                  lambda p: nvidia_desktop if p == "/dev/nvidiactl" else exists(p)):
            return [gpu.mode(android, s) for s in settings]

    def test_desktop_on_nvidia(self):
        self.assertEqual(self.modes("13", True, True), [("nvidia", None), ("software", None)])
        self.assertEqual(self.modes("17", True, True), [("nvidia", None), ("vkms", None)])
        # no NVIDIA build for 11: never the iGPU, whose buffers the desktop can't show
        self.assertEqual(self.modes("11", True, True)[0], ("software", None))

    def test_desktop_on_another_gpu_or_none(self):
        self.assertEqual(self.modes("16", False, True)[0], ("gpu", None))       # stock Waydroid's pick
        self.assertEqual(self.modes("17", False, False)[0], ("vkms", None))
        self.assertEqual(self.modes(None, False, False)[0], ("software", None))  # stock on 10

    def test_picked_gpu(self):
        self.assertEqual(self.modes("16", True, True, ("0000:11:00.0", "0000:01:00.0")),
                         [("gpu", "/dev/dri/renderD129"), ("nvidia", None)])
        with self.assertRaises(ValueError):
            self.modes("16", True, True, ("0000:99:00.0",))

    def test_marketing_name_from_pci_ids(self):
        with tempfile.NamedTemporaryFile("w", suffix=".ids") as f:
            f.write("10de  NVIDIA Corporation\n\t2d04  GB206 [GeForce RTX 5060 Ti]\n"
                    "1002  Advanced Micro Devices, Inc. [AMD/ATI]\n\t13c0  Granite Ridge [Radeon Graphics]\n")
            f.flush()
            with mock.patch.object(gpu, "PCI_IDS", (f.name,)):
                self.assertEqual(gpu._pci_name("1002", "13c0"), "Radeon Graphics")
                self.assertIsNone(gpu._pci_name("1002", "ffff"))
        self.assertEqual(validate_setting("gpu", "0000:11:00.0"), "0000:11:00.0")
        with self.assertRaises(ValueError):
            validate_setting("gpu", "nvidia")


class NvidiaLayerTest(unittest.TestCase):
    def tarball(self, d, files):
        path = os.path.join(d, "a.tar.gz")
        with tarfile.open(path, "w:gz") as t:
            for name in files:
                data = name.encode()
                info = tarfile.TarInfo("./" + name)
                info.size = len(data)
                t.addfile(info, io.BytesIO(data))
        return path

    def test_only_what_is_used_is_unpacked(self):
        with tempfile.TemporaryDirectory() as d:
            guest = self.tarball(d, ["vendor/lib64/hw/hwcomposer.waydroid.so", "system/bin/surfaceflinger",
                                     "README.txt"])
            nvidia._unpack(guest, os.path.join(d, "g"), lambda n: n if n.startswith("vendor/") else None)
            self.assertEqual(os.listdir(os.path.join(d, "g")), ["vendor"])
            host = self.tarball(d, ["virgl_test_server", "virgl_render_server", "libvirglrenderer.so.1"])
            nvidia._unpack(host, os.path.join(d, "h"), nvidia._host_place)
            self.assertEqual(sorted(os.listdir(os.path.join(d, "h", "bin"))), ["virgl_render_server",
                                                                               "virgl_test_server"])
            self.assertTrue(os.path.isfile(os.path.join(d, "h", "lib", "libvirglrenderer.so.1")))


class AppArmorTest(unittest.TestCase):
    def test_stock_profile_renamed_with_uevent_writes_denied(self):
        stock = ("#include <tunables/global>\n"
                 "profile lxc-waydroid flags=(attach_disconnected, complain) {\n"
                 "  /sys** rw,\n"
                 "  /system/bin/app_process Pix -> lxc-waydroid//&android_app,\n}\n")
        text = container.apparmor_profile(stock, "lxc-waydroid")
        self.assertIn("profile lxc-waydroid-manager flags=(attach_disconnected, complain) {\n"
                      "  deny /**/uevent w,\n", text)
        self.assertIn("-> lxc-waydroid-manager//&android_app", text)
        self.assertNotIn("profile lxc-waydroid ", text)
        with self.assertRaises(RuntimeError):     # never load something that could replace stock's
            container.apparmor_profile("profile other {\n}\n", "lxc-waydroid")


class GsfIdTest(unittest.TestCase):
    def test_read_from_the_database_without_following_links(self):
        with tempfile.TemporaryDirectory() as d:
            inst = Instance.new("4", 4, 1000, "", {}, {})
            db_dir = os.path.join(d, "data", "com.google.android.gsf", "databases")
            os.makedirs(db_dir)
            db = sqlite3.connect(os.path.join(db_dir, "gservices.db"))
            db.execute("CREATE TABLE main (name TEXT, value TEXT)")
            db.execute("INSERT INTO main VALUES ('android_id', '4154426684555490429')")
            db.commit()
            db.close()
            with mock.patch.object(Instance, "data_dir", d):
                self.assertEqual(storage.gsf_id(inst), "4154426684555490429")
                # an Android or owner-planted link is never followed out of the data directory
                os.rename(os.path.join(d, "data"), os.path.join(d, "real"))
                os.symlink(os.path.join(d, "real"), os.path.join(d, "data"))
                self.assertEqual(storage.gsf_id(inst), "")


class LayerHelpersTest(unittest.TestCase):
    def test_extract_apk_libs_only_for_x86(self):
        with tempfile.TemporaryDirectory() as d:
            with zipfile.ZipFile(os.path.join(d, "App.apk"), "w") as z:
                z.writestr("lib/x86/libfoo.so", b"x")
                z.writestr("lib/x86/sub/libbar.so", b"x")      # not a top-level lib
            layers.extract_apk_libs(d)
            self.assertEqual(sorted(os.listdir(os.path.join(d, "lib", "x86"))), ["libfoo.so"])


if __name__ == "__main__":
    unittest.main()
