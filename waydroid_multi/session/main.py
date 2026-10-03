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


def color_scheme():
    try:
        out = subprocess.run(["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
                             capture_output=True, text=True, timeout=3).stdout
        return "light" if "light" in out else ("dark" if "dark" in out else "light")
    except (OSError, subprocess.TimeoutExpired):
        return "dark"


def notify(summary, body):
    try:
        n = dbus.Interface(dbus.SessionBus().get_object("org.freedesktop.Notifications",
                                                        "/org/freedesktop/Notifications"),
                           "org.freedesktop.Notifications")
        n.Notify("waydroid-multi", 0, "camera-photo-symbolic", summary, body, [], {}, 4000)
    except dbus.DBusException:
        pass


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
        inst = self.inst
        os.makedirs(paths.user_runtime_dir(self.iid), mode=0o700, exist_ok=True)
        self.proxy = subprocess.Popen(
            [sys.executable, "-m", "waydroid_multi.session.wlproxy", "--listen", listen,
             "--upstream", upstream, "--id", self.iid, "--name", inst.name,
             "--width", inst.get("width"), "--height", inst.get("height"), "--zoom", inst.get("zoom"),
             "--frame", "on" if inst.getbool("window_frame") else "off", "--theme", color_scheme(),
             "--close-action", inst.get("close_action")],
            stdout=subprocess.PIPE, stdin=subprocess.DEVNULL, env=env,
            stderr=open(os.path.join(paths.user_runtime_dir(self.iid), "wlproxy.log"), "w")
            if os.environ.get("WDM_PROXY_TRACE") == "1" else None)
        # Read events with raw non-blocking reads: a buffered readline() under a
        # GLib fd watch can leave complete lines stuck in Python's buffer
        self.proxy_fd = self.proxy.stdout.fileno()
        self.proxy_buf = b""
        deadline = time.time() + 15
        while b"\n" not in self.proxy_buf and time.time() < deadline:
            chunk = os.read(self.proxy_fd, 4096)
            if not chunk:
                break
            self.proxy_buf += chunk
        line, _, self.proxy_buf = self.proxy_buf.partition(b"\n")
        if line.strip() != b"ready":
            raise RuntimeError("Wayland proxy failed to start")
        os.set_blocking(self.proxy_fd, False)
        GLib.io_add_watch(self.proxy_fd, GLib.PRIORITY_DEFAULT, GLib.IO_IN | GLib.IO_HUP, self.on_proxy)
        if self.proxy_buf:
            GLib.idle_add(lambda: (self._proxy_lines(), False)[1])
        return listen

    def on_proxy(self, fd, cond):
        try:
            chunk = os.read(self.proxy_fd, 65536)
        except BlockingIOError:
            return True
        except OSError:
            chunk = b""
        if not chunk:
            code = self.proxy.poll() if self.proxy else None
            log.warning("Wayland proxy exited (code %s); see ~/.cache/waydroid-multi/wlproxy-%s.log",
                        code, self.iid)
            return False
        self.proxy_buf += chunk
        self._proxy_lines()
        return True

    def _proxy_lines(self):
        while b"\n" in self.proxy_buf:
            line, _, self.proxy_buf = self.proxy_buf.partition(b"\n")
            self.handle_proxy_event(line.decode("utf-8", "replace").strip())

    def handle_proxy_event(self, ev):
        if ev == "close" and self.started:
            log.info("window closed")
            self._async("ReportClose", self.iid)
        elif ev.startswith("zoom "):
            # remember the window size the user picked (debounced)
            self.pending_zoom = ev.split()[1]
            if not getattr(self, "_zoom_timer", None):
                self._zoom_timer = GLib.timeout_add(1000, self._save_zoom)
        elif ev == "action screenshot":
            self.screenshot()
        elif ev.startswith("action key "):
            log.info("sending key %s", ev.split()[2])
            self._async("SendKey", self.iid, dbus.UInt32(int(ev.split()[2])))

    def _async(self, method, *args, ok=None):
        try:
            getattr(self.daemon.iface, method)(*args, reply_handler=ok or (lambda *a: None),
                                               error_handler=lambda e: log.warning("%s: %s", method, e),
                                               timeout=120)
        except dbus.DBusException as e:
            log.warning("%s: %s", method, e)

    def _save_zoom(self):
        self._zoom_timer = None
        self._async("SetConfig", self.iid, dbus.Dictionary({"zoom": self.pending_zoom}, signature="ss"))
        return False

    def screenshot(self):
        # ~/Pictures/Waydroid/<instance name>/Screenshot_<date>-<time>.png
        pics = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_PICTURES) or os.path.expanduser("~/Pictures")
        name = Instance.load(self.iid).name           # reload: the instance may have been renamed
        safe = "".join(c if c.isalnum() or c in "-_ ." else "_" for c in name).strip(" .") or "Instance " + self.iid
        folder = os.path.join(pics, "Waydroid", safe)
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, "Screenshot_{}.png".format(time.strftime("%Y%m%d-%H%M%S")))
        f = open(path, "wb")

        def done(*_):
            f.close()
            log.info("screenshot saved to %s", path)
            notify("Screenshot saved", path)

        def failed(e):
            f.close()
            try:
                os.unlink(path)          # don't leave an empty file behind
            except OSError:
                pass
            msg = e.get_dbus_message() if isinstance(e, dbus.DBusException) else str(e)
            log.warning("screenshot: %s", msg)
            notify("Screenshot failed", msg or "")
        try:
            self.daemon.iface.Screenshot(self.iid, dbus.types.UnixFd(f), reply_handler=done,
                                         error_handler=failed, timeout=60)
        except dbus.DBusException as e:
            failed(e)

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
