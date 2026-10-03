# SPDX-License-Identifier: GPL-3.0-or-later
"""Daemon-owned image store.

``waydroid upgrade`` rewrites the stock system.img/vendor.img *in place*
(zip extraction truncates the same inode), which would corrupt any instance
that has those files loop-mounted. Instances therefore run from copies in
/var/lib/waydroid-multi/images/<system_datetime>-<vendor_datetime>/.
"""
import os
import shutil

from .. import paths, stock
from .util import log, run

CURRENT = os.path.join(paths.IMAGES_DIR, "current")


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
    """Copy the stock images into the store if they changed. Returns the image id."""
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
    return bool(cur) and cur != stock_image_id()


def gc(in_use):
    """Remove image sets no instance uses (never the current one)."""
    keep = set(in_use) | {current_id()}
    for d in available():
        if d not in keep:
            log.info("removing unused image set %s", d)
            shutil.rmtree(image_dir(d), ignore_errors=True)
