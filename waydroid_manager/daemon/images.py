# SPDX-License-Identifier: GPL-3.0-or-later
"""Daemon-owned image store.

``waydroid upgrade`` rewrites the stock system.img/vendor.img *in place*
(zip extraction truncates the same inode), which would corrupt any instance
that has those files loop-mounted. Instances therefore run from copies in
/var/lib/waydroid-manager/images/<system_datetime>-<vendor_datetime>/.

The daemon keeps the store in sync automatically: whenever the stock image
ids change (``waydroid upgrade`` / ``waydroid init -f``) and the stock files
have settled, the new images are copied. Running instances keep their old
set; each instance switches at its next start, and unused sets are removed.
"""
import os
import shutil
import threading
import time

from .. import paths, stock
from .util import log, run

CURRENT = os.path.join(paths.IMAGES_DIR, "current")
_lock = threading.RLock()


def stock_image_id(cfg=None):
    cfg = cfg or stock.load_stock_cfg()
    w = cfg["waydroid"]
    return "{}-{}".format(w.get("system_datetime", "0"), w.get("vendor_datetime", "0"))


def current_id():
    try:
        return os.path.basename(os.readlink(CURRENT))
    except OSError:
        return ""


def image_dir(image_id):
    return os.path.join(paths.IMAGES_DIR, image_id)


def available():
    try:
        return sorted(d for d in os.listdir(paths.IMAGES_DIR)
                      if d != "current" and not d.endswith(".tmp")
                      and os.path.isfile(os.path.join(paths.IMAGES_DIR, d, "system.img")))
    except FileNotFoundError:
        return []


def sync():
    """Copy the stock images into the store (if not there yet) and make them
    the current set. Returns the image id."""
    with _lock:
        return _sync()


def _sync():
    cfg = stock.load_stock_cfg()
    iid = stock_image_id(cfg)
    src = stock.stock_images_path(cfg)
    for f in ("system.img", "vendor.img"):
        if not os.path.isfile(os.path.join(src, f)):
            raise RuntimeError("stock image {} not found; run 'waydroid init' first".format(os.path.join(src, f)))
    dst = image_dir(iid)
    if not os.path.isdir(dst):
        tmp = dst + ".tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp)
        for f in ("system.img", "vendor.img"):
            log.info("copying %s into the image store", f)
            run(["cp", "--sparse=always", os.path.join(src, f), os.path.join(tmp, f)], timeout=1800)
            os.chmod(os.path.join(tmp, f), 0o644)
        os.rename(tmp, dst)
    if current_id() != iid:
        tmp_link = CURRENT + ".tmp"
        if os.path.lexists(tmp_link):
            os.unlink(tmp_link)
        os.symlink(iid, tmp_link)
        os.replace(tmp_link, CURRENT)
    return iid


def stock_is_newer():
    cur = current_id()
    return cur != stock_image_id()


def stock_busy():
    """True while stock Waydroid may be rewriting its images."""
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open("/proc/{}/cmdline".format(pid), "rb") as f:
                argv = f.read().split(b"\0")
        except OSError:
            continue
        if any(a.endswith(b"waydroid") or a.endswith(b"waydroid.py") for a in argv[:2]) and \
                any(a in (b"upgrade", b"init") for a in argv):
            return True
    now = time.time()
    cfg = stock.load_stock_cfg()
    src = stock.stock_images_path(cfg)
    for p in (paths.STOCK_CFG, os.path.join(src, "system.img"), os.path.join(src, "vendor.img")):
        try:
            if now - os.stat(p).st_mtime < 30:
                return True
        except OSError:
            return True
    return False


def ensure_synced(in_use=()):
    """Sync if the stock images changed and are stable. Returns the current id.

    Never raises for a busy stock install: the previous set stays current.
    """
    with _lock:
        if current_id() and not stock_is_newer():
            return current_id()
        if current_id() and stock_busy():
            log.info("stock images are changing; keeping image set %s for now", current_id())
            return current_id()
        iid = sync()
        gc(in_use)
        return iid


def gc(in_use):
    """Remove image sets no instance uses (never the current one)."""
    keep = set(in_use) | {current_id()}
    for d in available():
        if d not in keep:
            log.info("removing unused image set %s", d)
            shutil.rmtree(image_dir(d), ignore_errors=True)
