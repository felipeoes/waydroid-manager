# SPDX-License-Identifier: GPL-3.0-or-later
"""Root switch: put Magisk Delta into an instance's own overlay layer (Waydroid has no boot image)."""
import gzip
import hashlib
import os
import platform
import shutil
import urllib.request
import zipfile

from .. import paths
from .util import CommandError, log

# Same build waydroid_script installs, pinned to a commit and checked by hash
APK_URL = ("https://github.com/mistrmochov/magiskdeltaorig/raw/"
           "793a78430bac879b8eb9bf72d03003ebc205c294/app-release.apk")
APK_SHA256 = "a3c37c4172b6ce7fdfbcccb2a608ef84b0a267d7aaef7c183047adceb8494c80"
APK_CACHE = paths.STATE_DIR + "/magisk-delta.apk"
ABIS = {"x86_64": "x86_64", "i686": "x86", "aarch64": "arm64-v8a", "armv7l": "armeabi-v7a"}
ASSETS = ("addon.d.sh", "boot_patch.sh", "stub.apk", "util_functions.sh")
SOCK = "/dev/magisk_iqeoVo2mDrO"
MAGISK = "/system/etc/init/magisk"
# Paths (below the overlay root) this module owns, in the lower layer and in the instance's upper layer
OWNED = ("system/etc/init/magisk", "system/etc/init/bootanim.rc", "system/etc/init/bootanim.rc.gz",
         "system/addon.d/99-magisk.sh")

BOOTANIM = """
service bootanim /system/bin/bootanimation
    class core animation
    user graphics
    group graphics audio
    disabled
    oneshot
    ioprio rt 0
    task_profiles MaxPerformance

"""
SETUP = """
on post-fs-data
    start logd
    exec u:r:su:s0 root root -- {m}/magiskpolicy --live --magisk
    exec u:r:magisk:s0 root root -- {m}/magiskpolicy --live --magisk
    exec u:r:update_engine:s0 root root -- {m}/magiskpolicy --live --magisk
    mkdir {s} 700
    exec u:r:su:s0 root root -- {m}/{bin} --auto-selinux --setup-sbin {m} {s}
    exec u:r:su:s0 root root -- {s}/magisk --auto-selinux --post-fs-data

on nonencrypted
    exec u:r:su:s0 root root -- {s}/magisk --auto-selinux --service

on property:vold.decrypt=trigger_restart_framework
    exec u:r:su:s0 root root -- {s}/magisk --auto-selinux --service

on property:sys.boot_completed=1
    mkdir /data/adb/magisk 755
    exec u:r:su:s0 root root -- /system/bin/cp -rf {m}/. /data/adb/magisk/
    exec u:r:su:s0 root root -- {s}/magisk --auto-selinux --boot-complete

on property:init.svc.zygote=restarting
    exec u:r:su:s0 root root -- {s}/magisk --auto-selinux --zygote-restart

on property:init.svc.zygote=stopped
    exec u:r:su:s0 root root -- {s}/magisk --auto-selinux --zygote-restart
"""


def _abi():
    abi = ABIS.get(platform.machine())
    if not abi:
        raise CommandError("the Root switch does not support the {} architecture".format(platform.machine()))
    return abi


def ensure_apk():
    """Download the pinned APK once; returns its path."""
    if os.path.isfile(APK_CACHE) and _sha256(APK_CACHE) == APK_SHA256:
        return APK_CACHE
    log.info("downloading Magisk Delta")
    tmp = APK_CACHE + ".tmp"
    try:
        with urllib.request.urlopen(APK_URL, timeout=60) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f)
    except OSError as e:
        raise CommandError("cannot download Magisk Delta for the Root switch: {}".format(e))
    if _sha256(tmp) != APK_SHA256:
        os.unlink(tmp)
        raise CommandError("the downloaded Magisk Delta does not match the expected checksum")
    os.replace(tmp, APK_CACHE)
    return APK_CACHE


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def remove(inst):
    """Take Magisk out of the lower layer and out of the instance's own changes."""
    for base in ("overlay", "overlay_rw/system"):
        for rel in OWNED:
            p = os.path.join(inst.dir, base, rel)
            if os.path.isdir(p) and not os.path.islink(p):
                shutil.rmtree(p)
            elif os.path.lexists(p):
                os.unlink(p)
    shutil.rmtree(os.path.join(inst.dir, "overlay/sbin"), ignore_errors=True)
    old_su = os.path.join(inst.dir, "overlay/system/xbin/su")  # the first Root switch's own su
    if os.path.lexists(old_su):
        os.unlink(old_su)


def install(inst):
    abi = _abi()
    remove(inst)
    root = os.path.join(inst.dir, "overlay")
    mdir = os.path.join(root, MAGISK.lstrip("/"))
    os.makedirs(mdir)
    os.makedirs(os.path.join(root, "sbin"), exist_ok=True)
    binary = "magisk64" if abi.endswith("64") else "magisk32"
    with zipfile.ZipFile(ensure_apk()) as z:
        for name in z.namelist():
            base = os.path.basename(name)
            if name.startswith("lib/" + abi + "/lib") and base.endswith(".so"):
                _put(z, name, os.path.join(mdir, base[3:-3]), 0o755)
            elif name.startswith("assets/chromeos/") and not name.endswith("/"):
                _put(z, name, os.path.join(mdir, name[len("assets/"):]), 0o644)
            elif name.startswith("assets/") and base in ASSETS:
                _put(z, name, os.path.join(mdir, base), 0o644)
    shutil.copyfile(APK_CACHE, os.path.join(mdir, "magisk.apk"))
    # Magisk's own updater rewrites bootanim.rc, so it keeps a pristine copy next to it
    rc = os.path.join(root, "system/etc/init/bootanim.rc")
    with gzip.open(rc + ".gz", "wb") as f:
        f.write(BOOTANIM.encode())
    with open(rc, "w") as f:
        f.write(BOOTANIM + SETUP.format(m=MAGISK, s=SOCK, bin=binary))
    for d, _, files in os.walk(mdir):
        os.chown(d, 0, 2000)
        os.chmod(d, 0o755)
        for n in files:
            os.chown(os.path.join(d, n), 0, 2000)


def _put(z, name, dst, mode):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with z.open(name) as src, open(dst, "wb") as out:
        shutil.copyfileobj(src, out)
    os.chmod(dst, mode)
