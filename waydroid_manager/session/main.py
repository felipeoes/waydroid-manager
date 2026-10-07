# SPDX-License-Identifier: GPL-3.0-or-later
"""Per-instance user session.

Runs as the user (normally in the transient unit waydroid-manager-session-<id>):
starts the window-labelling proxy, asks the daemon to start the container
with this session's Wayland/PulseAudio sockets, then hosts the instance's
clipboard/notification/user-monitor services until the instance stops.
"""
import collections
import logging
import os
import pwd
import resource
import shutil
import signal
import subprocess
import sys
import threading
import time

import dbus
import dbus.mainloop.glib
from gi.repository import GLib

from .. import paths
from ..client import Daemon, DaemonError, open_settings
from ..instance import Instance
from ..glibcompat import signal_add
from . import desktop, services

log = logging.getLogger("waydroid-manager.session")


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


def notify(summary, body, icon="camera-photo-symbolic"):
    try:
        n = dbus.Interface(dbus.SessionBus().get_object("org.freedesktop.Notifications",
                                                        "/org/freedesktop/Notifications"),
                           "org.freedesktop.Notifications")
        n.Notify("waydroid-manager", 0, icon, summary, body, [], {}, 4000)
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
        self.adb_serial = None   # what adb is connected to (or trying): "waydroid-<name>:5555"
        self.adb_key = None      # our adb public key, once the adb server is up; "" if unreadable

    # -- proxy -------------------------------------------------------------------
    def start_proxy(self, upstream, cpu_buffers=""):
        listen = os.path.join(paths.user_runtime_dir(self.iid), "wayland-0")
        env = dict(os.environ)
        env["PYTHONPATH"] = os.path.dirname(paths.PKG_DIR) + os.pathsep + env.get("PYTHONPATH", "")
        inst = self.inst
        android = self.daemon.get(self.iid)["android"]   # #0 follows stock's images, not its config default
        os.makedirs(paths.user_runtime_dir(self.iid), mode=0o700, exist_ok=True)
        # Android's display rotation reaches the proxy on its stdin, written by the daemon (watch_rotation)
        rot_r, self.rotation_w = os.pipe()
        self.proxy = subprocess.Popen(
            [sys.executable, "-m", "waydroid_manager.session.wlproxy", "--listen", listen,
             "--upstream", upstream, "--id", self.iid, "--name", inst.name,
             "--width", inst.get("width"), "--height", inst.get("height"), "--zoom", inst.get("zoom"),
             "--theme", color_scheme(), "--close-action", inst.get("close_action")]
            + (["--cpu-buffers", cpu_buffers] if cpu_buffers else [])
            + (["--logical-pointer"] if android == "11" else []),
            stdout=subprocess.PIPE, stdin=rot_r, env=env,
            stderr=open(os.path.join(paths.user_runtime_dir(self.iid), "wlproxy.log"), "w")
            if os.environ.get("WDM_PROXY_TRACE") == "1" else None)
        os.close(rot_r)
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
            log.warning("Wayland proxy exited (code %s); see ~/.cache/waydroid-manager/wlproxy-%s.log",
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
            if Instance.load(self.iid).get("close_action") == "stop":
                self.confirm_close()
            else:
                self._async("ReportClose", self.iid)
        elif ev.startswith("zoom "):
            # remember the window size the user picked (debounced)
            self.pending_zoom = ev.split()[1]
            if not getattr(self, "_zoom_timer", None):
                self._zoom_timer = GLib.timeout_add(1000, self._save_zoom)
        elif ev == "action screenshot":
            self.screenshot()
        elif ev == "action install":
            self.pick_apk()
        elif ev == "action settings":
            open_settings(self.iid)
        elif ev.startswith("install "):           # an APK dropped on the window
            self.install_apk(ev[len("install "):])
        elif ev.startswith("action key "):
            log.info("sending key %s", ev.split()[2])
            self._async("SendKey", self.iid, dbus.UInt32(int(ev.split()[2])))

    def _ask(self, attr, module, on_answer):
        """Run a small GTK process (gui/<module>.py) once at a time; on_answer gets its stdout."""
        if getattr(self, attr, None) and getattr(self, attr).poll() is None:
            return                     # already asking
        env = dict(os.environ)
        env["PYTHONPATH"] = os.path.dirname(paths.PKG_DIR) + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.Popen(
            [sys.executable, "-m", "waydroid_manager.gui." + module, "--name", Instance.load(self.iid).name],
            stdout=subprocess.PIPE, stdin=subprocess.DEVNULL, env=env)
        setattr(self, attr, proc)
        out = []

        def readable(fd, _cond):
            chunk = os.read(fd, 4096)
            if chunk:
                out.append(chunk)
                return True
            proc.stdout.close()
            proc.wait()
            on_answer(b"".join(out).decode("utf-8", "surrogateescape").rstrip("\n"))
            return False
        GLib.io_add_watch(proc.stdout.fileno(), GLib.PRIORITY_DEFAULT, GLib.IO_IN | GLib.IO_HUP, readable)

    def confirm_close(self):
        """Closing the window stops the instance: ask first."""
        def answer(a):
            if a == "stop":
                log.info("stop confirmed")
                self._async("ReportClose", self.iid)
        self._ask("confirm", "confirm", answer)

    def pick_apk(self):
        """Toolbar Install APK: pick a file, then hand it to the daemon like `app install`."""
        self._ask("picker", "pickapk", lambda path: path and self.install_apk(path))

    def install_apk(self, path):
        name = os.path.basename(path)
        try:
            f = open(path, "rb")
        except OSError as e:
            return notify("Install failed", str(e), "dialog-error-symbolic")
        notify("Installing " + name, "", "package-x-generic-symbolic")

        def done(res):
            f.close()
            log.info("installed %s: %s", name, res)
            notify("Installed " + name, str(res), "package-x-generic-symbolic")

        def failed(e):
            f.close()
            msg = e.get_dbus_message() if isinstance(e, dbus.DBusException) else str(e)
            log.warning("install %s: %s", name, msg)
            notify("Install failed", msg or name, "dialog-error-symbolic")
        try:
            self.daemon.iface.InstallApk(self.iid, dbus.types.UnixFd(f), name, reply_handler=done,
                                         error_handler=failed, timeout=900)
        except dbus.DBusException as e:
            failed(e)

    def _async(self, method, *args, ok=None):
        try:
            getattr(self.daemon.iface, method)(*args, reply_handler=ok or (lambda *a: None),
                                               error_handler=lambda e: log.warning("%s: %s", method, e),
                                               timeout=120)
        except dbus.DBusException as e:
            log.warning("%s: %s", method, e)

    def watch_rotation(self):
        """Have the daemon write Android's display rotation to the proxy, so the window turns with it."""
        self._async("WatchRotation", self.iid, dbus.types.UnixFd(self.rotation_w))

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

    # -- NVIDIA renderer ------------------------------------------------------------
    RENDERER_RESTARTS = 5      # starts within RENDERER_WINDOW seconds before it is given up

    RENDERER_WINDOW = 60

    def start_renderer(self, host):
        """Run the renderer Android's Venus driver talks to (gpu mode nvidia): as us, in its own
        process group, restarted when it dies. Returns its socket's directory."""
        self.renderer_dir = os.path.join(paths.user_runtime_dir(self.iid), "venus")
        os.makedirs(self.renderer_dir, exist_ok=True)
        os.chmod(self.renderer_dir, 0o755)          # Android's apps connect too, as other uids
        self.renderer_host = host
        self.renderer_starts = collections.deque(maxlen=self.RENDERER_RESTARTS)
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)   # inherited: a socket per Android client
        resource.setrlimit(resource.RLIMIT_NOFILE, (max(soft, min(65536, hard)), hard))
        self.spawn_renderer()
        return self.renderer_dir

    def spawn_renderer(self):
        sock = os.path.join(self.renderer_dir, "venus.sock")
        if os.path.lexists(sock):
            os.unlink(sock)
        host = self.renderer_host
        # CPU-readable buffers (screen captures: the task snapshot of every activity switch) come
        # from system memory: SurfaceFlinger rendering into NVIDIA's own LINEAR memory ends in
        # Xid 69 and Android's display restarts (docs/spike-findings.md)
        env = dict(os.environ, RENDER_SERVER_EXEC_PATH=host + "/bin/virgl_render_server",
                   LD_LIBRARY_PATH=host + "/lib", WAYDROID_NVIDIA_CPU_LINEAR="0")
        os.makedirs(paths.user_cache_dir(), exist_ok=True)
        log_path = os.path.join(paths.user_cache_dir(), "renderer-{}.log".format(self.iid))
        with open(log_path, "ab") as out:
            self.renderer = subprocess.Popen(
                [host + "/bin/virgl_test_server", "--no-virgl", "--venus", "--multi-clients", "--socket-path", sock],
                env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True)
        self.renderer_starts.append(time.monotonic())
        GLib.child_watch_add(GLib.PRIORITY_DEFAULT, self.renderer.pid, self.renderer_exited)
        deadline = time.monotonic() + 10
        while not os.path.exists(sock):           # Android connects as it boots
            if self.renderer.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("the NVIDIA renderer didn't start; see " + log_path)
            time.sleep(0.05)

    def renderer_exited(self, pid, status):
        if self.stopping or pid != self.renderer.pid:
            return
        starts = self.renderer_starts
        if len(starts) == starts.maxlen and time.monotonic() - starts[0] < self.RENDERER_WINDOW:
            log.error("the NVIDIA renderer keeps exiting; see %s", os.path.join(paths.user_cache_dir(),
                                                                               "renderer-{}.log".format(self.iid)))
            return
        log.warning("the NVIDIA renderer exited (status %s); restarting it", status)
        try:
            self.spawn_renderer()
        except (OSError, RuntimeError) as e:
            log.error("%s", e)

    def stop_renderer(self):
        if getattr(self, "renderer", None):
            try:
                os.killpg(self.renderer.pid, signal.SIGTERM)   # its per-client render servers too
            except ProcessLookupError:
                pass

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
                self.watch_rotation()
            except DaemonError as e:
                log.error("re-attach failed: %s", e)
                self.quit()

    def register_services(self):
        from ..stock import load_gbinder
        gbinder = load_gbinder()
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
        if shutil.which("adb") and self.adb_key is None:
            threading.Thread(target=self.adb_prepare, daemon=True, name="adb").start()
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
                keep.add("waydroid-manager.{}.{}.desktop".format(self.iid, app["packageName"]))
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
        render = self.daemon.prepare_gpu(self.iid)
        wl = self.start_proxy(upstream, cpu_buffers=render["cpu_buffers"])
        if not os.path.exists(desktop.launcher_path(self.iid)):
            desktop.write_launcher(self.iid, self.inst.name)
        self.session = self.session_dict(wl)
        if render["mode"] == "nvidia":
            self.session["venus_dir"] = self.start_renderer(render["renderer"])
        self.bus.add_signal_receiver(self.on_state, signal_name="StateChanged", dbus_interface=paths.DBUS_IFACE,
                                     bus_name=paths.DBUS_NAME, path=paths.DBUS_PATH)
        # A rename, or another instance taking or freeing our name, changes our adb name
        for sig in ("ConfigChanged", "InstanceAdded", "InstanceRemoved"):
            self.bus.add_signal_receiver(self.adb_sync, signal_name=sig, dbus_interface=paths.DBUS_IFACE,
                                         bus_name=paths.DBUS_NAME, path=paths.DBUS_PATH)
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal_add(sig, self.on_signal)
        log.info("starting instance %s", self.iid)
        self.daemon.start(self.iid, self.session)
        self.started = True
        self.watch_rotation()
        self.bus.watch_name_owner(paths.DBUS_NAME, self.on_daemon_owner)
        self.register_services()
        log.info("instance %s is running", self.iid)
        self.loop.run()

    def adb_prepare(self):
        """In a thread: the adb server's first start creates our key, which takes seconds."""
        home = os.environ.get("ANDROID_USER_HOME") or os.path.expanduser("~/.android")
        key = ""
        try:
            subprocess.run(["adb", "start-server"], stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
            with open(os.path.join(home, "adbkey.pub")) as f:
                key = f.read().strip()
        except (OSError, subprocess.TimeoutExpired) as e:
            log.warning("adb key: %s", e)
        GLib.idle_add(self.adb_ready, key)

    def adb_ready(self, key):
        self.adb_key = key
        self.adb_sync()
        return False

    def adb_sync(self, *_):
        """Show the instance in `adb devices` as waydroid-<name>:5555 (its /etc/hosts name)."""
        if self.adb_key is not None:
            self.daemon.iface.Get(self.iid, reply_handler=self.adb_on_info, timeout=60,
                                  error_handler=lambda e: log.warning("adb: %s", e))

    def adb_on_info(self, info):
        serial = str(info.get("adb_host", "")) + ":5555" if info.get("adb_host") else None
        if serial == self.adb_serial:
            return
        old, self.adb_serial = self.adb_serial, serial
        if old:
            threading.Thread(target=self.adb_run, args=(["disconnect", old],), daemon=True).start()
        if not serial:
            return

        def connect(*_):
            threading.Thread(target=self.adb_connect, args=(serial,), daemon=True, name="adb").start()

        def failed(e):
            log.warning("adb key not authorized: %s", e)
            connect()
        # Trust our adb key in Android first, like the emulator, so there is no "Allow USB debugging?" prompt
        if self.adb_key:
            self.daemon.iface.AuthorizeAdbKey(self.iid, self.adb_key, reply_handler=connect,
                                              error_handler=failed, timeout=60)
        else:
            connect()

    @staticmethod
    def adb_run(args):
        try:
            r = subprocess.run(["adb"] + args, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                               timeout=30)
            return (r.stdout + r.stderr).strip()
        except (OSError, subprocess.TimeoutExpired) as e:
            return str(e)

    def adb_connect(self, serial):
        """In a thread: adbd may come up on 5555 a little after Android's user unlocks."""
        for _ in range(20):
            if serial != self.adb_serial:   # renamed meanwhile
                return
            out = self.adb_run(["connect", serial])
            if out.startswith(("connected to", "already connected to")):
                log.info("adb: %s", out)
                return
            time.sleep(3)
        log.warning("adb connect %s: %s", serial, out)

    def cleanup(self):
        self.stopping = True
        self.stop_renderer()
        if getattr(self, "rotation_w", None) is not None:
            os.close(self.rotation_w)
        if getattr(self, "confirm", None) and self.confirm.poll() is None:
            self.confirm.terminate()
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
    logging.basicConfig(level=logging.DEBUG if os.environ.get("WAYDROID_MANAGER_DEBUG") else logging.INFO,
                        format="%(levelname)s %(message)s")
    try:
        Session(iid, background).run()
    except (DaemonError, RuntimeError, FileNotFoundError) as e:
        log.error("%s", e)
        time.sleep(0.2)
        return 1
    return 0
