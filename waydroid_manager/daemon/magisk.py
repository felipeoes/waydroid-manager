# SPDX-License-Identifier: GPL-3.0-or-later
"""Root switch: put Magisk into an instance's own overlay layer (Waydroid has no boot image)."""
import gzip
import hashlib
import os
import platform
import shutil
import threading
import zipfile

from .. import paths
from .util import CommandError, download

# Official v31.0: Zygisk knows the Zygote signatures of 11 to 17 (Magisk Delta 26.3 stopped at 13,
# Kitsune 27.2 at 16 QPR1). 31.1 drops the su prompt and the stub, so newer builds need a new look.
APK_URL = "https://github.com/topjohnwu/Magisk/releases/download/v31.0/Magisk-v31.0.apk"
APK_SHA256 = "2c8a488b9a5293e578e95ae4f07e3c57aba4feec4a52ca4dd852a2692d6dd4e8"
APK_CACHE = paths.STATE_DIR + "/magisk-v31.0.apk"
STAMP = "magisk.sha256"  # in the instance dir: which build its lower layer holds
ABIS = {"x86_64": ("x86_64", "x86"), "aarch64": ("arm64-v8a", "armeabi-v7a")}
# Waydroid runs Android without SELinux, which the daemon doesn't expect. (offset, was, becomes)
# 1. It serves Zygisk only to clients whose SELinux label is u:r:zygote:s0, so every Zygisk request
#    was refused. The check becomes "uid == 0": zygote's children are root until they specialize.
# 2. Reading a socket's label fails with an error other than "no label", so zygiskd dropped every
#    companion connection and modules with a companion hung app startup. Any error means no label.
PATCH = {
    "x86_64": (
        # call eq(label, "u:r:zygote:s0"); cmpl $0,uid; mov %al,is_zygote
        # -> cmpl $0,uid; sete %al; mov %al,is_zygote; nop; nop
        (0x27b2d, "e84e0d0000837c2410008844240f", "837c2410000f94c08844240f9090"),
        # fgetxattr failed: cmp $ENODATA,%eax; jne fail -> nop; nop
        (0x3b232, "7514", "9090"),
    ),
    "arm64-v8a": (
        # bl eq; mov w25, w0 -> cmp w20 (uid), #0; cset w25, eq
        (0x27b00, "dd020094f903002a", "9f020071f9179f1a"),
        # fgetxattr failed: cmp w0, #ENODATA; b.ne fail -> nop
        (0x381e8, "c1000054", "1f2003d5"),
    ),
}
TMP = "/debug_ramdisk"  # Magisk only runs from /debug_ramdisk or /sbin
MAGISK = "/system/etc/init/magisk"
# Paths (below the overlay root) this module owns, in the lower layer and in the instance's upper layer
OWNED = ("system/etc/init/magisk", "system/etc/init/bootanim.rc", "system/etc/init/bootanim.rc.gz",
         "system/addon.d/99-magisk.sh")

# Android 16's init runs each exec in a cgroup of its own and kills the whole cgroup when it ends,
# which took the Magisk daemon (forked by --post-fs-data) with it: start it in init's own cgroup.
# 11 to 15 have no /init cgroup (the write fails harmlessly); 17 runs everything in /init anyway.
INIT_CGROUP = "echo 0 > /sys/fs/cgroup/init/cgroup.procs"
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
# Earlier versions installed Magisk Delta, whose app (io.github.huskydg.magisk) the daemon now ignores
SETUP = """
on post-fs-data
    start logd
    mount tmpfs magisk {t} mode=0755
    mkdir /data/adb 700
    exec u:r:su:s0 root root -- /system/bin/sh {m}/setup.sh
    exec u:r:su:s0 root root -- /system/bin/sh -c "{cg}; exec {t}/magisk --post-fs-data"

on nonencrypted
    exec u:r:su:s0 root root -- {t}/magisk --service

on property:vold.decrypt=trigger_restart_framework
    exec u:r:su:s0 root root -- {t}/magisk --service

on property:sys.boot_completed=1
    exec u:r:su:s0 root root -- /system/bin/sh -c "pm path io.github.huskydg.magisk > /dev/null && pm uninstall io.github.huskydg.magisk"
    exec u:r:su:s0 root root -- {t}/magisk --boot-complete

on property:init.svc.zygote=restarting
    exec u:r:su:s0 root root -- {t}/magisk --zygote-restart

on property:init.svc.zygote=stopped
    exec u:r:su:s0 root root -- {t}/magisk --zygote-restart
"""
# What upstream's scripts/avd_setup.sh does before the daemon starts. init's umask is 077, which
# would keep apps' su off the daemon's socket.
SETUP_SH = """umask 022
M={m}
T={t}
B=/data/adb/magisk
rm -rf $B
mkdir -p $B /data/adb/modules /data/adb/post-fs-data.d /data/adb/service.d
for f in $M/*; do
    case ${{f##*/}} in stub.apk|setup.sh) ;; *) cp -af $f $B/ ;; esac
done
cp -af $M/magisk $M/magisk32 $M/magiskpolicy $M/stub.apk $T/
ln -s ./magisk $T/su
ln -s ./magisk $T/resetprop
ln -s ./magiskpolicy $T/supolicy
mkdir -p $T/.magisk/device $T/.magisk/worker
mount -t tmpfs -o mode=0755 worker $T/.magisk/worker
$M/busybox mount --make-private $T/.magisk/worker
touch $T/.magisk/config
# No partition holds modules' SELinux rules here, but the app asks for a reflash without one.
# 0:0 is no device: nothing can be mounted or written through it.
mknod $T/.magisk/device/preinit b 0 0
"""


def _abis():
    abis = ABIS.get(platform.machine())
    if not abis:
        raise CommandError("the Root switch does not support the {} architecture".format(platform.machine()))
    return abis


_apk_lock = threading.Lock()


def ensure_apk():
    """Download the pinned APK once; returns its path."""
    with _apk_lock:  # parallel starts share the cache and its temp file
        return download(APK_URL, APK_SHA256, APK_CACHE, "Magisk for the Root switch")


def remove(inst, upper=True):
    """Take Magisk out of the lower layer and, with upper, out of the instance's own changes."""
    for base in ("overlay", "overlay_rw/system")[:2 if upper else 1]:
        base = os.path.realpath(os.path.join(inst.dir, base))
        for rel in OWNED:
            p = os.path.join(base, rel)
            # Android writes the upper layer: never follow a symlinked parent out of it
            if os.path.realpath(os.path.dirname(p)) != os.path.dirname(p):
                continue
            if os.path.isdir(p) and not os.path.islink(p):
                shutil.rmtree(p)
            elif os.path.lexists(p):
                os.unlink(p)
    shutil.rmtree(os.path.join(inst.dir, "overlay/sbin"), ignore_errors=True)
    if os.path.lexists(os.path.join(inst.dir, STAMP)):
        os.unlink(os.path.join(inst.dir, STAMP))


def install(inst):
    """Put the pinned build in the lower layer; a no-op when it is already there.
    The upper layer is left alone: it holds Magisk's own in-app updates."""
    abi, abi32 = _abis()
    root = os.path.join(inst.dir, "overlay")
    mdir = os.path.join(root, MAGISK.lstrip("/"))
    stamp = os.path.join(inst.dir, STAMP)
    rc_text = BOOTANIM + SETUP.format(m=MAGISK, t=TMP, cg=INIT_CGROUP)
    sh_text = SETUP_SH.format(m=MAGISK, t=TMP)
    # a new init script, setup script or patch reinstalls too
    build = hashlib.sha256((APK_SHA256 + rc_text + sh_text + repr(PATCH)).encode()).hexdigest()
    try:
        with open(stamp) as f:
            if f.read() == build and os.path.isdir(mdir):
                return
    except OSError:
        pass
    apk = ensure_apk()  # before anything is torn down: a failed download changes nothing
    remove(inst, upper=False)
    os.makedirs(mdir)
    with zipfile.ZipFile(apk) as z:
        for name in z.namelist():
            base = os.path.basename(name)
            if name.startswith("lib/{}/lib".format(abi)) and base.endswith(".so"):
                _put(z, name, os.path.join(mdir, base[3:-3]), 0o755)
            elif name == "lib/{}/libmagisk.so".format(abi32):
                _put(z, name, os.path.join(mdir, "magisk32"), 0o755)
            elif name.startswith("assets/") and name.count("/") == 1 and base.endswith(".sh"):
                _put(z, name, os.path.join(mdir, base), 0o644)
    for patch in PATCH[abi]:
        _patch(os.path.join(mdir, "magisk"), *patch)
    with open(os.path.join(mdir, "setup.sh"), "w") as f:
        f.write(sh_text)
    # magiskd installs stub.apk when no Magisk app is installed, and trusts apps signed like it.
    # The full app instead of the real stub: no download on first open.
    shutil.copyfile(apk, os.path.join(mdir, "stub.apk"))
    # Magisk's own updater rewrites bootanim.rc, so it keeps a pristine copy next to it
    rc = os.path.join(root, "system/etc/init/bootanim.rc")
    with gzip.open(rc + ".gz", "wb") as f:
        f.write(BOOTANIM.encode())
    with open(rc, "w") as f:
        f.write(rc_text)
    for d, _, files in os.walk(mdir):
        os.chown(d, 0, 2000)
        os.chmod(d, 0o755)
        for n in files:
            os.chown(os.path.join(d, n), 0, 2000)
    with open(stamp, "w") as f:
        f.write(build)


def _patch(path, offset, was, becomes):
    with open(path, "r+b") as f:
        f.seek(offset)
        if f.read(len(was) // 2) != bytes.fromhex(was):
            raise CommandError("unexpected Magisk build: {} doesn't match its patch".format(path))
        f.seek(offset)
        f.write(bytes.fromhex(becomes))


def _put(z, name, dst, mode):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with z.open(name) as src, open(dst, "wb") as out:
        shutil.copyfileobj(src, out)
    os.chmod(dst, mode)
