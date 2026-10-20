# SPDX-License-Identifier: GPL-3.0-or-later
"""Google Play for images that don't include it in a form the container can use: MindTheGapps
as a shared layer (14, 15), newer Play services for 16's stale factory APK, or the image's
GMS APEX unpacked into a layer (17: a signed APEX needs device-mapper, which we don't expose)."""
import os
import shutil
import threading
import zipfile

from .. import catalog, paths
from . import layers
from .util import download, mount_image, umount_tree

# MindTheGapps' SetupWizard crashes on Waydroid ("WifiService: Permission denied") and, as the
# device is never marked set up, blocks the GSF check-in Play needs
_MTG_SKIP = ("system/system_ext/priv-app/SetupWizard/",)
_gms_lock = threading.Lock()


def gms16_layer():
    """Replace 16's factory GmsCore and its permission list without touching instance data."""
    url, hashes = catalog.GMS16
    d = os.path.join(paths.STATE_DIR, "gapps", "gms16-" + "-".join(sha[:12] for sha in hashes))
    with _gms_lock:
        if os.path.isdir(d):
            return d
        tmp = d + ".tmp"
        parts = []
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            source = os.path.join(tmp, "source")
            apex_dir = os.path.join(source, "system/product/apex")
            os.makedirs(apex_dir)
            with open(os.path.join(apex_dir, "com.google.android.gms.apex"), "wb") as out:
                for n, sha in enumerate(hashes):
                    part = download(url + "{:02}".format(n), sha, d + ".{}.download".format(n), "Play services")
                    parts.append(part)
                    with open(part, "rb") as src:
                        shutil.copyfileobj(src, out)
            layer = flatten_gms_apex(source, os.path.join(tmp, "layer"))
            product = os.path.join(layer, "system/product")
            app = os.path.join(product, "priv-app/PrebuiltGmsCoreVic")
            # Replace the existing path: two factory copies of one package make scanning ambiguous.
            os.rename(os.path.join(app, "PrebuiltGmsCoreVic.apk"), os.path.join(app, "GmsCore.apk"))
            os.rename(app, os.path.join(product, "priv-app/GmsCore"))
            shutil.rmtree(os.path.join(product, "apex"))
            os.rename(layer, d)
            for part in parts:
                os.unlink(part)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        return d


def mtg_layer():
    url, sha = catalog.MTG14
    d = os.path.join(paths.STATE_DIR, "gapps", "mtg14-" + sha[:12])
    return layers.layer(d, url, sha, _unpack_mtg, "MindTheGapps")


def _unpack_mtg(zpath, tmp):
    with zipfile.ZipFile(zpath) as z:
        for name in z.namelist():
            if name.startswith("system/") and not name.endswith("/") and not name.startswith(_MTG_SKIP):
                dst = os.path.join(tmp, name)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                with z.open(name) as src, open(dst, "wb") as out:
                    shutil.copyfileobj(src, out)
    layers.extract_apk_libs(tmp)


def flatten_gms_apex(system_root, d):
    """Unpack the GMS APEX of a mounted system image into the layer d: its apps become ordinary
    privileged apps of /product, and the APEX itself is hidden. Returns d, or None without one."""
    apex_dir = os.path.join(system_root, "system/product/apex")
    names = sorted(n for n in os.listdir(apex_dir)
                   if n.startswith("com.google.android.gms") and n.endswith(".apex")) if os.path.isdir(apex_dir) else []
    if not names:
        return None
    tmp, mnt = d + ".tmp", d + ".mnt"
    product = os.path.join(tmp, "system/product")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    try:
        for name in names:
            payload = os.path.join(tmp, "payload.img")
            with zipfile.ZipFile(os.path.join(apex_dir, name)) as z, z.open("apex_payload.img") as src, \
                    open(payload, "wb") as out:
                shutil.copyfileobj(src, out)
            mount_image(payload, mnt)
            try:
                for sub in ("priv-app", "app"):
                    for app in sorted(os.listdir(os.path.join(mnt, sub))) if os.path.isdir(os.path.join(mnt, sub)) else []:
                        # PrebuiltGmsCoreVic@<version>/ holds PrebuiltGmsCoreVic.apk
                        shutil.copytree(os.path.join(mnt, sub, app), os.path.join(product, sub, app.split("@")[0]))
                for sub in ("etc/permissions", "etc/sysconfig"):
                    if os.path.isdir(os.path.join(mnt, sub)):
                        shutil.copytree(os.path.join(mnt, sub), os.path.join(product, sub), dirs_exist_ok=True)
            finally:
                umount_tree(mnt)
                os.rmdir(mnt)
            os.unlink(payload)
            layers.whiteout(os.path.join(product, "apex", name))
        layers.extract_apk_libs(tmp)
        layers.set_modes(tmp)
        os.rename(tmp, d)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return d
