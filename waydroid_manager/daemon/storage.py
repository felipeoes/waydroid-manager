# SPDX-License-Identifier: GPL-3.0-or-later
"""Instance data operations (root): clone, identity reset, delete, APK install."""
import os
import pwd
import secrets
import shutil
import sqlite3
import stat
import tempfile

from .. import paths
from .util import attach, log, open_beneath, run, umount_tree

SSAID_FILES = ("system/users/0/settings_ssaid.xml", "system/users/0/settings_ssaid.xml.fallback")
GMS_PACKAGES = ("com.google.android.gsf", "com.google.android.gms")


STOCK_DATA = ".local/share/waydroid/data"
AID_SYSTEM = 1000


def stock_data_path(uid):
    return os.path.join(pwd.getpwuid(uid).pw_dir, STOCK_DATA)


def open_stock_data(uid):
    """O_PATH fd of the user's stock Waydroid data dir (raises OSError if missing).

    The user controls everything below their home: open each step without
    following symlinks and use the fd, so nothing can be swapped in between."""
    fd = os.open(os.path.realpath(pwd.getpwuid(uid).pw_dir), os.O_PATH | os.O_DIRECTORY)
    try:
        for part in STOCK_DATA.split("/"):
            nfd = os.open(part, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nfd
        # Android's init chowns /data, this directory, to its system uid at every boot
        if os.fstat(fd).st_uid not in (uid, AID_SYSTEM):
            raise OSError("{} is not owned by uid {}".format(stock_data_path(uid), uid))
    except OSError:
        os.close(fd)
        raise
    return fd


def copy_data(src, dst):
    """Copy an Android /data tree preserving ownership, modes and xattrs.
    src may be a /proc/<pid>/fd/<n> path: its contents are copied into dst."""
    if os.path.lexists(dst):
        shutil.rmtree(dst)
    os.makedirs(dst)
    run(["cp", "-a", "--reflink=auto", src + "/.", dst + "/"], timeout=3600)
    if not stat.S_ISDIR(os.lstat(dst).st_mode):
        raise RuntimeError("copy produced no data directory")


def reset_ids_offline(data_dir):
    """Before first boot: drop the SSAID store so Android regenerates the
    per-app Android ID key. (Settings files are binary XML; never edit them.)"""
    root = os.path.realpath(data_dir)
    for rel in SSAID_FILES:
        p = os.path.join(data_dir, rel)
        # never follow a symlinked parent out of the data tree
        if os.path.realpath(os.path.dirname(p)).startswith(root + os.sep) and os.path.lexists(p):
            os.unlink(p)


SHADER_CACHES = ("com.android.skia.shaders_cache", "com.android.opengl.shaders_cache")


def clear_shader_caches(data_dir):
    """Delete the apps' GPU shader caches (hwui's and EGL's, in each app's code_cache): they
    hold what the renderer of the last start built, and Android 13's hwui divides by zero
    cleaning one another renderer filled (the launcher crashed in a loop after a move from
    software to NVIDIA). Android's tree: no symlink is followed."""
    def subdirs(fd, names=None):
        for name in names or os.listdir(fd):
            try:
                sub = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            except OSError:
                continue                    # missing, a file or a symlink
            try:
                yield sub
            finally:
                os.close(sub)

    def clear_apps(fd):
        for app in subdirs(fd):
            for cache in subdirs(app, ["code_cache"]):
                for name in SHADER_CACHES:
                    try:
                        os.unlink(name, dir_fd=cache)
                    except OSError:
                        pass

    root = os.open(data_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for apps in subdirs(root, ["data"]):             # data/<app>: user 0
            clear_apps(apps)
        for top in subdirs(root, ["user", "user_de"]):   # user[_de]/<user>/<app> (user/0 links to data)
            for user in subdirs(top):
                clear_apps(user)
    finally:
        os.close(root)


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


GSF_DB = "data/com.google.android.gsf/databases/gservices.db"
GSF_DB_MAX = 64 * 1024 ** 2


def gsf_id(inst):
    """The Google Services Framework ID (decimal) used for device registration, or ''.
    Read from GSF's database: Android's own provider query answers nothing on 14 and newer.
    The database and its write-ahead log (GMS keeps recent rows there) are read from a private
    copy: Android's files are only ever read, and a FIFO or a huge file planted there is refused."""
    with tempfile.TemporaryDirectory() as tmp:
        for suffix in ("", "-wal"):
            try:
                fd = open_beneath(inst.data_dir, GSF_DB + suffix, os.O_RDONLY | os.O_NONBLOCK)
            except OSError:
                if suffix:
                    continue        # no log: everything is in the database
                return ""
            with os.fdopen(fd, "rb") as src:
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode) or st.st_size > GSF_DB_MAX:
                    return ""
                with open(os.path.join(tmp, "gservices.db" + suffix), "wb") as out:
                    shutil.copyfileobj(src, out)
        try:
            db = sqlite3.connect(os.path.join(tmp, "gservices.db"))
            try:
                row = db.execute("SELECT value FROM main WHERE name = 'android_id'").fetchone()
            finally:
                db.close()
        except sqlite3.Error:
            return ""
    return row[0] if row and str(row[0]).isdigit() else ""


def delete_instance_files(inst):
    umount_tree(inst.dir)
    umount_tree(paths.staging_dir(inst.id))
    for p in (inst.lxc_dir, inst.dir, paths.staging_dir(inst.id)):
        if os.path.isdir(p):
            # cp/rm stay on this filesystem: never follow into a stray mount
            run(["rm", "-rf", "--one-file-system", p], timeout=3600)


def install_apk(inst, fd, filename):
    """Stream an APK into 'pm install -S' over stdin, or the APKs of an XAPK into one install session.

    Nothing is staged in the instance's data dir: its top level belongs to the
    user, so a root write there could be redirected through a planted symlink.
    """
    src = os.fdopen(fd, "rb", closefd=True)
    try:
        if filename.lower().endswith(".xapk"):
            out = _install_xapk(inst, src)
        else:
            out = _pm(inst, ["install", "-r", "-S", str(os.fstat(src.fileno()).st_size)], src)
    finally:
        src.close()
    log.info("%s: install %r: %s", inst.id, filename, out)
    return out


def _install_xapk(inst, src):
    """An XAPK is a zip of the app's APKs (base and splits): each is streamed from it into
    one pm install session. OBB files some XAPKs carry are not copied."""
    import zipfile
    try:
        z = zipfile.ZipFile(src)
    except zipfile.BadZipFile:
        raise RuntimeError("install failed: not an XAPK file")
    with z:
        apks = [i for i in z.infolist() if "/" not in i.filename and i.filename.lower().endswith(".apk")]
        if not apks:
            raise RuntimeError("install failed: no APKs in this XAPK")
        out = _pm(inst, ["install-create", "-r", "-S", str(sum(i.file_size for i in apks))])
        session = out[out.find("[") + 1:out.find("]")]
        if not session.isdigit():
            raise RuntimeError("install failed: " + out[-500:])
        try:
            for n, i in enumerate(apks):
                with z.open(i) as member:  # names are the zip's: never handed to pm
                    _pm(inst, ["install-write", "-S", str(i.file_size), session, "{}.apk".format(n), "-"],
                        member)
            return _pm(inst, ["install-commit", session])
        except BaseException:
            _pm(inst, ["install-abandon", session], check=False)
            raise


def _pm(inst, args, src=None, check=True):
    """Run 'pm ARGS' in the instance, src (a file object) on its stdin; returns its output."""
    import subprocess
    import threading
    from .util import android_attach_env
    # lxc-attach chowns/chmods its stdio, so it must never get the caller's
    # file descriptor directly: feed the APK through a pipe instead.
    env = android_attach_env(inst.id)
    cmd = ["lxc-attach", "-P", paths.LXC_PATH, "-n", inst.container, "--clear-env"]
    for k, v in env.items():
        cmd += ["--set-var", "{}={}".format(k, v)]
    cmd += ["--", "/system/bin/pm"] + args
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    chunks = {"out": b"", "err": b""}

    def drain(stream, key):
        chunks[key] = stream.read()
    readers = [threading.Thread(target=drain, args=(p.stdout, "out"), daemon=True),
               threading.Thread(target=drain, args=(p.stderr, "err"), daemon=True)]
    for t in readers:
        t.start()
    try:
        if src is not None:
            shutil.copyfileobj(src, p.stdin, 1024 * 1024)
    except (BrokenPipeError, OSError):
        pass  # pm exited early; its output says why
    finally:
        try:
            p.stdin.close()
        except OSError:
            pass
    try:
        p.wait(timeout=900)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait()
    for t in readers:
        t.join(5)
    out = (chunks["out"] + chunks["err"]).decode("utf-8", "replace").strip()
    if check and (p.returncode != 0 or "Success" not in out):
        raise RuntimeError("install failed: " + out[-500:])
    return out


def screenshot(inst, fd):
    """Write a PNG of the Android screen to fd (never handed to lxc-attach itself)."""
    import subprocess
    out = os.fdopen(fd, "wb", closefd=True)
    try:
        cmd = ["lxc-attach", "-P", paths.LXC_PATH, "-n", inst.container, "--clear-env",
               "--set-var", "PATH=/system/bin:/system/xbin:/vendor/bin", "--",
               "/system/bin/screencap", "-p"]
        p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        size = 0
        for chunk in iter(lambda: p.stdout.read(1024 * 1024), b""):
            out.write(chunk)
            size += len(chunk)
        p.wait(timeout=30)
        if p.returncode != 0 or size == 0:
            raise RuntimeError("screencap failed")
        return size
    finally:
        out.close()
