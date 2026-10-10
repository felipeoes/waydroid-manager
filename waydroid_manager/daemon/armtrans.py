# SPDX-License-Identifier: GPL-3.0-or-later
"""ARM translation (houdini or libndk_translation): one shared, read-only overlay layer per build."""
import os
import platform
import shutil
import zipfile

from .. import paths
from . import layers
from .util import CommandError, log

# The builds waydroid_script installs, pinned to a commit and checked by hash; keyed by Android SDK level
_HOUDINI = "https://github.com/supremegamers/vendor_intel_proprietary_houdini/archive/"
_NDK = "https://github.com/supremegamers/vendor_google_proprietary_ndk_translation-prebuilt/archive/"
BUILDS = {
    ("houdini", "30"): (_HOUDINI + "81f2a51ef539a35aead396ab7fce2adf89f46e88.zip",
                        "c46fce463ca55eefa66965119e70d80255ca2f5f26ea31ce7ca010b7829440ed"),
    ("houdini", "33"): (_HOUDINI + "2f8f088671182e17e67321e098e8411a3972a628.zip",
                        "2e82cdc88ddc4d418f7fb861aeebb7c49c83a91b97f57da10510b5e1146a4ed5"),
    # Android 14's image ships an older Houdini (GoogleGame_com1.2) without its ARM libraries;
    # pairip-protected apps abort in it. The Android 13 build runs them on 14.
    ("houdini", "34"): (_HOUDINI + "2f8f088671182e17e67321e098e8411a3972a628.zip",
                        "2e82cdc88ddc4d418f7fb861aeebb7c49c83a91b97f57da10510b5e1146a4ed5"),
    ("libndk", "30"): (_NDK + "9324a8914b649b885dad6f2bfd14a67e5d1520bf.zip",
                       "87089b896ce6fed313dd5c2dd1bf22db857621c27e04471aadda69a1a2795fa1"),
    ("libndk", "33"): (_NDK + "68734c52556d3d7a6db34c603dd9276915c29f2f.zip",
                       "a142d1586c9eafb5edf62277110f2128bc03066179a6783bcaa33ee322e1cbd0"),
}
_ABIS = {
    "ro.product.cpu.abilist": "x86_64,x86,arm64-v8a,armeabi-v7a,armeabi",
    "ro.product.cpu.abilist32": "x86,armeabi-v7a,armeabi",
    "ro.product.cpu.abilist64": "x86_64,arm64-v8a",
    "ro.enable.native.bridge.exec": "1",
    "ro.dalvik.vm.isa.arm": "x86",
    "ro.dalvik.vm.isa.arm64": "x86_64",
}
PROPS = {
    "houdini": dict(_ABIS, **{"ro.dalvik.vm.native.bridge": "libhoudini.so"}),
    "libndk": dict(_ABIS, **{"ro.dalvik.vm.native.bridge": "libndk_translation.so",
                             "ro.vendor.enable.native.bridge.exec": "1",
                             "ro.vendor.enable.native.bridge.exec64": "1",
                             "ro.ndk_translation.version": "0.2.3"}),
}
ALL_PROPS = set(PROPS["houdini"]) | set(PROPS["libndk"])
# Houdini's own rc reads its binfmt_misc entries from /vendor; this layer puts them in /system.
# The entries are host-wide (privileged container) and named alike for both builds: the first
# instance to register them wins until reboot, which only matters for exec'ing ARM binaries.
HOUDINI_RC = """on early-init
    mount binfmt_misc binfmt_misc /proc/sys/fs/binfmt_misc

on property:ro.enable.native.bridge.exec=1
    copy /system/etc/binfmt_misc/arm_exe /proc/sys/fs/binfmt_misc/register
    copy /system/etc/binfmt_misc/arm_dyn /proc/sys/fs/binfmt_misc/register
    copy /system/etc/binfmt_misc/arm64_exe /proc/sys/fs/binfmt_misc/register
    copy /system/etc/binfmt_misc/arm64_dyn /proc/sys/fs/binfmt_misc/register
"""

def layer(kind, sdk):
    """Directory to mount below the instance's own overlay layer, or None (off, an image with its
    own translation, no build for this Android, or the download failed: the instance then
    starts without ARM translation)."""
    if kind == "none":
        return None
    build = BUILDS.get((kind, sdk))
    if platform.machine() != "x86_64" or not build:
        log.info("no %s build of ours for %s Android SDK %r; the image's own translation, if any, is used",
                 kind, platform.machine(), sdk)
        return None
    url, sha = build
    # ponytail: layers of older pins are never removed; add cleanup if the pins change often
    d = os.path.join(paths.STATE_DIR, "arm", "{}-{}".format(kind, sha[:12]))
    try:
        return layers.layer(d, url, sha, lambda z, tmp: _unpack(z, tmp, kind), kind)
    except (CommandError, OSError, zipfile.BadZipFile) as e:
        log.warning("starting without ARM translation: %s", e)
        return None


def _unpack(zpath, tmp, kind):
    """The zip's prebuilts/ become tmp/system/."""
    sysdir = os.path.join(tmp, "system")
    with zipfile.ZipFile(zpath) as z:
        for name in z.namelist():
            top, _, rel = name.partition("/prebuilts/")
            if not rel or "/" in top or name.endswith("/"):
                continue
            dst = os.path.join(sysdir, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with z.open(name) as src, open(dst, "wb") as out:
                shutil.copyfileobj(src, out)
    if kind == "houdini":
        with open(os.path.join(sysdir, "etc/init/houdini.rc"), "w") as f:
            f.write(HOUDINI_RC)
