# SPDX-License-Identifier: GPL-3.0-or-later
"""Per-instance user session.

Runs as the user (normally in the transient unit waydroid-multi-session-<id>):
starts the window-labelling proxy, asks the daemon to start the container
with this session's Wayland/PulseAudio sockets, then hosts the instance's
clipboard/notification/user-monitor services until the instance stops.
"""
import logging
import os
import pwd
import signal
import subprocess
import sys
import time

import dbus
import dbus.mainloop.glib
from gi.repository import GLib

from .. import paths
from ..client import Daemon, DaemonError
from ..instance import Instance
from ..glibcompat import signal_add
from . import desktop, services

log = logging.getLogger("waydroid-multi.session")


def wayland_socket():
    disp = os.environ.get("WAYLAND_DISPLAY") or "wayland-0"
    if os.path.isabs(disp):
        return disp
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if not xdg:
        raise RuntimeError("XDG_RUNTIME_DIR is not set; start the session from your desktop session")
    return os.path.join(xdg, disp)


def pulse_socket():
    base = os.environ.get("PULSE_RUNTIME_PATH") or os.path.join(os.environ.get("XDG_RUNTIME_DIR", ""), "pulse")
    p = os.path.join(base, "native")
    return p if os.path.exists(p) else ""


class Session:
    def __init__(self, iid, background=False):
        self.iid = iid
        self.background = background
        self.inst = Instance.load(iid)
        self.loop = GLib.MainLoop()
        self.proxy = None
        self.services = []
        self.stopping = False
        self.started = False

    # -- proxy -------------------------------------------------------------------
    def start_proxy(self, upstream):
        listen = os.path.join(paths.user_runtime_dir(self.iid), "wayland-0")
        env = dict(os.environ)
        env["PYTHONPATH"] = os.path.dirname(paths.PKG_DIR) + os.pathsep + env.get("PYTHONPATH", "")
        self.proxy = subprocess.Popen(
            [sys.executable, "-m", "waydroid_multi.session.wlproxy", "--listen", listen,
             "--upstream", upstream, "--id", self.iid, "--name", self.inst.name],
            stdout=subprocess.PIPE, stdin=subprocess.DEVNULL, env=env)
        line = self.proxy.stdout.readline().decode().strip()
        if line != "ready":
            raise RuntimeError("Wayland proxy failed to start")
        GLib.io_add_watch(self.proxy.stdout, GLib.PRIORITY_DEFAULT, GLib.IO_IN | GLib.IO_HUP, self.on_proxy)
        return listen

    def on_proxy(self, src, cond):
        line = src.readline()
        if not line:
            log.warning("Wayland proxy exited")
            return False
        if line.decode().strip() == "close" and self.started:
            log.info("window closed")
            try:
                self.daemon.iface.ReportClose(self.iid, reply_handler=lambda: None,
                                              error_handler=lambda e: log.warning("ReportClose: %s", e))
            except dbus.DBusException as e:
                log.warning("ReportClose: %s", e)
        return True

    # -- lifecycle -----------------------------------------------------------------
    def session_dict(self, wl):
        pw = pwd.getpwuid(os.getuid())
        return {
            "user_name": pw.pw_name, "user_id": str(os.getuid()), "group_id": str(os.getgid()),
            "pid": str(os.getpid()), "xdg_runtime_dir": os.environ.get("XDG_RUNTIME_DIR", ""),
            "wayland_socket": wl, "pulse_socket": pulse_socket(),
            "background_start": "true" if self.background else "false",
        }

    def on_state(self, iid, state):
        if str(iid) == self.iid and str(state) in ("STOPPED", "DELETED") and self.started:
            log.info("instance stopped")
            self.quit()

    def on_daemon_owner(self, owner):
        # Called once right away with the current owner; act only on a change
        prev, self.daemon_owner = getattr(self, "daemon_owner", None), owner
        if prev is None or not owner or owner == prev:
            return
        if self.started and not self.stopping:
            log.info("daemon restarted, re-attaching")
            try:
                self.daemon = Daemon(self.bus)
                self.daemon.start(self.iid, self.session)
            except DaemonError as e:
                log.error("re-attach failed: %s", e)
                self.quit()

    def register_services(self):
        import gbinder
        inst = Instance.load(self.iid)   # protocols are known after the start
        w = inst.cfg["waydroid"]
        sm = gbinder.ServiceManager("/dev/" + w["binder"], w["service_manager_protocol"], w["binder_protocol"])
        self.sm = sm
        clip = services.clipboard_service(sm)
        if clip:
            self.services.append(clip)
        self.services.append(services.Notifications(sm, self.iid, inst.name))
        self.services.append(services.user_monitor_service(sm, self.on_unlocked, self.on_package))

    def on_unlocked(self, uid):
        log.info("Android user %s is ready", uid)
        if Instance.load(self.iid).getbool("desktop_apps"):
            GLib.idle_add(self.sync_app_entries)

    def on_package(self, mode, pkg, uid):
        if Instance.load(self.iid).getbool("desktop_apps"):
            GLib.idle_add(self.sync_app_entries)

    def sync_app_entries(self):
        try:
            from ..client import platform_service
            p = platform_service(self.iid, timeout=10)
            apps = p.getAppsInfo() or []
            icons = os.path.join(Instance.load(self.iid).data_dir, "icons")
            keep = set()
            for app in apps:
                desktop.write_app_entry(self.iid, self.inst.name, app, icons)
                keep.add("waydroid-multi.{}.{}.desktop".format(self.iid, app["packageName"]))
            for p in desktop.app_entries(self.iid):
                if os.path.basename(p) not in keep:
                    os.unlink(p)
        except Exception as e:  # noqa: BLE001
            log.warning("desktop entries: %s", e)
        return False

    def quit(self):
        if self.loop.is_running():
            self.loop.quit()

    def on_signal(self):
        log.info("stopping")
        self.stopping = True
        try:
            self.daemon.stop(self.iid)
        except DaemonError as e:
            log.warning("stop: %s", e)
        self.quit()
        return False

    def run(self):
        try:
            self._run()
        finally:
            self.cleanup()

    def _run(self):
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        self.bus = dbus.SystemBus()
        self.daemon = Daemon(self.bus)
        upstream = wayland_socket()
        if not os.path.exists(upstream):
            raise RuntimeError("Wayland socket {} not found; is a Wayland compositor running?".format(upstream))
        wl = self.start_proxy(upstream) if self.inst.getbool("window_labels") else upstream
        if self.inst.getbool("window_labels") and not os.path.exists(desktop.launcher_path(self.iid)):
            desktop.write_launcher(self.iid, self.inst.name)
        self.session = self.session_dict(wl)
        self.bus.add_signal_receiver(self.on_state, signal_name="StateChanged", dbus_interface=paths.DBUS_IFACE,
                                     bus_name=paths.DBUS_NAME, path=paths.DBUS_PATH)
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal_add(sig, self.on_signal)
        log.info("starting instance %s", self.iid)
        self.daemon.start(self.iid, self.session)
        self.started = True
        self.bus.watch_name_owner(paths.DBUS_NAME, self.on_daemon_owner)
        self.register_services()
        log.info("instance %s is running", self.iid)
        self.loop.run()

    def cleanup(self):
        for s in self.services:
            try:
                s.close()
            except Exception:  # noqa: BLE001
                pass
        if self.proxy and self.proxy.poll() is None:
            self.proxy.terminate()
            try:
                self.proxy.wait(3)
            except subprocess.TimeoutExpired:
                self.proxy.kill()


def main(iid, background=False):
    logging.basicConfig(level=logging.DEBUG if os.environ.get("WAYDROID_MULTI_DEBUG") else logging.INFO,
                        format="%(levelname)s %(message)s")
    try:
        Session(iid, background).run()
    except (DaemonError, RuntimeError, FileNotFoundError) as e:
        log.error("%s", e)
        time.sleep(0.2)
        return 1
    return 0
