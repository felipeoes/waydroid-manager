# SPDX-License-Identifier: GPL-3.0-or-later
"""Client side helpers shared by the CLI, the session and the GUI."""
import os
import shutil
import subprocess
import sys
import time

import dbus

from . import paths

LONG_TIMEOUT = 3600


class DaemonError(RuntimeError):
    pass


def _clean(e):
    msg = e.get_dbus_message() or str(e)
    return msg.strip().splitlines()[-1] if msg else str(e)


class Daemon:
    """Thin wrapper over the system-bus API."""

    def __init__(self, bus=None):
        self.bus = bus or dbus.SystemBus()
        try:
            obj = self.bus.get_object(paths.DBUS_NAME, paths.DBUS_PATH)
        except dbus.DBusException as e:
            raise DaemonError("waydroid-multi daemon is not available: {}".format(_clean(e)))
        self.iface = dbus.Interface(obj, paths.DBUS_IFACE)

    def call(self, method, *args, timeout=LONG_TIMEOUT):
        try:
            return getattr(self.iface, method)(*args, timeout=timeout)
        except dbus.DBusException as e:
            raise DaemonError(_clean(e))

    @staticmethod
    def plain(v):
        if isinstance(v, (dbus.Array, list)):
            return [Daemon.plain(x) for x in v]
        if isinstance(v, (dbus.Dictionary, dict)):
            return {str(k): str(x) for k, x in v.items()}
        return str(v)

    def info(self):
        return self.plain(self.call("GetInfo", timeout=30))

    def list(self):
        return self.plain(self.call("List", timeout=60))

    def get(self, iid):
        return self.plain(self.call("Get", iid, timeout=60))

    def create(self, opts):
        """Create (or clone, with opts['clone_from']) an instance; returns its number."""
        return str(self.call("Create", dbus.Dictionary(opts, signature="ss")))

    def resolve(self, ref):
        """Instance number for a reference: a number, 'default'/'0', or a display name."""
        ref = str(ref).strip()
        num = "0" if ref.lower() == "default" else ref.lstrip("#")
        items = self.list()
        if num.isdigit():
            if any(i["id"] == num for i in items):
                return num
            if num == "0":
                return "default"  # the caller's stock Waydroid, while #0 is another user's
            raise DaemonError("no instance #{}".format(num))
        hits = [i for i in items if i["name"].lower() == ref.lower()]
        if len(hits) == 1:
            return hits[0]["id"]
        if not hits:
            raise DaemonError("no instance named '{}'".format(ref))
        raise DaemonError("several instances are named '{}': {}".format(
            ref, ", ".join("#" + i["id"] for i in hits)))

    def delete(self, iid):
        self.call("Delete", iid)

    def set_config(self, iid, values):
        self.call("SetConfig", iid, dbus.Dictionary(values, signature="ss"), timeout=60)

    def start(self, iid, session):
        self.call("Start", iid, dbus.Dictionary(session, signature="ss"), timeout=300)

    def stop(self, iid):
        self.call("Stop", iid, timeout=300)

    def freeze(self, iid):
        self.call("Freeze", iid, timeout=60)

    def unfreeze(self, iid):
        self.call("Unfreeze", iid, timeout=60)

    def report_close(self, iid):
        self.call("ReportClose", iid, timeout=30)

    def install_apk(self, iid, path):
        with open(path, "rb") as f:
            return str(self.call("InstallApk", iid, dbus.types.UnixFd(f), os.path.basename(path), timeout=900))

    def gsf_id(self, iid):
        return str(self.call("GetGsfId", iid, timeout=120))

    def sync_images(self):
        return str(self.call("SyncImages"))


# -- binder access (CLI only: sync gbinder calls block the whole process) -------

def instance_args(iid):
    from . import stock
    from .instance import Instance
    inst = Instance.load(iid)
    return stock.make_args(inst.dir, inst.cfg_path), inst


def platform_service(iid, timeout=60):
    """IPlatform for an instance, or raise. Waits up to timeout for the service."""
    from . import stock
    t = stock.tools()
    import gbinder
    args, inst = instance_args(iid)
    w = inst.cfg["waydroid"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        # A new one each time: without a running GLib main loop libgbinder never notices a
        # service manager that comes up later (and the old one must go first, or its
        # per-device cache hands it back)
        sm = None
        sm = gbinder.ServiceManager("/dev/" + w["binder"], w["service_manager_protocol"], w["binder_protocol"])
        if sm.is_present():
            remote, _ = sm.get_service_sync(t.interfaces.IPlatform.SERVICE_NAME)
            if remote:
                return t.interfaces.IPlatform.IPlatform(remote)
        time.sleep(1)
    raise DaemonError("Android in #{} did not become ready in {}s".format(iid, timeout))


def statusbar_service(iid):
    from . import stock
    t = stock.tools()
    import gbinder
    _, inst = instance_args(iid)
    w = inst.cfg["waydroid"]
    sm = gbinder.ServiceManager("/dev/" + w["binder"], w["service_manager_protocol"], w["binder_protocol"])
    if not sm.is_present():
        return None
    remote, _ = sm.get_service_sync(t.interfaces.IStatusBarService.SERVICE_NAME)
    return t.interfaces.IStatusBarService.IStatusBarService(remote) if remote else None


# -- sessions (systemd --user transient units) ---------------------------------

def session_unit(iid):
    return "waydroid-multi-session-{}.service".format(iid)


def session_active(iid):
    r = subprocess.run(["systemctl", "--user", "is-active", "-q", session_unit(iid)])
    return r.returncode == 0


def start_session(iid, background=False):
    """Start the per-instance session as a transient user unit."""
    cmd = [sys.executable, "-m", "waydroid_multi", "session", iid]
    if background:
        cmd.append("--background")
    env = {"PYTHONPATH": os.path.dirname(paths.PKG_DIR)}
    if shutil.which("systemd-run"):
        subprocess.run(["systemctl", "--user", "reset-failed", session_unit(iid)],
                       stderr=subprocess.DEVNULL, check=False)
        r = subprocess.run(["systemd-run", "--user", "--unit=" + session_unit(iid), "--collect",
                            "--description=waydroid-multi session for " + iid,
                            "-p", "KillSignal=SIGTERM", "-p", "TimeoutStopSec=60",
                            "--setenv=PYTHONPATH=" + env["PYTHONPATH"]] + cmd,
                           capture_output=True, text=True)
        if r.returncode == 0:
            return
        if "already" in (r.stderr or "").lower() or "exists" in (r.stderr or "").lower():
            return
    # Fallback: detached process
    log_dir = paths.user_cache_dir()
    os.makedirs(log_dir, exist_ok=True)
    full_env = dict(os.environ)
    full_env.update(env)
    with open(os.path.join(log_dir, iid + ".log"), "ab") as log:
        subprocess.Popen(cmd, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                         start_new_session=True, env=full_env)


def stop_session(iid):
    if session_active(iid):
        subprocess.run(["systemctl", "--user", "stop", session_unit(iid)], check=False)
        return True
    return False
