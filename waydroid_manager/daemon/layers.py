# SPDX-License-Identifier: GPL-3.0-or-later
"""Read-only overlay layers built once from pinned downloads (ARM translation, GApps, GPU).

A layer holds system/... and/or vendor/... and is stacked below an instance's own layer
(container.mount_rootfs). It is named by its archive's hash and never rewritten, so a mounted
layer stays valid while a newer pin is unpacked next to it.
"""
import os
import shutil
import stat
import threading
import zipfile

from .util import download

ABIS = ("x86_64", "x86")
_lock = threading.Lock()


def layer(d, url, sha256, unpack, what):
    """Directory d, built on first use by unpack(archive, tmp_dir) from the archive at url.
    Raises CommandError (download) or OSError/BadZipFile when it can't be built."""
    # ponytail: one lock for every layer; per-layer locks if parallel first downloads matter
    with _lock:   # parallel starts share layers and their temp files
        if os.path.isdir(d):
            return d
        os.makedirs(os.path.dirname(d), exist_ok=True)
        try:
            shutil.rmtree(d + ".tmp", ignore_errors=True)
            unpack(download(url, sha256, d + ".download", what), d + ".tmp")
            set_modes(d + ".tmp")
            os.rename(d + ".tmp", d)
        finally:
            shutil.rmtree(d + ".tmp", ignore_errors=True)
            if os.path.lexists(d + ".download"):
                os.unlink(d + ".download")
    return d


def extract_apk_libs(root):
    """Put each APK's x86/x86_64 native libraries next to it (<app>/lib/<abi>/): Android can't
    extract them into a read-only system partition at install time."""
    for d, _, files in os.walk(root):
        for f in files:
            if not f.endswith(".apk"):
                continue
            with zipfile.ZipFile(os.path.join(d, f)) as apk:
                for name in apk.namelist():
                    parts = name.split("/")
                    if len(parts) == 3 and parts[0] == "lib" and parts[1] in ABIS and parts[2].endswith(".so"):
                        dst = os.path.join(d, "lib", parts[1], parts[2])
                        os.makedirs(os.path.dirname(dst), exist_ok=True)
                        with apk.open(name) as src, open(dst, "wb") as out:
                            shutil.copyfileobj(src, out)


def whiteout(path):
    """Hide path of the layers below (an overlayfs whiteout)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    os.mknod(path, 0o600 | stat.S_IFCHR, os.makedev(0, 0))


def set_modes(top):
    """root-owned, world-readable; executables and bin/ directories in the shell group, as on
    Android's own partitions. Whiteouts are left alone."""
    for d, dirs, files in os.walk(top):
        gid = 2000 if "bin" in os.path.relpath(d, top).split(os.sep) else 0
        os.chown(d, 0, gid)
        os.chmod(d, 0o755)
        for n in files:
            p = os.path.join(d, n)
            if stat.S_ISREG(os.lstat(p).st_mode):
                os.chown(p, 0, gid)
                os.chmod(p, 0o755 if gid else 0o644)
