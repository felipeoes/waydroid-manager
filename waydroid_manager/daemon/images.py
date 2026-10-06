# SPDX-License-Identifier: GPL-3.0-or-later
"""Daemon-owned image store: /var/lib/waydroid-manager/images/<id>/ holds system.img,
vendor.img, image.cfg (which Android it is) and, for some versions, gapps/ (catalog.py).

Device #0 runs stock Waydroid's own images. ``waydroid upgrade`` rewrites them *in place*
(zip extraction truncates the same inode), which would corrupt a mounted copy, so #0 runs
from a copy, synced whenever stock's images change and have settled (the `current` set).

Every other device runs the Android version it was created with: the newest installed set of
that version. ``install`` downloads a version, ``update`` fetches newer builds; a device
switches at its next start, and sets no device runs or switches to are removed (gc). Set ids are
<system datetime>-<vendor datetime> for OTA builds (like stock's sets, so stock's copy is
reused when it is that very build) and the zip's hash for zip builds.
"""
import configparser
import json
import os
import shutil
import threading
import time
import urllib.request
import zipfile

from .. import catalog, paths, stock
from . import gapps
from .util import download, log, mount_image, run, umount_tree

CURRENT = os.path.join(paths.IMAGES_DIR, "current")
lock = threading.RLock()            # the store's sets and `current`; held while a device picks its set
_install_lock = threading.Lock()    # downloads (minutes): never under `lock`, starts must not wait


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
                      if d != "current" and "." not in d
                      and os.path.isfile(os.path.join(paths.IMAGES_DIR, d, "system.img")))
    except FileNotFoundError:
        return []


def read_cfg(image_id):
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read(os.path.join(image_dir(image_id), "image.cfg"))
    return dict(cfg["image"]) if "image" in cfg else {}


def _write_cfg(d, **fields):
    cfg = configparser.ConfigParser(interpolation=None)
    cfg["image"] = {k: str(v) for k, v in fields.items()}
    with open(os.path.join(d, "image.cfg.tmp"), "w") as f:
        cfg.write(f)
    os.replace(os.path.join(d, "image.cfg.tmp"), os.path.join(d, "image.cfg"))


def probe(d, gms_layer=None):
    """API levels of a set's system and vendor image, read from the images mounted read-only
    (ext4, squashfs and EROFS all occur). With gms_layer, also unpacks the system image's GMS
    APEX there."""
    mnt = d + ".probe"
    try:
        mount_image(os.path.join(d, "system.img"), mnt + "/s")
        mount_image(os.path.join(d, "vendor.img"), mnt + "/v")
        sys_sdk = stock.read_prop_file(mnt + "/s/system/build.prop", "ro.build.version.sdk")
        ven_sdk = stock.read_prop_file(mnt + "/v/build.prop", "ro.vendor.build.version.sdk")
        if gms_layer:
            gapps.flatten_gms_apex(mnt + "/s", gms_layer)
    finally:
        umount_tree(mnt)
        shutil.rmtree(mnt, ignore_errors=True)
    return (int(sys_sdk) if sys_sdk.isdigit() else 0), (int(ven_sdk) if ven_sdk.isdigit() else 0)


# -- #0: stock Waydroid's images ---------------------------------------------------------

def sync():
    """Copy the stock images into the store (if not there yet) and make them
    the current set. Returns the image id."""
    with lock:
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
        sdk, _ = probe(tmp)
        # channel: an official GAPPS set is reused for devices of its version (install)
        _write_cfg(tmp, sdk=sdk, built=cfg["waydroid"].get("system_datetime", "0"), stock="true",
                   channel=cfg["waydroid"].get("system_ota", ""))
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


def ensure_synced(in_use):
    """Sync if the stock images changed and are stable (in_use: see gc). Returns the current id.

    Never raises for a busy stock install: the previous set stays current.
    """
    with lock:
        if current_id() and not stock_is_newer():
            return current_id()
        if current_id() and stock_busy():
            log.info("stock images are changing; keeping image set %s for now", current_id())
            return current_id()
        iid = sync()
        gc(in_use)
        return iid


def gc(in_use):
    """Remove the sets no device needs, never the current one. in_use(): (set id, Android
    version, "" for #0) of every device, read under the lock so no pick is missed. A device
    needs its set and the newest of its version, which it switches to at its next start."""
    with lock:
        devices = in_use()
        keep = {sid for sid, _ in devices} | {latest(k) for _, k in devices if k} | {current_id()}
        for d in available():
            if d not in keep:
                log.info("removing unused image set %s", d)
                shutil.rmtree(image_dir(d), ignore_errors=True)


# -- Android versions (catalog) -----------------------------------------------------------

def latest(key):
    """Newest installed set of Android key, or ''."""
    sets = [(read_cfg(d).get("built", ""), d) for d in available() if read_cfg(d).get("android") == key]
    return max(sets)[1] if sets else ""


def _newest(channel, version):
    """Newest entry of an OTA channel for a version: datetime, then the file name's date
    (a channel may list two builds with the same datetime)."""
    with urllib.request.urlopen(channel, timeout=60) as r:
        entries = [e for e in json.load(r)["response"] if e.get("version") == version]
    if not entries:
        raise RuntimeError("{} lists no {} build".format(channel, version))
    return max(entries, key=lambda e: (e["datetime"], e["filename"]))


def _build(key):
    """(set id, built, [(url, sha256)]) of the newest build of Android key."""
    v = catalog.VERSIONS[key]
    if "zip" in v:
        url, sha, built = v["zip"]
        return sha[:16], built, [(url, sha)]
    system, vendor = _newest(v["ota"][0], v["ota"][2]), _newest(v["ota"][1], v["ota"][2])
    return ("{}-{}".format(system["datetime"], vendor["datetime"]), str(system["datetime"]),
            [(system["url"], system["id"]), (vendor["url"], vendor["id"])])


def install(key):
    """The newest build of Android key, downloaded, checked and unpacked if it isn't here yet
    (with its Google Play layer). Returns its set id."""
    v = catalog.VERSIONS[key]
    channel = v["ota"][0] if "ota" in v else v["zip"][0]
    with _install_lock:
        sid, built, sources = _build(key)
        d = image_dir(sid)
        cfg = read_cfg(sid)
        if cfg.get("android") == key:
            return sid
        if os.path.isfile(os.path.join(d, "system.img")) and cfg.get("channel") != channel:
            raise RuntimeError("image set {} exists but isn't Android {}".format(sid, key))
        # a new set is checked and described in d.tmp: d only ever appears with its image.cfg
        work = d if os.path.isfile(os.path.join(d, "system.img")) else d + ".tmp"
        shutil.rmtree(os.path.join(d, "gapps"), ignore_errors=True)    # a failed earlier attempt's
        if work != d:
            _fetch(work, sources, v, key)
        sys_sdk, ven_sdk = probe(work, os.path.join(work, "gapps") if v.get("gapps") == "gms_apex" else None)
        if sys_sdk != v["sdk"] or ven_sdk != v["sdk"]:
            shutil.rmtree(work, ignore_errors=True)
            raise RuntimeError("the Android {} download is API {}/{}, not {}".format(key, sys_sdk, ven_sdk, v["sdk"]))
        if v.get("gapps") == "mtg14":
            gapps.mtg_layer()
        _write_cfg(work, android=key, sdk=v["sdk"], built=built, channel=channel, stock=cfg.get("stock", "false"))
        if work != d:
            os.rename(work, d)
        log.info("Android %s installed as image set %s", key, sid)
        return sid


def _fetch(tmp, sources, v, key):
    """Download a set's zip(s) and extract system.img and vendor.img into tmp."""
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    need = int(v["gb"] * 3.5 * 1024 ** 3)   # the zips plus the images they unpack to
    free = shutil.disk_usage(paths.IMAGES_DIR).free
    if free < need:
        raise RuntimeError("Android {} needs about {:.0f} GB free in {}, {:.0f} GB are".format(
            key, need / 1024 ** 3, paths.IMAGES_DIR, free / 1024 ** 3))
    for n, (url, sha) in enumerate(sources):
        zpath = download(url, sha, os.path.join(paths.IMAGES_DIR, "android{}-{}.zip".format(key, n)),
                         "Android {} ({} of {})".format(key, n + 1, len(sources)))
        with zipfile.ZipFile(zpath) as z:
            for name in z.namelist():
                if os.path.basename(name) in ("system.img", "vendor.img"):
                    with z.open(name) as src, open(os.path.join(tmp, os.path.basename(name)), "wb") as out:
                        shutil.copyfileobj(src, out, 1 << 20)
        os.unlink(zpath)
    for f in ("system.img", "vendor.img"):
        if not os.path.isfile(os.path.join(tmp, f)):
            raise RuntimeError("the Android {} download has no {}".format(key, f))
        os.chmod(os.path.join(tmp, f), 0o644)


def update(in_use):
    """Fetch newer builds of the versions the devices run (in_use: see gc) and sync stock's;
    returns the versions updated."""
    keys = sorted({k for _, k in in_use() if k})
    done = [k for k in keys if latest(k) != install(k)]
    with lock:
        if stock_is_newer() and not stock_busy():
            sync()
        gc(in_use)
    return done


def gapps_layer(image_id):
    """The Google Play layer a set runs with, or None (in the image itself, or stock's)."""
    kind = catalog.get(read_cfg(image_id).get("android"), "gapps", "image")
    if kind == "mtg14":
        return gapps.mtg_layer()
    if kind == "gms_apex":
        return os.path.join(image_dir(image_id), "gapps")
    return None
