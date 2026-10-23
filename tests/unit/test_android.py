# SPDX-License-Identifier: GPL-3.0-or-later
import hashlib
import io
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from unittest import mock

from waydroid_manager import catalog, gpu
from waydroid_manager.daemon import container, gapps, images, layers, main, nvidia, storage, util
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
            self.assertIn(v.get("software", True), (True, "vkms", False), key)

    def test_android_is_chosen_at_creation(self):
        self.assertEqual(validate_setting("android", "16"), "16")
        with self.assertRaises(ValueError):
            validate_setting("android", "12")
        self.assertIn("android", CREATE_ONLY)
        self.assertEqual(Instance.new("5", 5, 1000, "", {}, {}).get("android"), catalog.DEFAULT)

    def test_normal_adb_for_images_without_a_default(self):
        for key in ("15", "16"):
            with self.subTest(android=key):
                self.assertEqual(catalog.get(key, "props")["persist.sys.usb.config"], "adb")

    def test_a_version_we_dont_offer(self):
        # #0's before its first sync (None, '' over D-Bus) or stock on Android 10: defaults, no KeyError
        self.assertTrue(catalog.get(None, "software", True))
        self.assertIsNone(catalog.get("", "gb"))
        check = main.Manager.check_android
        with mock.patch.object(main.gpu, "gpus", return_value=[]):
            check(None, None, {"root": "true", "gpu": "software"})     # checked at start instead
            check(None, "16", {"root": "true"})                         # every version can have root
            with self.assertRaisesRegex(main.Error, "software"):
                check(None, "15", {"gpu": "software"})


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
        self.make_set("250-251", android="17", built="250")     # no device runs 17
        self.make_set("300-301", stock="true", built="300")     # stock's, for #0
        os.makedirs(os.path.join(self.tmp.name, "400-401.tmp"))  # an install in progress
        os.symlink("300-301", images.CURRENT)
        self.assertEqual(images.latest("16"), "200-201")
        self.assertEqual(images.latest("15"), "")
        # a stopped device still on the older build keeps it, and the newer one it switches to
        images.gc(lambda: [("100-101", "16"), ("", "")])
        self.assertEqual(images.available(), ["100-101", "200-201", "300-301"])
        images.gc(lambda: [("200-201", "16")])                  # it switched
        self.assertEqual(images.available(), ["200-201", "300-301"])

    def test_update_keeps_the_new_builds(self):
        self.make_set("100-101", android="16", built="100")
        self.make_set("300-301", stock="true", built="300")
        os.symlink("300-301", images.CURRENT)

        def install(key):
            self.make_set("200-201", android=key, built="200")
            return "200-201"
        devices = lambda: [("100-101", "16"), ("300-301", "")]     # noqa: E731
        with mock.patch.object(images, "install", side_effect=install) as inst, \
                mock.patch.object(images, "stock_is_newer", return_value=False):
            self.assertEqual(images.update(devices), ["16"])
        inst.assert_called_once_with("16")      # only the versions devices run
        self.assertEqual(images.available(), ["100-101", "200-201", "300-301"])

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

    def test_play_services_repair_only_for_affected_system_build(self):
        with mock.patch.object(gapps, "gms16_layer", return_value="/gms16", create=True):
            for sid, android, built, expected in (
                    ("old", "16", "1784256682", "/gms16"),
                    ("new", "16", "1790000000", None),
                    ("other", "13", "1784256682", None)):
                self.make_set(sid, android=android, built=built)
                self.assertEqual(images.gapps_layer(sid), expected)


class LayerTest(unittest.TestCase):
    def test_play_services_replaces_factory_apk_with_matching_permissions(self):
        with tempfile.TemporaryDirectory() as d:
            apk = io.BytesIO()
            with zipfile.ZipFile(apk, "w") as z:
                z.writestr("lib/x86_64/libgms.so", b"native library")
            apex = io.BytesIO()
            with zipfile.ZipFile(apex, "w") as z:
                z.writestr("apex_payload.img", b"payload")
            parts = []
            for n, data in enumerate((apex.getvalue()[:20], apex.getvalue()[20:])):
                part = os.path.join(d, str(n))
                with open(part, "wb") as f:
                    f.write(data)
                parts.append(part)

            def mount(payload, mnt):
                with open(payload, "rb") as f:
                    self.assertEqual(f.read(), b"payload")
                app = os.path.join(mnt, "priv-app/PrebuiltGmsCoreVic@261136038")
                os.makedirs(app)
                with open(os.path.join(app, "PrebuiltGmsCoreVic.apk"), "wb") as f:
                    f.write(apk.getvalue())
                os.makedirs(os.path.join(mnt, "etc/permissions"))
                with open(os.path.join(mnt, "etc/permissions/gms.xml"), "w") as f:
                    f.write('<permissions><privapp-permissions package="com.google.android.gms">'
                            '<permission name="android.permission.GET_ROLE_HOLDERS"/>'
                            '</privapp-permissions></permissions>')

            def unmount(mnt):
                shutil.rmtree(mnt)
                os.mkdir(mnt)

            with mock.patch.object(gapps.paths, "STATE_DIR", d), \
                    mock.patch.object(gapps, "download", side_effect=parts, create=True), \
                    mock.patch.object(gapps, "mount_image", side_effect=mount), \
                    mock.patch.object(gapps, "umount_tree", side_effect=unmount), \
                    mock.patch.object(layers, "whiteout",
                                      side_effect=lambda p: os.makedirs(os.path.dirname(p))), \
                    mock.patch.object(layers, "set_modes"):
                layer = gapps.gms16_layer()
                self.assertEqual(gapps.gms16_layer(), layer)   # reuse without downloading or mounting
            product = os.path.join(layer, "system/product")
            self.assertEqual(os.listdir(os.path.join(product, "priv-app")), ["GmsCore"])
            app = os.path.join(product, "priv-app/GmsCore")
            with open(os.path.join(app, "GmsCore.apk"), "rb") as f:
                self.assertEqual(f.read(), apk.getvalue())       # preserve Google's signed APK bytes
            with open(os.path.join(app, "lib/x86_64/libgms.so"), "rb") as f:
                self.assertEqual(f.read(), b"native library")
            perms = ET.parse(os.path.join(product, "etc/permissions/gms.xml"))
            self.assertEqual(perms.find("privapp-permissions").get("package"), "com.google.android.gms")
            self.assertEqual(perms.find(".//permission").get("name"), "android.permission.GET_ROLE_HOLDERS")
            self.assertFalse(os.path.exists(os.path.join(product, "apex")))
            self.assertFalse(os.path.exists(layer + ".tmp"))

    def test_play_services_download_failure_does_not_publish_layer(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(gapps.paths, "STATE_DIR", d), \
                mock.patch.object(gapps, "download", side_effect=util.CommandError("bad checksum"), create=True):
            with self.assertRaisesRegex(util.CommandError, "bad checksum"):
                gapps.gms16_layer()
            self.assertEqual(os.listdir(os.path.join(d, "gapps")), [])

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

    def test_download_resumes_after_an_early_close(self):
        body = b"0123456789"
        sha = hashlib.sha256(body).hexdigest()

        class Response(io.BytesIO):
            def __init__(self, data, status, headers):
                super().__init__(data)
                self.status, self.headers = status, headers

        replies = [Response(body[:4], 200, {"Content-Length": "10"}),       # closed early, no error
                   Response(body[4:], 206, {"Content-Range": "bytes 4-9/10"})]
        ranges = []

        def urlopen(req, timeout):
            ranges.append(req.get_header("Range"))
            return replies.pop(0)
        with tempfile.TemporaryDirectory() as d:
            dest = os.path.join(d, "a.zip")
            with open(dest + ".0123456789abcdef.part", "wb") as f:
                f.write(b"another build's")       # never extended
            with mock.patch.object(util.urllib.request, "urlopen", urlopen):
                self.assertEqual(util.download("https://x/a.zip", sha, dest, "a"), dest)
            with open(dest, "rb") as f:
                self.assertEqual(f.read(), body)
            self.assertEqual(ranges, [None, "bytes=4-"])
            self.assertEqual(os.listdir(d), ["a.zip"])

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


class StockStopTest(unittest.TestCase):
    def test_rotation_restored_before_stopping_frozen_stock(self):
        inst = Instance.new("0", 0, 1000, "", {}, {})
        for initial in ("RUNNING", "FROZEN", "STOPPED"):
            state, actions = [initial], []

            def run(cmd, **kwargs):
                actions.append(cmd[0])
                if cmd[0] == "lxc-unfreeze":
                    state[0] = "RUNNING"
                elif cmd[0] == "lxc-stop":
                    state[0] = "STOPPED"

            def attach(iid, cmd, **kwargs):
                self.assertEqual(state[0], "RUNNING")
                self.assertIn("wm fixed-to-user-rotation default", cmd[-1])
                actions.append("restore rotation")

            with self.subTest(state=initial), \
                    mock.patch.object(container, "lxc_state", side_effect=lambda _: state[0]), \
                    mock.patch.object(container, "run", side_effect=run), \
                    mock.patch.object(container, "attach", side_effect=attach), \
                    mock.patch.object(container, "cleanup") as cleanup:
                container.stop(inst)
                expected = [] if initial == "STOPPED" else ["restore rotation", "lxc-stop", "lxc-wait"]
                if initial == "FROZEN":
                    expected = ["lxc-unfreeze", "lxc-wait"] + expected
                self.assertEqual(actions, expected)
                cleanup.assert_called_once_with(inst, False)


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
        self.assertEqual(self.modes("11", True, True), [("nvidia", None), ("software", None)])

    def test_desktop_on_another_gpu_or_none(self):
        self.assertEqual(self.modes("16", False, True)[0], ("gpu", None))       # stock Waydroid's pick
        self.assertEqual(self.modes("17", False, False)[0], ("vkms", None))
        self.assertEqual(self.modes(None, False, False)[0], ("software", None))  # stock on 10

    def test_picked_gpu(self):
        self.assertEqual(self.modes("16", True, True, ("0000:11:00.0", "0000:01:00.0")),
                         [("gpu", "/dev/dri/renderD129"), ("nvidia", None)])
        with self.assertRaises(ValueError):
            self.modes("16", True, True, ("0000:99:00.0",))

    def test_android_15_refuses_software(self):
        inst = Instance.new("5", 5, 1000, "", {}, {})
        inst.cfg["instance"]["android"] = "15"
        with mock.patch.object(gpu, "mode", return_value=("software", None)), \
                self.assertRaisesRegex(RuntimeError, "can't render in software"):
            container.render_for(inst)

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


class StockPropsTest(unittest.TestCase):
    STOCK = {"ro.dalvik.vm.native.bridge": "libndk_translation.so", "ro.ndk_translation.version": "0.2.3",
             "ro.hardware.gralloc": "minigbm_gbm_mesa", "persist.waydroid.multi_windows": "true"}

    def props(self, index, android, stock_android="13", arm=None, mode="gpu", **settings):
        inst = Instance.new(str(index), index, 1000, "", {}, {})
        inst.cfg["instance"].update(settings, android=android)
        cfg = {"properties": dict(self.STOCK)}
        with mock.patch.object(container.stock, "load_stock_cfg", return_value=cfg), \
                mock.patch.object(container, "stock_android", return_value=stock_android):
            return container.stock_props(inst, arm, mode)

    def test_stock_arm_translation_only_on_stock_android(self):
        self.assertIn("ro.dalvik.vm.native.bridge", self.props(3, "13"))       # its overlay has it
        self.assertNotIn("ro.dalvik.vm.native.bridge", self.props(3, "14"))    # the image's own runs
        self.assertNotIn("ro.ndk_translation.version", self.props(3, "13", arm="/houdini"))  # ours replaces it
        self.assertNotIn("ro.dalvik.vm.native.bridge", self.props(3, "13", arm_translation="none"))
        self.assertEqual(self.props(3, "14")["persist.waydroid.multi_windows"], "true")

    def test_clicks_are_touches_unless_stock_says_otherwise(self):
        self.assertEqual(self.props(3, "13")["persist.waydroid.fake_touch"], "*")
        self.STOCK = dict(self.STOCK, **{"persist.waydroid.fake_touch": "com.example.game"})
        self.assertEqual(self.props(3, "13")["persist.waydroid.fake_touch"], "com.example.game")

    def test_stock_graphics_choice_only_for_0_as_stock_renders(self):
        self.assertIn("ro.hardware.gralloc", self.props(0, "13"))
        self.assertNotIn("ro.hardware.gralloc", self.props(0, "13", mode="nvidia"))
        self.assertNotIn("ro.hardware.gralloc", self.props(0, "13", gpu="0000:0a:00.0"))
        self.assertNotIn("ro.hardware.gralloc", self.props(3, "13"))    # its own Graphics setting decides


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

    def test_rows_still_in_the_write_ahead_log(self):
        with tempfile.TemporaryDirectory() as d:
            inst = Instance.new("4", 4, 1000, "", {}, {})
            db_dir = os.path.join(d, "data", "com.google.android.gsf", "databases")
            os.makedirs(db_dir)
            db = sqlite3.connect(os.path.join(db_dir, "gservices.db"))
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA wal_autocheckpoint=0")       # GMS keeps it open: no checkpoint yet
            db.execute("CREATE TABLE main (name TEXT, value TEXT)")
            db.execute("INSERT INTO main VALUES ('android_id', '4154426684555490429')")
            db.commit()
            with mock.patch.object(Instance, "data_dir", d):
                self.assertEqual(storage.gsf_id(inst), "4154426684555490429")
                db.close()
                # a FIFO planted there is refused, not waited on
                for f in os.listdir(db_dir):
                    os.unlink(os.path.join(db_dir, f))
                os.mkfifo(os.path.join(db_dir, "gservices.db"))
                self.assertEqual(storage.gsf_id(inst), "")


class LayerHelpersTest(unittest.TestCase):
    def test_extract_apk_libs_only_for_x86(self):
        with tempfile.TemporaryDirectory() as d:
            with zipfile.ZipFile(os.path.join(d, "App.apk"), "w") as z:
                z.writestr("lib/x86/libfoo.so", b"x")
                z.writestr("lib/x86/sub/libbar.so", b"x")      # not a top-level lib
            layers.extract_apk_libs(d)
            self.assertEqual(sorted(os.listdir(os.path.join(d, "lib", "x86"))), ["libfoo.so"])


class EmulatorStealthTest(unittest.TestCase):
    @staticmethod
    def make_prop_area(props):
        import struct
        buf = bytearray(128)
        struct.pack_into("<IIII", buf, 0, 0, 0, 0x504F5250, 0xFC6ED0AB)
        for k, v in props.items():
            vb = v.encode()
            buf.extend(struct.pack("<I92s", len(vb) << 24, vb))
            buf.extend(k.encode() + b"\0")
            while len(buf) % 4:
                buf.append(0)
        return bytes(buf)

    @staticmethod
    def read_prop(data, key):
        import struct
        idx = data.find(key.encode() + b"\0", 128 + 96)
        length = struct.unpack_from("<I", data, idx - 96)[0] >> 24
        return data[idx - 92:idx - 92 + length].decode()

    def test_armtrans_renames_houdini_and_whiteouts_base_houdini(self):
        from waydroid_manager.daemon import armtrans
        with tempfile.TemporaryDirectory() as d:
            zpath = os.path.join(d, "arm.zip")
            with zipfile.ZipFile(zpath, "w") as z:
                z.writestr("top/prebuilts/lib64/libhoudini.so", b"ELF\0libhoudini.so\0")
                z.writestr("top/prebuilts/lib64/arm64/nb/libtcb.so", b"NEEDED\0libhoudini.so\0")
            whiteouts = []
            with mock.patch.object(layers, "whiteout", side_effect=whiteouts.append):
                h_dir = os.path.join(d, "houdini")
                armtrans._unpack(zpath, h_dir, "houdini")
                self.assertEqual(armtrans.PROPS["houdini"]["ro.dalvik.vm.native.bridge"], "libnb_impl.so")
                with open(os.path.join(h_dir, "system/lib64/libnb_impl.so"), "rb") as f:
                    self.assertEqual(f.read(), b"ELF\0libnb_impl.so\0")
                with open(os.path.join(h_dir, "system/lib64/arm64/nb/libtcb.so"), "rb") as f:
                    self.assertEqual(f.read(), b"NEEDED\0libnb_impl.so\0")
                self.assertIn(os.path.join(h_dir, "system/lib64/libhoudini.so"), whiteouts)
                whiteouts.clear()
                n_dir = os.path.join(d, "libndk")
                armtrans._unpack(zpath, n_dir, "libndk")
                self.assertIn(os.path.join(n_dir, "system/lib64/libhoudini.so"), whiteouts)

    def test_cpuinfo_hides_host_x86_markers(self):
        text = container.cpuinfo_text(4)
        self.assertEqual(text.count("processor\t:"), 4)
        for token in ("intel", "amd", "hypervisor"):
            self.assertNotIn(token, text.lower())

    def test_hide_emulator_props_updates_in_place_and_replaces_dalvik_prop_inode(self):
        with tempfile.TemporaryDirectory() as d:
            props_dir = os.path.join(d, "dev", "__properties__")
            os.makedirs(props_dir)
            with open(os.path.join(props_dir, "u:object_r:build_prop:s0"), "wb") as f:
                f.write(self.make_prop_area({
                    "ro.product.cpu.abilist": "x86_64,x86,arm64-v8a,armeabi-v7a,armeabi",
                    "ro.product.cpu.abilist32": "x86,armeabi-v7a,armeabi",
                    "ro.product.cpu.abilist64": "x86_64,arm64-v8a",
                    "ro.product.cpu.abi": "x86_64",
                    "ro.build.product": "waydroid_x86_64",
                    "ro.build.flavor": "lineage_waydroid_x86_64-userdebug",
                    "ro.build.tags": "test-keys",
                }))
            with open(os.path.join(props_dir, "u:object_r:build_vendor_prop:s0"), "wb") as f:
                f.write(self.make_prop_area({
                    "ro.product.vendor.device": "waydroid_x86_64",
                    "ro.vendor.product.cpu.abilist": "x86_64,x86,arm64-v8a,armeabi-v7a,armeabi",
                }))
            with open(os.path.join(props_dir, "u:object_r:bootloader_prop:s0"), "wb") as f:
                f.write(self.make_prop_area({"ro.boot.hardware": "waydroid"}))
            with open(os.path.join(props_dir, "u:object_r:dalvik_config_prop:s0"), "wb") as f:
                f.write(self.make_prop_area({"ro.dalvik.vm.native.bridge": "libndk_translation.so"}))
            dalvik_path = os.path.join(props_dir, "u:object_r:dalvik_prop:s0")
            with open(dalvik_path, "wb") as f:
                f.write(self.make_prop_area({"ro.dalvik.vm.isa.arm": "x86", "ro.dalvik.vm.isa.arm64": "x86_64"}))
            # Simulate system_server holding an open fd to the boot-time dalvik_prop inode
            ss_fd = os.open(dalvik_path, os.O_RDONLY)
            try:
                inst = Instance.new("11", 11, 1000, "", {}, {})
                inst.set("device_model", "pixel_8_pro")
                inst.set("arm_translation", "libndk")
                with mock.patch.object(container, "container_pid", return_value=os.getpid()), \
                        mock.patch.object(container, "open_in_container",
                                          side_effect=lambda _, rel, flags: util.open_beneath(d, rel, flags)):
                    container.hide_emulator_props(inst)
                with open(os.path.join(props_dir, "u:object_r:build_prop:s0"), "rb") as f:
                    build_data = f.read()
                self.assertEqual(self.read_prop(build_data, "ro.product.cpu.abilist"),
                                 "arm64-v8a,armeabi-v7a,armeabi")
                self.assertEqual(self.read_prop(build_data, "ro.product.cpu.abi"), "arm64-v8a")
                self.assertEqual(self.read_prop(build_data, "ro.build.product"), "husky")
                self.assertEqual(self.read_prop(build_data, "ro.build.flavor"), "husky-user")
                self.assertEqual(self.read_prop(build_data, "ro.build.tags"), "release-keys")
                with open(os.path.join(props_dir, "u:object_r:build_vendor_prop:s0"), "rb") as f:
                    vendor_data = f.read()
                self.assertEqual(self.read_prop(vendor_data, "ro.product.vendor.device"), "husky")
                self.assertEqual(self.read_prop(vendor_data, "ro.vendor.product.cpu.abilist"),
                                 "arm64-v8a,armeabi-v7a,armeabi")
                with open(os.path.join(props_dir, "u:object_r:bootloader_prop:s0"), "rb") as f:
                    self.assertEqual(self.read_prop(f.read(), "ro.boot.hardware"), "husky")
                with open(os.path.join(props_dir, "u:object_r:dalvik_config_prop:s0"), "rb") as f:
                    self.assertEqual(self.read_prop(f.read(), "ro.dalvik.vm.native.bridge"), "0")
                # system_server's existing inode keeps x86_64; apps opening the path see ""
                self.assertEqual(self.read_prop(os.pread(ss_fd, 4096, 0), "ro.dalvik.vm.isa.arm64"), "x86_64")
                with open(dalvik_path, "rb") as f:
                    self.assertEqual(self.read_prop(f.read(), "ro.dalvik.vm.isa.arm64"), "")
            finally:
                os.close(ss_fd)

    def test_fix_ndk_translation_patches_pinned_build(self):
        with tempfile.TemporaryDirectory() as d:
            inst = Instance.new("4", 4, 1000, "", {}, {})
            lib = os.path.join(d, "rootfs/system/lib64/libndk_translation.so")
            os.makedirs(os.path.dirname(lib))
            raw = b"\x00" * 64
            sha = hashlib.sha256(raw).hexdigest()
            with open(lib, "wb") as f:
                f.write(raw)
            with mock.patch.object(Instance, "dir", d), \
                    mock.patch.object(Instance, "rootfs", os.path.join(d, "rootfs")), \
                    mock.patch.dict(container.NDK_TRANSLATION_PATCHES, {sha: (0x10, "4885db7407")}), \
                    mock.patch.object(container, "bind_mount") as bm:
                container.fix_ndk_translation(inst)
                bm.assert_called_once()
                with open(os.path.join(d, "libndk_translation.so"), "rb") as f:
                    patched = f.read()
                self.assertEqual(patched[0x10:0x15], bytes.fromhex("4885db7407"))


if __name__ == "__main__":
    unittest.main()
