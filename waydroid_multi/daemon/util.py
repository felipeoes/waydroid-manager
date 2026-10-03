# SPDX-License-Identifier: GPL-3.0-or-later
"""Small root-side helpers: running commands, mounts, attaching to containers."""
import ctypes
import ctypes.util
import logging
import os
import stat
import subprocess

from .. import paths, stock

log = logging.getLogger("waydroid-multi")

MS_BIND = 4096
MNT_DETACH = 2
_libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)


class CommandError(RuntimeError):
    pass


def run(cmd, check=True, env=None, input=None, timeout=300):
    """Run a command, capture output, log it. Returns CompletedProcess."""
    log.debug("run: %s", " ".join(cmd))
    full_env = None
    if env:
        full_env = dict(os.environ)
        full_env.update(env)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, env=full_env, input=input,
                           timeout=timeout, stdin=None if input is not None else subprocess.DEVNULL)
    except subprocess.TimeoutExpired as e:
        raise CommandError("{} timed out".format(cmd[0])) from e
    if r.stdout.strip():
        log.debug("  out: %s", r.stdout.strip()[-2000:])
    if r.stderr.strip():
        log.debug("  err: %s", r.stderr.strip()[-2000:])
    if check and r.returncode != 0:
        raise CommandError("{} failed ({}): {}".format(" ".join(cmd[:3]), r.returncode,
                                                     (r.stderr or r.stdout).strip()[-500:]))
    return r


def bind_mount(src, dst):
    if _libc.mount(src.encode(), dst.encode(), None, MS_BIND, None) != 0:
        e = ctypes.get_errno()
        raise OSError(e, "bind mount {} -> {} failed: {}".format(src, dst, os.strerror(e)))


def umount(path, lazy=False):
    if _libc.umount2(path.encode(), MNT_DETACH if lazy else 0) != 0:
        e = ctypes.get_errno()
        raise OSError(e, "umount {} failed: {}".format(path, os.strerror(e)))


def mounts_under(path):
    """Mount points equal to or below path (deepest first)."""
    path = os.path.realpath(path)
    found = []
    with open("/proc/self/mountinfo") as f:
        for line in f:
            mp = line.split()[4].replace("\\040", " ")
            if mp == path or mp.startswith(path + "/"):
                found.append(mp)
    return sorted(set(found), key=lambda p: p.count("/"), reverse=True)


def is_mount(path):
    return os.path.realpath(path) in mounts_under(path)


def umount_tree(path):
    """Unmount everything at or below path (trailing-slash safe, unlike stock)."""
    for _ in range(3):
        mps = mounts_under(path)
        if not mps:
            return
        for mp in mps:
            try:
                umount(mp)
            except OSError as e:
                log.debug("umount %s: %s", mp, e)
    for mp in mounts_under(path):
        log.warning("lazy-unmounting busy mount %s", mp)
        try:
            umount(mp, lazy=True)
        except OSError:
            pass


def stage_socket(src_path, dst_path, uid):
    """Validate a user's socket and bind it at a root-owned path.

    The socket must be a real (non-symlink) unix socket owned by uid that
    lives under /run/user/<uid>. It is opened with O_PATH|O_NOFOLLOW and bound
    through /proc/self/fd so the checked inode is exactly the one mounted.
    """
    runtime = "/run/user/{}/".format(uid)
    norm = os.path.normpath(src_path)
    if not norm.startswith(runtime):
        raise PermissionError("socket {} is not under {}".format(src_path, runtime))
    fd = os.open(norm, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        st = os.fstat(fd)
        if not stat.S_ISSOCK(st.st_mode):
            raise PermissionError("{} is not a socket".format(src_path))
        if st.st_uid != uid:
            raise PermissionError("{} is not owned by uid {}".format(src_path, uid))
        os.makedirs(os.path.dirname(dst_path), mode=0o755, exist_ok=True)
        if is_mount(dst_path):
            umount_tree(dst_path)
        if not os.path.exists(dst_path):
            with open(dst_path, "w"):
                pass
        bind_mount("/proc/self/fd/{}".format(fd), dst_path)
    finally:
        os.close(fd)


def chown_tree_top(path, uid, gid, mode):
    os.makedirs(path, exist_ok=True)
    os.chown(path, uid, gid)
    os.chmod(path, mode)


# -- LXC ---------------------------------------------------------------------

def lxc_state(iid):
    r = run(["lxc-info", "-P", paths.LXC_PATH, "-n", paths.container_name(iid), "-sH"], check=False)
    s = r.stdout.strip()
    return s if s in ("RUNNING", "FROZEN", "STOPPED", "STARTING", "STOPPING", "FREEZING", "ABORTING") else "STOPPED"


_classpath_cache = {}


def android_attach_env(iid):
    env = stock.android_env()
    r = run(["lxc-attach", "-P", paths.LXC_PATH, "-n", paths.container_name(iid), "--clear-env",
             "--", "/system/bin/cat", "/data/system/environ/classpath"], check=False)
    for line in r.stdout.splitlines():
        parts = line.split(" ", 2)
        if len(parts) == 3 and any(p in parts[1] for p in ("CLASSPATH", "SYSTEMSERVER")):
            env[parts[1]] = parts[2]
    return env


def attach(iid, argv, check=True, timeout=300, env=None):
    """Run argv inside the container as root with the Android environment."""
    env = env or android_attach_env(iid)
    cmd = ["lxc-attach", "-P", paths.LXC_PATH, "-n", paths.container_name(iid), "--clear-env"]
    for k, v in env.items():
        cmd += ["--set-var", "{}={}".format(k, v)]
    cmd += ["--"] + list(argv)
    # Output goes through pipes: lxc-attach chmods its stdout otherwise
    return run(cmd, check=check, timeout=timeout)
