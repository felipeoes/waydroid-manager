# SPDX-License-Identifier: GPL-3.0-or-later
"""Instance data operations (root): clone, identity reset, delete, APK install."""
import os
import secrets
import shutil

from .. import paths
from .util import attach, log, run, umount_tree

SSAID_FILES = ("system/users/0/settings_ssaid.xml", "system/users/0/settings_ssaid.xml.fallback")
GMS_PACKAGES = ("com.google.android.gsf", "com.google.android.gms")


def copy_data(src, dst):
    """Copy an Android /data tree preserving ownership, modes and xattrs."""
    if os.path.exists(dst):
        shutil.rmtree(dst)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    run(["cp", "-a", "--reflink=auto", src, dst], timeout=3600)


def reset_ids_offline(data_dir):
    """Before first boot: drop the SSAID store so Android regenerates the
    per-app Android ID key. (Settings files are binary XML; never edit them.)"""
    for rel in SSAID_FILES:
        p = os.path.join(data_dir, rel)
        if os.path.lexists(p):
            os.unlink(p)


def reset_ids_online(iid):
    """After boot: new legacy android_id and fresh Google Services identity."""
    out = []
    new_id = secrets.token_hex(8)
    attach(iid, ["/system/bin/settings", "put", "secure", "android_id", new_id], check=False)
    out.append("android_id={}".format(new_id))
    pkgs = attach(iid, ["/system/bin/pm", "list", "packages"], check=False).stdout
    for pkg in GMS_PACKAGES:
        if "package:" + pkg + "\n" in pkgs + "\n":
            r = attach(iid, ["/system/bin/pm", "clear", pkg], check=False)
            out.append("cleared {}: {}".format(pkg, r.stdout.strip() or r.stderr.strip()))
    return out


def gsf_id(iid):
    """The Google Services Framework ID (decimal) used for device registration, or ''."""
    r = attach(iid, ["/system/bin/sh", "-c",
                     "content query --uri content://com.google.android.gsf.gservices "
                     "--where \"name='android_id'\""], check=False)
    for part in r.stdout.replace(",", " ").split():
        if part.startswith("value="):
            v = part[len("value="):]
            if v.isdigit():
                return v
    return ""


def delete_instance_files(inst):
    umount_tree(inst.dir)
    umount_tree(paths.staging_dir(inst.id))
    for p in (inst.lxc_dir, inst.dir, paths.staging_dir(inst.id)):
        if os.path.isdir(p):
            # cp/rm stay on this filesystem: never follow into a stray mount
            run(["rm", "-rf", "--one-file-system", p], timeout=3600)


def install_apk(inst, fd, filename):
    tmpdir = os.path.join(inst.data_dir, "waydroid_tmp")
    os.makedirs(tmpdir, exist_ok=True)
    os.chmod(tmpdir, 0o755)
    target = os.path.join(tmpdir, "wdm-install.apk")
    with os.fdopen(fd, "rb", closefd=True) as src, open(target, "wb") as dst:
        shutil.copyfileobj(src, dst, 1024 * 1024)
    os.chmod(target, 0o644)
    try:
        r = attach(inst.id, ["/system/bin/pm", "install", "-r", "/data/waydroid_tmp/wdm-install.apk"],
                   check=False, timeout=600)
        out = (r.stdout + r.stderr).strip()
        log.info("%s: installed %s: %s", inst.id, filename, out)
        if r.returncode != 0 or "Success" not in out:
            raise RuntimeError("install failed: " + out[-500:])
        return out
    finally:
        try:
            os.unlink(target)
        except OSError:
            pass
