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
    root = os.path.realpath(data_dir)
    for rel in SSAID_FILES:
        p = os.path.join(data_dir, rel)
        # never follow a symlinked parent out of the data tree
        if os.path.realpath(os.path.dirname(p)).startswith(root + os.sep) and os.path.lexists(p):
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
    """Stream the APK into 'pm install -S' over stdin.

    Nothing is staged in the instance's data dir: its top level belongs to the
    user, so a root write there could be redirected through a planted symlink.
    """
    import subprocess
    import threading
    from .util import android_attach_env
    # lxc-attach chowns/chmods its stdio, so it must never get the caller's
    # file descriptor directly: feed the APK through a pipe instead.
    src = os.fdopen(fd, "rb", closefd=True)
    try:
        size = os.fstat(src.fileno()).st_size
        env = android_attach_env(inst.id)
        cmd = ["lxc-attach", "-P", paths.LXC_PATH, "-n", inst.container, "--clear-env"]
        for k, v in env.items():
            cmd += ["--set-var", "{}={}".format(k, v)]
        cmd += ["--", "/system/bin/pm", "install", "-r", "-S", str(size)]
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        chunks = {"out": b"", "err": b""}

        def drain(stream, key):
            chunks[key] = stream.read()
        readers = [threading.Thread(target=drain, args=(p.stdout, "out"), daemon=True),
                   threading.Thread(target=drain, args=(p.stderr, "err"), daemon=True)]
        for t in readers:
            t.start()
        try:
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
        r = subprocess.CompletedProcess(cmd, p.returncode, chunks["out"], chunks["err"])
    finally:
        src.close()
    out = (r.stdout + r.stderr).decode("utf-8", "replace").strip()
    log.info("%s: install %s: %s", inst.id, filename, out)
    if r.returncode != 0 or "Success" not in out:
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
