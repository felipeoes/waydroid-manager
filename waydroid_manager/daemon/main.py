# SPDX-License-Identifier: GPL-3.0-or-later
"""waydroid-manager root daemon: one process managing every instance.

D-Bus API on the system bus (io.github.waydroidmanager.Manager). Long
operations run in worker threads under a per-instance lock; results and
signals are delivered from the GLib main loop. The daemon never makes binder
calls itself (see hwhelper.py).
"""
import collections
import contextlib
import fcntl
import logging
import os
import pwd
import shutil
import stat
import subprocess
import sys
import threading
import time

import dbus
import dbus.exceptions
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib

from .. import __version__, catalog, gpu, paths, stock, stockctl
from ..instance import (CREATE_ONLY, Instance, SETTINGS, list_ids, validate_adb_key, validate_id, validate_prop,
                        validate_setting)
from ..netconfig import host_names
from ..registry import allocate_index
from . import container, images, nvidia, storage
from .network import Network
from .util import active_ids, adb_disconnect, container_pid, log, lxc_state, open_in_container

ERR = "io.github.waydroidmanager.Error"
AID_SYSTEM, AID_SHELL = 1000, 2000   # adb_keys is system:shell 0640, as Android writes it on "Always allow"
ACTIVE = ("RUNNING", "FROZEN")
MAX_PER_USER = 64


class Error(dbus.exceptions.DBusException):
    def __init__(self, msg, kind="Failed"):
        super().__init__(str(msg), name=ERR + "." + kind)


def _regular_fd(fd, write):
    """Take a passed file descriptor; it must be a regular file open for reading (or
    writing). Pipes and the like could block a worker thread forever."""
    raw = fd.take()
    try:
        acc = fcntl.fcntl(raw, fcntl.F_GETFL) & os.O_ACCMODE
        if not stat.S_ISREG(os.fstat(raw).st_mode) or acc == (os.O_RDONLY if write else os.O_WRONLY):
            raise Error("expected a regular file opened for {}".format("writing" if write else "reading"),
                        "InvalidArgs")
    except (OSError, Error):
        os.close(raw)
        raise
    return raw


def _s(d):
    return {str(k): str(v) for k, v in d.items()}


def stock_waydroid_cfg():
    """Stock's [waydroid] section without what each instance has its own of."""
    w = dict(stock.load_stock_cfg()["waydroid"])
    for k in ("binder", "vndbinder", "hwbinder", "images_path"):
        w.pop(k, None)
    return w


class Manager(dbus.service.Object):
    def __init__(self, bus):
        super().__init__(bus, paths.DBUS_PATH)
        self.bus = bus
        self.net = Network()
        self.locks = collections.defaultdict(threading.Lock)
        self.transient = {}      # id -> STARTING/STOPPING/CLONING/DELETING
        self.picked = set()      # ids starting whose CPUs are picked (see cpus_busy)
        self.known = {}          # id -> last observed state
        self.sessions = {}       # id -> dict(sender, pid, uid, watch, session)
        self.helpers = {}        # id -> Popen
        self.last_close = {}
        self.disk = {}           # id -> (bytes used as a string, time measured)
        self.names = {}          # id -> "waydroid-<name>", its /etc/hosts name for adb
        self.names_lock = threading.Lock()   # computing and writing them, from several threads
        self.stopping_by_us = set()
        self.key_fds = {}        # id -> fd of the instance's keyboard FIFO (kept open while running)
        self.dbus_info = dbus.Interface(bus.get_object("org.freedesktop.DBus", "/org/freedesktop/DBus"),
                                        "org.freedesktop.DBus")

    # -- helpers -------------------------------------------------------------
    def caller(self, sender):
        return int(self.dbus_info.GetConnectionUnixUser(sender))

    def load(self, iid):
        try:
            iid = "0" if str(iid) == "default" else str(iid)
            return Instance.load(validate_id(iid))
        except (FileNotFoundError, ValueError) as e:
            raise Error(e, "NotFound")

    def ensure_stock_instance(self, uid):
        """#0: the caller's stock Waydroid data, run as a managed instance. Its data is
        bind-mounted in place at start, never copied (see container.start)."""
        if uid == 0 or os.path.isfile(os.path.join(paths.instance_dir("0"), "instance.cfg")):
            return
        try:
            os.close(storage.open_stock_data(uid))
        except (OSError, KeyError):
            return  # no stock Waydroid set up for this user (or no such user)
        # ponytail: one #0 owner per host, first user wins; per-user #0 needs a per-user index
        try:
            os.mkdir(paths.instance_dir("0"), 0o700)
        except FileExistsError:
            return
        try:
            inst = Instance.new("0", 0, uid, "", stock_waydroid_cfg(), {})  # image set picked at start
            inst.cfg["instance"]["name"] = "Stock Waydroid"
            inst.save()
            container.ensure_dirs(inst)
        except Exception:  # noqa: BLE001 - List works without #0; the next List tries again
            log.exception("creating #0 failed")
            shutil.rmtree(paths.instance_dir("0"), ignore_errors=True)
            return
        self.refresh_hosts()

    def check_owner(self, inst, uid):
        if uid != 0 and uid != inst.owner_uid:
            raise Error("instance #{} belongs to another user".format(inst.id), "AccessDenied")

    def state(self, iid):
        return self.transient.get(iid) or lxc_state(iid)

    def all_instances(self):
        out = []
        for iid in list_ids():
            try:
                out.append(Instance.load(iid))
            except Exception as e:  # noqa: BLE001
                log.warning("skipping broken instance %s: %s", iid, e)
        return out

    def hosts(self):
        return [(i.mac, self.net.ip_for(i)) for i in self.all_instances()]

    def refresh_hosts(self):
        """DHCP leases and the /etc/hosts names (adb) for the current instances."""
        self.net.reload_hosts(self.hosts())
        self.refresh_names()

    def refresh_names(self):
        # Load the instances under the lock: a thread with an older list can't write after a newer one
        with self.names_lock:
            insts = self.all_instances()
            self.names = host_names([(i.id, i.name) for i in insts])
            self.net.write_names([(self.net.ip_for(i), self.names[i.id]) for i in insts])

    def images_in_use(self):
        """Every instance's image set: the store keeps them (a stopped instance switches to
        a newer set of its Android version at its next start)."""
        return [i.image_id for i in self.all_instances() if i.image_id]

    def check_android(self, android, settings):
        """Refuse settings an instance's Android version can't run."""
        if settings.get("root") == "true" and catalog.get(android, "sdk") not in container.ROOT_SDKS:
            raise Error("root is only available on Android 11 and 13", "InvalidArgs")
        picked = next((g for g in gpu.gpus() if g.pci == settings.get("gpu")), None)
        if picked and picked.driver == "nvidia" and android in catalog.VERSIONS and not catalog.get(android, "nvidia"):
            raise Error("Android {} has no NVIDIA build".format(android), "InvalidArgs")

    def cpus_busy(self, iid):
        """The other instances' (cpus limit, pinned CPUs), for iid's pick: called under
        container._pin_lock right before iid's pick is written. A starting instance counts
        once its pick is written (until then its config holds its last run's pin), a stopping
        or deleting one no longer does."""
        up = active_ids()
        self.picked.add(iid)
        return [(i.get("cpus"), container.pinned_cpus(i)) for i in self.all_instances()
                if i.id != iid and (i.id in self.picked or i.id in up and i.id not in self.transient)]

    def disk_used(self, inst):
        """Bytes the instance's directory takes (data, writable layers); measured with du in a
        thread, at most once a minute, since List runs on the main loop."""
        val, at = self.disk.get(inst.id, ("", 0))
        if time.time() - at > 60:
            self.disk[inst.id] = (val, time.time())

            def measure():
                args, fds = [inst.dir], []
                if inst.index == 0:
                    # #0's data is stock's, in the user's home (bound at data/ only while running)
                    try:
                        fds.append(storage.open_stock_data(inst.owner_uid))
                        args = ["/proc/self/fd/{}".format(fds[0])] + [
                            os.path.join(inst.dir, n) for n in os.listdir(inst.dir) if n != "data"]
                    except OSError:
                        pass
                # -x: not into the mounted rootfs (shared images); -D: through the fd link only;
                # du exits 1 when Android deletes files mid-walk but still prints the total
                try:
                    r = subprocess.run(["du", "-sxcD", "--block-size=1"] + args, capture_output=True, text=True,
                                       pass_fds=fds)
                finally:
                    for fd in fds:
                        os.close(fd)
                if r.stdout.split():
                    self.disk[inst.id] = (r.stdout.split()[-2], time.time())
            threading.Thread(target=measure, daemon=True, name="du-" + inst.id).start()
        return val

    def set_transient(self, iid, st):
        if st:
            self.transient[iid] = st
        else:
            self.transient.pop(iid, None)
        GLib.idle_add(self._emit_state, iid)

    def _emit_state(self, iid):
        st = self.transient.get(iid) or lxc_state(iid) if os.path.isdir(paths.instance_dir(iid)) else "DELETED"
        self.known[iid] = st
        self.StateChanged(iid, st)
        return False

    def info(self, inst):
        d = inst.info()
        d["state"] = self.state(inst.id)
        d["ip"] = self.net.ip_for(inst)
        d["session"] = "yes" if inst.id in self.sessions else "no"
        d["pending_id_reset"] = inst.cfg["instance"].get("pending_id_reset", "false")
        d["android"] = container.android_of(inst) or ""
        d["disk_used"] = self.disk_used(inst)
        d["adb_host"] = self.names.get(inst.id, "")
        d["mem_used"] = ""
        d["pinned"] = ""  # CPUs in use: the cpuset setting, or the ones picked at start
        if d["state"] in ACTIVE:
            for key, name in (("mem_used", "memory.current"), ("pinned", "cpuset.cpus")):
                try:
                    with open("/sys/fs/cgroup/lxc.payload.{}/{}".format(inst.container, name)) as f:
                        d[key] = f.read().strip()
                except OSError:
                    pass
        return d

    def run_async(self, iid, fn, reply, error, lock=True):
        def work():
            try:
                if lock:
                    with self.locks[iid]:
                        res = fn()
                else:
                    res = fn()
                GLib.idle_add(lambda: (reply() if res is None else reply(res), False)[1])
            except Exception as e:  # noqa: BLE001
                if not isinstance(e, Error):
                    log.exception("operation on %s failed", iid)
                err = e if isinstance(e, Error) else Error(e)
                GLib.idle_add(lambda: (error(err), False)[1])
        threading.Thread(target=work, daemon=True, name="op-" + str(iid)).start()

    # -- hardware helper -----------------------------------------------------
    def start_helper(self, inst):
        self.stop_helper(inst.id)
        env = dict(os.environ)
        env["PYTHONPATH"] = os.path.dirname(paths.PKG_DIR) + os.pathsep + env.get("PYTHONPATH", "")
        p = subprocess.Popen([sys.executable, "-m", "waydroid_manager.daemon.hwhelper", inst.cfg_path],
                             stdout=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True, env=env)
        self.helpers[inst.id] = p

        def reader():
            for line in p.stdout:
                line = line.strip()
                if line.startswith("wdm:"):
                    GLib.idle_add(self.on_hw_event, inst.id, line[4:])
                elif line:
                    log.debug("%s hwhelper: %s", inst.id, line)
        threading.Thread(target=reader, daemon=True, name="hw-" + inst.id).start()

    def stop_helper(self, iid):
        p = self.helpers.pop(iid, None)
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(3)
            except subprocess.TimeoutExpired:
                p.kill()

    def on_hw_event(self, iid, word):
        if word in ("registered",):
            log.debug("%s: IHardware service registered", iid)
            return False
        log.info("%s: android requested %s", iid, word)
        if word == "suspend":
            if time.time() - self.last_close.get(iid, 0) < 15:
                return False  # already handled as a window close
            try:
                inst = Instance.load(iid)
            except FileNotFoundError:
                return False
            self.apply_action(iid, inst.get("idle_action"))
        elif word == "reboot":
            self.run_async(iid, lambda: self._restart(iid), lambda *a: None, lambda e: log.error("%s", e))
        elif word == "upgrade":
            log.warning("%s: in-Android OTA upgrade refused (images are shared); "
                        "use 'waydroid-manager images update'", iid)
        return False

    def apply_action(self, iid, action):
        if action == "stop":
            self.run_async(iid, lambda: self._stop(iid), lambda *a: None, lambda e: log.error("%s", e))
        elif action == "freeze":
            self.run_async(iid, lambda: self._freeze(iid), lambda *a: None, lambda e: log.error("%s", e))

    # -- core operations (run in worker threads, instance lock held) -----------
    def _start(self, iid, uid, session, sender):
        inst = Instance.load(iid)
        st = lxc_state(iid)
        if st in ACTIVE:
            # Re-attach (e.g. after a daemon restart) or a second session
            cur = self.sessions.get(iid)
            if cur and cur["sender"] != sender and self._name_alive(cur["sender"]):
                raise Error("instance #{} is already running in another session".format(iid), "Busy")
            GLib.idle_add(self._attach_session, iid, uid, session, sender)
            if iid not in self.helpers:
                self.start_helper(inst)
            return
        self.set_transient(iid, "STARTING")
        try:
            container.start(inst, self.net, self.hosts(), session, uid, self.images_in_use(),
                            lambda: self.cpus_busy(iid))
            inst = Instance.load(iid)
            self.start_helper(inst)
            GLib.idle_add(self._attach_session, iid, uid, session, sender)
        finally:
            self.set_transient(iid, None)
            self.picked.discard(iid)
        if inst.cfg["instance"].get("pending_id_reset") == "true":
            threading.Thread(target=self._finish_id_reset, args=(iid,), daemon=True).start()
        if inst.index and catalog.get(inst.get("android"), "provision") and \
                inst.cfg["instance"].get("set_up") != "true":
            threading.Thread(target=self._finish_setup, args=(iid,), daemon=True).start()

    def _finish_setup(self, iid):
        try:
            if not container.wait_boot(Instance.load(iid)):
                return
            with self.locks[iid]:
                inst = Instance.load(iid)
                container.finish_setup(inst)
                inst.cfg["instance"]["set_up"] = "true"
                inst.save()
            log.info("%s: marked as set up", iid)
        except Exception:  # noqa: BLE001
            log.exception("%s: marking the device set up failed", iid)

    def _finish_id_reset(self, iid):
        try:
            inst = Instance.load(iid)
            if not container.wait_boot(inst):
                return
            time.sleep(5)
            with self.locks[iid]:
                res = storage.reset_ids_online(iid)
                inst = Instance.load(iid)
                inst.cfg["instance"]["pending_id_reset"] = "false"
                inst.save()
            log.info("%s: identity reset: %s", iid, "; ".join(res))
            GLib.idle_add(lambda: (self.ConfigChanged(iid), False)[1])
        except Exception:  # noqa: BLE001
            log.exception("%s: identity reset failed", iid)

    def _name_alive(self, name):
        try:
            return bool(self.dbus_info.NameHasOwner(name))
        except dbus.DBusException:
            return False

    def _attach_session(self, iid, uid, session, sender):
        old = self.sessions.pop(iid, None)
        if old and old.get("watch"):
            old["watch"].cancel()

        def owner_changed(new_owner):
            if not new_owner and self.sessions.get(iid, {}).get("sender") == sender:
                log.info("%s: session went away, stopping instance", iid)
                self.sessions.pop(iid, None)
                self.run_async(iid, lambda: self._stop(iid), lambda *a: None, lambda e: log.error("%s", e))
        watch = self.bus.watch_name_owner(sender, owner_changed)
        self.sessions[iid] = {"sender": sender, "uid": uid, "session": session, "watch": watch}
        return False

    def _close_key_fd(self, iid):
        fd = self.key_fds.pop(iid, None)
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    def send_key(self, iid, code):
        """Inject an evdev key straight into Android's keyboard input FIFO.

        The hwcomposer drops key codes >= 239 (e.g. KEYCODE_APP_SWITCH, 580),
        so these bypass Wayland. The writer stays open while the instance
        runs, so Android never sees end-of-file on the FIFO.
        """
        import struct
        for attempt in (0, 1):
            fd = self.key_fds.get(iid)
            if fd is None:
                pid = container_pid(iid)
                if not pid:
                    raise Error("instance #{} is not running".format(iid), "NotRunning")
                # Android's /dev is writable from inside: don't follow its links to the host
                fd = open_in_container(pid, "dev/input/wl_keyboard_events", os.O_WRONLY | os.O_NONBLOCK)
                if not stat.S_ISFIFO(os.fstat(fd).st_mode):
                    os.close(fd)
                    raise Error("instance #{}: keyboard input is not a FIFO".format(iid))
                self.key_fds[iid] = fd
            try:
                now = time.time()
                sec, usec = int(now), int((now % 1) * 1e6)
                os.write(fd, struct.pack("=qqHHi", sec, usec, 1, code, 1))
                os.write(fd, struct.pack("=qqHHi", sec, usec + 1, 1, code, 0))
                return
            except OSError:
                self._close_key_fd(iid)
                if attempt:
                    raise

    def _stop(self, iid):
        inst = Instance.load(iid)
        self._close_key_fd(iid)
        self.set_transient(iid, "STOPPING")
        self.stopping_by_us.add(iid)
        if iid in self.names:
            adb_disconnect(self.names[iid] + ":5555")
        try:
            self.stop_helper(iid)
            container.stop(inst)
        finally:
            self.stopping_by_us.discard(iid)
            self.set_transient(iid, None)
            GLib.idle_add(self._drop_session, iid)
            self._maybe_network_down()

    def _drop_session(self, iid):
        s = self.sessions.pop(iid, None)
        if s and s.get("watch"):
            s["watch"].cancel()
        return False

    def _maybe_network_down(self, exclude=None):
        for i in list_ids():
            if i != exclude and (self.transient.get(i) == "STARTING" or lxc_state(i) in ACTIVE):
                return
        self.net.ensure_down()

    def _restart(self, iid):
        s = self.sessions.get(iid)
        if not s:
            return self._stop(iid)
        inst = Instance.load(iid)
        self.stop_helper(iid)
        container.stop(inst, keep_stock=True)  # #0: stock Waydroid stays paused across the reboot
        session = dict(s["session"])
        session["background_start"] = "false"
        try:
            container.start(inst, self.net, self.hosts(), session, s["uid"], self.images_in_use(),
                            lambda: self.cpus_busy(iid))
        except Exception:
            container.cleanup(inst)  # #0 didn't come back: give stock Waydroid back
            raise
        finally:
            self.picked.discard(iid)
        self.start_helper(Instance.load(iid))
        GLib.idle_add(self._emit_state, iid)

    def _freeze(self, iid):
        inst = Instance.load(iid)
        container.freeze(inst)
        GLib.idle_add(self._emit_state, iid)

    def _unfreeze(self, iid):
        inst = Instance.load(iid)
        container.unfreeze(inst)
        GLib.idle_add(self._emit_state, iid)

    def _create(self, uid, opts):
        """Create an instance with the lowest free number; returns its id."""
        clone_from = opts.pop("clone_from", "")
        if clone_from == "0":
            clone_from = "default"
        reset_ids = opts.pop("reset_ids", "true") != "false"
        # counted again here: the check in Create() races with parallel calls
        if uid != 0 and sum(1 for i in self.all_instances() if i.owner_uid == uid and i.index) >= MAX_PER_USER:
            raise Error("instance limit reached ({} per user)".format(MAX_PER_USER), "LimitReached")
        sid = self._source_id(clone_from, uid) if clone_from else None
        # the source's lock: it can't start (and change its data) while it's copied
        with self.locks[sid] if sid else contextlib.nullcontext():
            src_data, src_fd = self._clone_source(clone_from, uid, sid) if clone_from else (None, None)
            try:
                return self._create_from(uid, opts, clone_from, src_data, reset_ids)
            finally:
                if src_fd is not None:
                    os.close(src_fd)

    def _create_from(self, uid, opts, clone_from, src_data, reset_ids):
        settings = {}
        props = {}
        if clone_from:
            if clone_from != "default":
                # A clone starts with the source's settings; options override them
                src = Instance.load(clone_from)
                for k in SETTINGS:
                    if k != "name" and k in src.cfg["instance"]:
                        settings[k] = src.cfg["instance"][k]
                props.update(src.cfg["properties"])
        for k, v in opts.items():
            if k.startswith("prop:"):
                if v:
                    props[k[5:]] = validate_prop(k[5:], v, trusted=uid == 0)
                else:
                    props.pop(k[5:], None)
            else:
                settings[k] = validate_setting(k, v)
        used = [i.index for i in self.all_instances()]
        index = allocate_index(used)
        iid = str(index)
        if os.path.exists(paths.instance_dir(iid)):
            raise Error("instance #{} already exists on disk".format(iid), "Exists")
        settings.setdefault("name", "Instance {}".format(iid))
        if clone_from == "default":   # the copy of stock's data must run stock's Android
            android = container.stock_android()
            if not android:
                raise Error("stock Waydroid's Android version can't be cloned: it isn't one of "
                            + ", ".join(catalog.VERSIONS), "InvalidArgs")
            settings["android"] = android
        android = settings.setdefault("android", catalog.DEFAULT)
        self.check_android(android, settings)
        image_id = images.latest(android) or images.install(android)
        inst = Instance.new(iid, index, uid, image_id, stock_waydroid_cfg(), props)
        for k, v in settings.items():
            inst.cfg["instance"][k] = v
        # The instance becomes visible with save(): hold its lock (Start waits) and mark
        # it busy until its data is complete
        with self.locks[iid]:
            if src_data:
                self.set_transient(iid, "CLONING")
            inst.save()
            try:
                container.ensure_dirs(inst)
                if src_data:
                    storage.copy_data(src_data, inst.data_dir)
                    if clone_from != "default":  # the source's own /system and /vendor changes
                        storage.copy_data(os.path.join(paths.instance_dir(clone_from), "overlay_rw"),
                                          os.path.join(inst.dir, "overlay_rw"))
                    pw = pwd.getpwuid(uid)
                    os.chown(inst.data_dir, uid, pw.pw_gid, follow_symlinks=False)
                    if reset_ids:
                        storage.reset_ids_offline(inst.data_dir)
                        inst.cfg["instance"]["pending_id_reset"] = "true"
                    inst.cfg["instance"]["cloned_from"] = clone_from
                    inst.save()
            except Exception:
                storage.delete_instance_files(inst)
                raise
            finally:
                self.set_transient(iid, None)
        self.refresh_hosts()
        GLib.idle_add(lambda: (self.InstanceAdded(iid), False)[1])
        return iid

    def _source_id(self, src, uid):
        """Instance whose data a clone of src copies: 'default' is #0's if #0 is the caller's."""
        if src != "default":
            return src
        try:
            return "0" if Instance.load("0").owner_uid == uid else src
        except FileNotFoundError:
            return src

    def _clone_source(self, src, uid, sid):
        """Returns (path to copy from, fd to close afterwards or None)."""
        if src == "default":
            if stockctl.container_state() != "STOPPED":
                raise Error("stop the stock Waydroid session first ('waydroid session stop')", "Busy")
            if sid == "0" and lxc_state("0") != "STOPPED":  # #0 runs on the same data
                raise Error("stop instance #0 before cloning it", "Busy")
            try:
                fd = storage.open_stock_data(uid)
            except OSError:
                raise Error("no stock Waydroid data found at {}".format(storage.stock_data_path(uid)), "NotFound")
            return "/proc/{}/fd/{}".format(os.getpid(), fd), fd
        sinst = self.load(src)
        self.check_owner(sinst, uid)
        if self.state(sinst.id) != "STOPPED":
            raise Error("stop instance #{} before cloning it".format(src), "Busy")
        return sinst.data_dir, None

    def _delete(self, iid):
        inst = Instance.load(iid)
        if lxc_state(iid) != "STOPPED":
            self._stop(iid)
        self.set_transient(iid, "DELETING")
        try:
            storage.delete_instance_files(inst)
        finally:
            self.transient.pop(iid, None)
        self.refresh_hosts()
        GLib.idle_add(lambda: (self.InstanceRemoved(iid), False)[1])

    # -- D-Bus API -------------------------------------------------------------
    @dbus.service.method(paths.DBUS_IFACE, in_signature="", out_signature="a{ss}")
    def GetInfo(self):
        warn = stock.check_version() or ""
        return {"version": __version__, "stock_version": stock.version(), "warnings": warn,
                "subnet": str(self.net.cfg.network), "bridge": self.net.cfg.bridge,
                "image": images.current_id(), "stock_image": images.stock_image_id()}

    @dbus.service.method(paths.DBUS_IFACE, in_signature="", out_signature="aa{ss}", sender_keyword="sender")
    def List(self, sender):
        """The caller's instances (root: everyone's)."""
        uid = self.caller(sender)
        self.ensure_stock_instance(uid)
        return [self.info(i) for i in self.all_instances() if uid == 0 or i.owner_uid == uid]

    @dbus.service.method(paths.DBUS_IFACE, in_signature="s", out_signature="a{ss}", sender_keyword="sender")
    def Get(self, iid, sender):
        inst = self.load(iid)
        self.check_owner(inst, self.caller(sender))
        return self.info(inst)

    @dbus.service.method(paths.DBUS_IFACE, in_signature="a{ss}", out_signature="s",
                         sender_keyword="sender", async_callbacks=("reply", "error"))
    def Create(self, opts, sender, reply, error):
        """Create (or clone, with opts["clone_from"]) an instance; returns its number."""
        uid = self.caller(sender)
        if uid != 0 and sum(1 for i in self.all_instances() if i.owner_uid == uid and i.index) >= MAX_PER_USER:
            return error(Error("instance limit reached ({} per user)".format(MAX_PER_USER), "LimitReached"))
        # Creates are serialised: the lowest free number is reserved by creating its directory
        self.run_async("__create__", lambda: self._create(uid, _s(opts)), reply, error)

    @dbus.service.method(paths.DBUS_IFACE, in_signature="s", out_signature="",
                         sender_keyword="sender", async_callbacks=("reply", "error"))
    def Delete(self, iid, sender, reply, error):
        try:
            inst = self.load(iid)
            self.check_owner(inst, self.caller(sender))
            if inst.index == 0:
                raise Error("#0 is your stock Waydroid; it can't be deleted", "InvalidArgs")
        except Error as e:
            return error(e)
        self.run_async(inst.id, lambda: self._delete(inst.id), reply, error)

    @dbus.service.method(paths.DBUS_IFACE, in_signature="sa{ss}", out_signature="", sender_keyword="sender")
    def SetConfig(self, iid, values, sender):
        inst = self.load(iid)
        uid = self.caller(sender)
        self.check_owner(inst, uid)
        with self.locks[inst.id]:
            inst = Instance.load(inst.id)
            try:
                for k, v in _s(values).items():
                    if k.startswith("prop:"):
                        key = k[5:]
                        if v == "":
                            inst.cfg["properties"].pop(key, None)
                        else:
                            inst.cfg["properties"][key] = validate_prop(key, v, trusted=uid == 0)
                    elif k in CREATE_ONLY:
                        raise ValueError("{} is chosen when an instance is created".format(k))
                    else:
                        inst.set(k, v)
                self.check_android(container.android_of(inst), {k: inst.get(k) for k in SETTINGS})
            except ValueError as e:
                raise Error(e, "InvalidArgs")
            inst.save()
        if "name" in values:
            self.refresh_names()
        self.ConfigChanged(inst.id)

    @dbus.service.method(paths.DBUS_IFACE, in_signature="sa{ss}", out_signature="",
                         sender_keyword="sender", async_callbacks=("reply", "error"))
    def Start(self, iid, session, sender, reply, error):
        try:
            inst = self.load(iid)
            uid = self.caller(sender)
            self.check_owner(inst, uid)
            session = _s(session)
            pid = int(self.dbus_info.GetConnectionUnixProcessID(sender))
            if uid != 0 and str(pid) != session.get("pid"):
                raise Error("invalid session pid", "AccessDenied")
            if uid == 0 and inst.owner_uid != 0:
                uid = inst.owner_uid  # root starting a user's instance: use the owner's sockets
            if self.transient.get(inst.id) in ("STARTING", "STOPPING", "CLONING", "DELETING"):
                raise Error("instance #{} is busy ({})".format(inst.id, self.transient[inst.id]), "Busy")
        except Error as e:
            return error(e)
        except (ValueError, dbus.DBusException) as e:
            return error(Error(e))
        self.run_async(inst.id, lambda: self._start(inst.id, uid, session, str(sender)), reply, error)

    @dbus.service.method(paths.DBUS_IFACE, in_signature="s", out_signature="",
                         sender_keyword="sender", async_callbacks=("reply", "error"))
    def Stop(self, iid, sender, reply, error):
        try:
            inst = self.load(iid)
            self.check_owner(inst, self.caller(sender))
        except Error as e:
            return error(e)
        self.run_async(inst.id, lambda: self._stop(inst.id), reply, error)

    @dbus.service.method(paths.DBUS_IFACE, in_signature="ss", out_signature="",
                         sender_keyword="sender", async_callbacks=("reply", "error"))
    def AuthorizeAdbKey(self, iid, key, sender, reply, error):
        """Trust the caller's adb public key in their running instance, like the emulator does:
        adb connects without Android's "Allow USB debugging?" prompt."""
        try:
            inst = self.load(iid)
            self.check_owner(inst, self.caller(sender))
            key = validate_adb_key(key)
        except ValueError as e:
            return error(Error(e, "InvalidArgs"))
        except Error as e:
            return error(e)

        def work():
            pid = container_pid(inst.id)
            if not pid:
                raise Error("instance #{} is not running".format(inst.id), "NotRunning")
            # Android's /data is writable from inside: don't follow its links (or hard links) to other files
            fd = open_in_container(pid, "data/misc/adb/adb_keys", os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o640)
            with os.fdopen(fd, "r+") as f:
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                    raise Error("instance #{}: adb_keys is not a plain file".format(inst.id))
                old = f.read()
                if key not in old.splitlines():
                    f.write(("" if not old or old.endswith("\n") else "\n") + key + "\n")
                os.fchown(fd, AID_SYSTEM, AID_SHELL)
                os.fchmod(fd, 0o640)
        self.run_async(inst.id, work, reply, error, lock=False)

    @dbus.service.method(paths.DBUS_IFACE, in_signature="s", out_signature="",
                         sender_keyword="sender", async_callbacks=("reply", "error"))
    def Freeze(self, iid, sender, reply, error):
        try:
            inst = self.load(iid)
            self.check_owner(inst, self.caller(sender))
        except Error as e:
            return error(e)
        self.run_async(inst.id, lambda: self._freeze(inst.id), reply, error)

    @dbus.service.method(paths.DBUS_IFACE, in_signature="s", out_signature="",
                         sender_keyword="sender", async_callbacks=("reply", "error"))
    def Unfreeze(self, iid, sender, reply, error):
        try:
            inst = self.load(iid)
            self.check_owner(inst, self.caller(sender))
        except Error as e:
            return error(e)
        self.run_async(inst.id, lambda: self._unfreeze(inst.id), reply, error)

    @dbus.service.method(paths.DBUS_IFACE, in_signature="s", out_signature="", sender_keyword="sender")
    def ReportClose(self, iid, sender):
        inst = self.load(iid)
        self.check_owner(inst, self.caller(sender))
        self.last_close[inst.id] = time.time()
        action = inst.get("close_action")
        log.info("%s: window closed, close_action=%s", inst.id, action)
        self.apply_action(inst.id, action)

    @dbus.service.method(paths.DBUS_IFACE, in_signature="shs", out_signature="s",
                         sender_keyword="sender", async_callbacks=("reply", "error"))
    def InstallApk(self, iid, fd, filename, sender, reply, error):
        try:
            inst = self.load(iid)
            self.check_owner(inst, self.caller(sender))
            raw = _regular_fd(fd, write=False)
        except Error as e:
            return error(e)

        def work():
            st = lxc_state(inst.id)
            if st == "FROZEN":
                container.unfreeze(inst)
            elif st != "RUNNING":
                os.close(raw)
                raise Error("instance #{} is not running".format(inst.id), "NotRunning")
            return storage.install_apk(inst, raw, str(filename))
        self.run_async(inst.id, work, reply, error)

    @dbus.service.method(paths.DBUS_IFACE, in_signature="su", out_signature="", sender_keyword="sender")
    def SendKey(self, iid, code, sender):
        """Press and release an evdev key in the instance (e.g. 580 = Recents)."""
        inst = self.load(iid)
        self.check_owner(inst, self.caller(sender))
        if not 1 <= int(code) <= 767:
            raise Error("invalid key code {}".format(code), "InvalidArgs")
        try:
            self.send_key(inst.id, int(code))
        except OSError as e:
            raise Error("could not send key: {}".format(e))

    @dbus.service.method(paths.DBUS_IFACE, in_signature="sh", out_signature="t",
                         sender_keyword="sender", async_callbacks=("reply", "error"))
    def Screenshot(self, iid, fd, sender, reply, error):
        """Write a PNG of the instance's screen into the passed file descriptor."""
        try:
            inst = self.load(iid)
            self.check_owner(inst, self.caller(sender))
            raw = _regular_fd(fd, write=True)
        except Error as e:
            return error(e)
        st = lxc_state(inst.id)
        if st not in ACTIVE:
            os.close(raw)
            return error(Error("instance #{} is not running".format(inst.id), "NotRunning"))

        def work():
            if lxc_state(inst.id) == "FROZEN":       # idle-paused: wake it up for the capture
                with self.locks[inst.id]:
                    container.unfreeze(inst)
                GLib.idle_add(self._emit_state, inst.id)
            return dbus.UInt64(storage.screenshot(inst, raw))
        self.run_async(inst.id, work, reply, error, lock=False)

    @dbus.service.method(paths.DBUS_IFACE, in_signature="s", out_signature="s",
                         sender_keyword="sender", async_callbacks=("reply", "error"))
    def GetGsfId(self, iid, sender, reply, error):
        try:
            inst = self.load(iid)
            self.check_owner(inst, self.caller(sender))
        except Error as e:
            return error(e)
        self.run_async(inst.id, lambda: storage.gsf_id(inst), reply, error, lock=False)

    @dbus.service.method(paths.DBUS_IFACE, in_signature="s", out_signature="a{ss}",
                         sender_keyword="sender", async_callbacks=("reply", "error"))
    def PrepareGpu(self, iid, sender, reply, error):
        """How the instance renders at its next start, called by its session before Start:
        {"mode": see gpu.mode, "renderer": for nvidia, the directory of the renderer the session
        runs, "cpu_buffers": when the desktop can't import Android's buffers as dmabufs, how the
        session's proxy shows them: "shared" (CPU-rendered ones, mapped where they are) or the
        render node to copy them through (another GPU's under a desktop on NVIDIA)}. NVIDIA's
        binaries are downloaded on first use."""
        try:
            inst = self.load(iid)
            self.check_owner(inst, self.caller(sender))
        except Error as e:
            return error(e)

        def prepare():
            mode, node = container.render_for(inst)
            if mode != "nvidia":
                cpu = "shared" if mode == "vkms" else node if mode == "gpu" and gpu.nvidia_display() else ""
                return {"mode": mode, "renderer": "", "cpu_buffers": cpu or ""}
            nvidia.guest_layers(catalog.get(container.android_of(inst), "nvidia"))
            return {"mode": mode, "renderer": nvidia.host_dir(), "cpu_buffers": ""}
        self.run_async(inst.id, prepare, reply, error, lock=False)

    @dbus.service.method(paths.DBUS_IFACE, in_signature="", out_signature="s",
                         sender_keyword="sender", async_callbacks=("reply", "error"))
    def UpdateImages(self, sender, reply, error):
        """Fetch newer builds of the installed Android versions (and sync stock's); returns
        the versions updated, comma-separated."""
        self.run_async("__images__", lambda: ",".join(images.update(self.images_in_use())), reply, error)

    @dbus.service.method(paths.DBUS_IFACE, in_signature="", out_signature="aa{ss}")
    def Images(self):
        """The image sets in the store, with the instances using them."""
        users = collections.defaultdict(list)
        for i in self.all_instances():
            users[i.image_id].append(i.id)
        out = []
        for sid in images.available():
            d = dict(images.read_cfg(sid), id=sid, current="true" if sid == images.current_id() else "false",
                     used_by=",".join(users.get(sid, [])))
            d["size"] = str(sum(os.path.getsize(os.path.join(images.image_dir(sid), f))
                                for f in ("system.img", "vendor.img")))
            out.append(d)
        return out

    @dbus.service.signal(paths.DBUS_IFACE, signature="ss")
    def StateChanged(self, iid, state):
        pass

    @dbus.service.signal(paths.DBUS_IFACE, signature="s")
    def InstanceAdded(self, iid):
        pass

    @dbus.service.signal(paths.DBUS_IFACE, signature="s")
    def InstanceRemoved(self, iid):
        pass

    @dbus.service.signal(paths.DBUS_IFACE, signature="s")
    def ConfigChanged(self, iid):
        pass

    # -- background: state watcher, startup reconciliation ---------------------
    def watch_states(self):
        def loop():
            ticks = 0
            while True:
                time.sleep(3)
                ticks += 1
                if ticks % 10 == 1:   # every ~30 s
                    try:
                        if images.stock_is_newer() and not images.stock_busy():
                            log.info("stock Waydroid images changed, syncing the image store")
                            images.ensure_synced(self.images_in_use())
                            log.info("image store now at %s", images.current_id())
                    except Exception as e:  # noqa: BLE001
                        log.warning("automatic image sync failed: %s", e)
                for iid in list_ids():
                    if iid in self.transient or iid in self.stopping_by_us:
                        continue
                    st = lxc_state(iid)
                    if self.known.get(iid) != st:
                        GLib.idle_add(self._on_observed, iid, st)
        threading.Thread(target=loop, daemon=True, name="state-watch").start()

    def _on_observed(self, iid, st):
        prev = self.known.get(iid)
        self.known[iid] = st
        if st == "STOPPED" and prev in ACTIVE and iid not in self.transient:
            log.info("%s: container stopped on its own, cleaning up", iid)
            self.run_async(iid, lambda: self._stop(iid), lambda *a: None, lambda e: log.error("%s", e))
        self.StateChanged(iid, st)
        return False

    def reconcile(self):
        try:
            a = stock.make_args(paths.STATE_DIR, paths.STATE_DIR + "/none.cfg")
            container.binder.ensure_binderfs(a)
        except Exception as e:  # noqa: BLE001
            log.warning("binder setup: %s", e)
        active = False
        for inst in self.all_instances():
            st = lxc_state(inst.id)
            self.known[inst.id] = st
            if st in ACTIVE:
                active = True
                log.info("%s: adopting running container", inst.id)
                self.start_helper(inst)
            else:
                container.cleanup(inst)
        if active:
            try:
                self.net.ensure_up(self.hosts())
            except Exception as e:  # noqa: BLE001
                log.error("network: %s", e)
        else:
            self.net.ensure_down()
        self.refresh_names()


def main():
    logging.basicConfig(level=logging.DEBUG if os.environ.get("WAYDROID_MANAGER_DEBUG") else logging.INFO,
                        format="%(levelname)s %(message)s", stream=sys.stderr)
    if os.geteuid() != 0:
        print("waydroid-manager-daemon must run as root", file=sys.stderr)
        return 1
    os.umask(0o022)
    for d in (paths.STATE_DIR, paths.INSTANCES_DIR, paths.LXC_PATH, paths.IMAGES_DIR, paths.RUN_DIR):
        os.makedirs(d, mode=0o755, exist_ok=True)
    w = stock.check_version()
    if w:
        log.warning(w)
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    dbus.mainloop.glib.threads_init()
    bus = dbus.SystemBus()
    mgr = Manager(bus)
    mgr.reconcile()
    try:
        name = dbus.service.BusName(paths.DBUS_NAME, bus, do_not_queue=True)  # noqa: F841
    except dbus.exceptions.NameExistsException:
        log.error("another waydroid-manager daemon is running")
        return 1
    mgr.watch_states()
    loop = GLib.MainLoop()
    import signal
    from ..glibcompat import signal_add
    signal_add(signal.SIGTERM, loop.quit)
    signal_add(signal.SIGINT, loop.quit)
    log.info("waydroid-manager daemon %s ready (stock Waydroid %s)", __version__, stock.version())
    loop.run()
    for iid in list(mgr.helpers):
        mgr.stop_helper(iid)
    # Containers and dnsmasq keep running; a restarted daemon adopts them.
    return 0


if __name__ == "__main__":
    sys.exit(main())
