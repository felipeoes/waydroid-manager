# SPDX-License-Identifier: GPL-3.0-or-later
"""waydroid-multi root daemon: one process managing every instance.

D-Bus API on the system bus (io.github.waydroidmulti.Manager). Long
operations run in worker threads under a per-instance lock; results and
signals are delivered from the GLib main loop. The daemon never makes binder
calls itself (see hwhelper.py).
"""
import collections
import logging
import os
import pwd
import subprocess
import sys
import threading
import time

import dbus
import dbus.exceptions
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib

from .. import __version__, paths, stock
from ..instance import (Instance, SETTINGS, list_ids, validate_id, validate_prop,
                        validate_setting)
from ..registry import allocate_index
from . import container, images, storage
from .network import Network
from .util import log, lxc_state

ERR = "io.github.waydroidmulti.Error"
ACTIVE = ("RUNNING", "FROZEN")
POLKIT_CREATE = "io.github.waydroidmulti.create"


class Error(dbus.exceptions.DBusException):
    def __init__(self, msg, kind="Failed"):
        super().__init__(str(msg), name=ERR + "." + kind)


def _s(d):
    return {str(k): str(v) for k, v in d.items()}


class Manager(dbus.service.Object):
    def __init__(self, bus):
        super().__init__(bus, paths.DBUS_PATH)
        self.bus = bus
        self.net = Network()
        self.locks = collections.defaultdict(threading.Lock)
        self.transient = {}      # id -> STARTING/STOPPING/CLONING/DELETING
        self.known = {}          # id -> last observed state
        self.sessions = {}       # id -> dict(sender, pid, uid, watch, session)
        self.helpers = {}        # id -> Popen
        self.last_close = {}
        self.stopping_by_us = set()
        self.dbus_info = dbus.Interface(bus.get_object("org.freedesktop.DBus", "/org/freedesktop/DBus"),
                                        "org.freedesktop.DBus")

    # -- helpers -------------------------------------------------------------
    def caller(self, sender):
        return int(self.dbus_info.GetConnectionUnixUser(sender))

    def load(self, iid):
        try:
            return Instance.load(validate_id(str(iid)))
        except (FileNotFoundError, ValueError) as e:
            raise Error(e, "NotFound")

    def check_owner(self, inst, uid):
        if uid != 0 and uid != inst.owner_uid:
            raise Error("instance '{}' belongs to another user".format(inst.id), "AccessDenied")

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

    def polkit_check(self, sender, action):
        try:
            pid = int(self.dbus_info.GetConnectionUnixProcessID(sender))
            authority = dbus.Interface(self.bus.get_object("org.freedesktop.PolicyKit1",
                                                           "/org/freedesktop/PolicyKit1/Authority"),
                                       "org.freedesktop.PolicyKit1.Authority")
            with open("/proc/{}/stat".format(pid)) as f:
                start_time = int(f.read().rsplit(")", 1)[1].split()[19])
            subject = ("unix-process", {"pid": dbus.UInt32(pid, variant_level=1),
                                        "start-time": dbus.UInt64(start_time, variant_level=1)})
            ok, _, _ = authority.CheckAuthorization(subject, action, {"AllowUserInteraction": "true"},
                                                    dbus.UInt32(1), "", timeout=300)
            return bool(ok)
        except dbus.DBusException as e:
            if "ServiceUnknown" in (e.get_dbus_name() or ""):
                log.warning("polkit unavailable, allowing %s", action)
                return True
            if "No policy" in str(e) or "not registered" in str(e):
                log.warning("polkit action %s not installed, allowing", action)
                return True
            raise Error("authorization failed: {}".format(e), "AccessDenied")

    # -- hardware helper -----------------------------------------------------
    def start_helper(self, inst):
        self.stop_helper(inst.id)
        env = dict(os.environ)
        env["PYTHONPATH"] = os.path.dirname(paths.PKG_DIR) + os.pathsep + env.get("PYTHONPATH", "")
        p = subprocess.Popen([sys.executable, "-m", "waydroid_multi.daemon.hwhelper", inst.cfg_path],
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
                        "use 'waydroid upgrade' then 'waydroid-multi images sync'", iid)
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
                raise Error("instance '{}' is already running in another session".format(iid), "Busy")
            GLib.idle_add(self._attach_session, iid, uid, session, sender)
            if iid not in self.helpers:
                self.start_helper(inst)
            return
        self.set_transient(iid, "STARTING")
        try:
            container.start(inst, self.net, self.hosts(), session, uid)
            inst = Instance.load(iid)
            self.start_helper(inst)
            GLib.idle_add(self._attach_session, iid, uid, session, sender)
        finally:
            self.set_transient(iid, None)
        if inst.cfg["instance"].get("pending_id_reset") == "true":
            threading.Thread(target=self._finish_id_reset, args=(iid,), daemon=True).start()

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

    def _stop(self, iid):
        inst = Instance.load(iid)
        self.set_transient(iid, "STOPPING")
        self.stopping_by_us.add(iid)
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
        container.stop(inst)
        session = dict(s["session"])
        session["background_start"] = "false"
        container.start(inst, self.net, self.hosts(), session, s["uid"])
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

    def _create(self, iid, uid, opts):
        if os.path.exists(paths.instance_dir(iid)):
            raise Error("instance '{}' already exists".format(iid), "Exists")
        clone_from = opts.pop("clone_from", "")
        reset_ids = opts.pop("reset_ids", "true") != "false"
        src_data = None
        if clone_from:
            src_data = self._clone_source(clone_from, uid)
        settings = {}
        props = {}
        for k, v in opts.items():
            if k.startswith("prop:"):
                props[k[5:]] = validate_prop(k[5:], v)
            else:
                settings[k] = validate_setting(k, v)
        used = [i.index for i in self.all_instances()]
        index = allocate_index(used)
        image_id = images.current_id() or images.sync()
        w = dict(stock.load_stock_cfg()["waydroid"])
        for k in ("binder", "vndbinder", "hwbinder", "images_path"):
            w.pop(k, None)
        inst = Instance.new(iid, index, uid, image_id, w, props)
        inst.cfg["waydroid"]["images_path"] = images.image_dir(image_id)
        for k, v in settings.items():
            inst.cfg["instance"][k] = v
        inst.save()
        try:
            container.ensure_dirs(inst)
            if src_data:
                self.set_transient(iid, "CLONING")
                storage.copy_data(src_data, inst.data_dir)
                pw = pwd.getpwuid(uid)
                os.chown(inst.data_dir, uid, pw.pw_gid)
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
        self.net.reload_hosts(self.hosts())
        GLib.idle_add(lambda: (self.InstanceAdded(iid), False)[1])

    def _clone_source(self, src, uid):
        if src == "default":
            pw = pwd.getpwuid(uid)
            data = os.path.join(pw.pw_dir, ".local/share/waydroid/data")
            r = subprocess.run(["lxc-info", "-P", paths.STOCK_WORK + "/lxc", "-n", "waydroid", "-sH"],
                               capture_output=True, text=True)
            if r.stdout.strip() not in ("", "STOPPED"):
                raise Error("stop the stock Waydroid session first ('waydroid session stop')", "Busy")
            if not os.path.isdir(data) or os.path.islink(data) or os.stat(data).st_uid != uid:
                raise Error("no stock Waydroid data found at {}".format(data), "NotFound")
            return data
        sinst = self.load(src)
        self.check_owner(sinst, uid)
        if self.state(sinst.id) != "STOPPED":
            raise Error("stop instance '{}' before cloning it".format(src), "Busy")
        return sinst.data_dir

    def _delete(self, iid):
        inst = Instance.load(iid)
        if lxc_state(iid) != "STOPPED":
            self._stop(iid)
        self.set_transient(iid, "DELETING")
        try:
            storage.delete_instance_files(inst)
        finally:
            self.transient.pop(iid, None)
        self.net.reload_hosts(self.hosts())
        GLib.idle_add(lambda: (self.InstanceRemoved(iid), False)[1])

    # -- D-Bus API -------------------------------------------------------------
    @dbus.service.method(paths.DBUS_IFACE, in_signature="", out_signature="a{ss}")
    def GetInfo(self):
        warn = stock.check_version() or ""
        if images.stock_is_newer():
            warn = (warn + "; " if warn else "") + "stock images are newer than the image store: run 'waydroid-multi images sync'"
        return {"version": __version__, "stock_version": stock.version(), "warnings": warn,
                "subnet": str(self.net.cfg.network), "bridge": self.net.cfg.bridge,
                "image": images.current_id(), "stock_image": images.stock_image_id()}

    @dbus.service.method(paths.DBUS_IFACE, in_signature="", out_signature="aa{ss}")
    def List(self):
        return [self.info(i) for i in self.all_instances()]

    @dbus.service.method(paths.DBUS_IFACE, in_signature="s", out_signature="a{ss}")
    def Get(self, iid):
        return self.info(self.load(iid))

    @dbus.service.method(paths.DBUS_IFACE, in_signature="sa{ss}", out_signature="",
                         sender_keyword="sender", async_callbacks=("reply", "error"))
    def Create(self, iid, opts, sender, reply, error):
        uid = self.caller(sender)
        try:
            iid = validate_id(str(iid))
        except ValueError as e:
            return error(Error(e, "InvalidArgs"))
        if uid != 0 and not self.polkit_check(sender, POLKIT_CREATE):
            return error(Error("not authorized to create instances", "AccessDenied"))
        self.run_async(iid, lambda: self._create(iid, uid, _s(opts)), reply, error)

    @dbus.service.method(paths.DBUS_IFACE, in_signature="s", out_signature="",
                         sender_keyword="sender", async_callbacks=("reply", "error"))
    def Delete(self, iid, sender, reply, error):
        try:
            inst = self.load(iid)
            self.check_owner(inst, self.caller(sender))
        except Error as e:
            return error(e)
        self.run_async(inst.id, lambda: self._delete(inst.id), reply, error)

    @dbus.service.method(paths.DBUS_IFACE, in_signature="sa{ss}", out_signature="", sender_keyword="sender")
    def SetConfig(self, iid, values, sender):
        inst = self.load(iid)
        self.check_owner(inst, self.caller(sender))
        with self.locks[inst.id]:
            inst = Instance.load(inst.id)
            try:
                for k, v in _s(values).items():
                    if k.startswith("prop:"):
                        key = k[5:]
                        if v == "":
                            inst.cfg["properties"].pop(key, None)
                        else:
                            inst.cfg["properties"][key] = validate_prop(key, v)
                    else:
                        inst.set(k, v)
            except ValueError as e:
                raise Error(e, "InvalidArgs")
            inst.save()
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
                raise Error("instance '{}' is busy ({})".format(inst.id, self.transient[inst.id]), "Busy")
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
            raw = fd.take()
        except Error as e:
            return error(e)

        def work():
            st = lxc_state(inst.id)
            if st == "FROZEN":
                container.unfreeze(inst)
            elif st != "RUNNING":
                os.close(raw)
                raise Error("instance '{}' is not running".format(inst.id), "NotRunning")
            return storage.install_apk(inst, raw, str(filename))
        self.run_async(inst.id, work, reply, error)

    @dbus.service.method(paths.DBUS_IFACE, in_signature="s", out_signature="s",
                         sender_keyword="sender", async_callbacks=("reply", "error"))
    def GetGsfId(self, iid, sender, reply, error):
        try:
            inst = self.load(iid)
            self.check_owner(inst, self.caller(sender))
        except Error as e:
            return error(e)
        self.run_async(inst.id, lambda: storage.gsf_id(inst.id) if lxc_state(inst.id) == "RUNNING" else "",
                       reply, error, lock=False)

    @dbus.service.method(paths.DBUS_IFACE, in_signature="", out_signature="s",
                         sender_keyword="sender", async_callbacks=("reply", "error"))
    def SyncImages(self, sender, reply, error):
        uid = self.caller(sender)
        if uid != 0 and not self.polkit_check(sender, POLKIT_CREATE):
            return error(Error("not authorized", "AccessDenied"))

        def work():
            iid = images.sync()
            images.gc(i.image_id for i in self.all_instances())
            return iid
        self.run_async("__images__", work, reply, error)

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
            while True:
                time.sleep(3)
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


def main():
    logging.basicConfig(level=logging.DEBUG if os.environ.get("WAYDROID_MULTI_DEBUG") else logging.INFO,
                        format="%(levelname)s %(message)s", stream=sys.stderr)
    if os.geteuid() != 0:
        print("waydroid-multi-daemon must run as root", file=sys.stderr)
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
        log.error("another waydroid-multi daemon is running")
        return 1
    mgr.watch_states()
    loop = GLib.MainLoop()
    import signal
    from ..glibcompat import signal_add
    signal_add(signal.SIGTERM, loop.quit)
    signal_add(signal.SIGINT, loop.quit)
    log.info("waydroid-multi daemon %s ready (stock Waydroid %s)", __version__, stock.version())
    loop.run()
    for iid in list(mgr.helpers):
        mgr.stop_helper(iid)
    # Containers and dnsmasq keep running; a restarted daemon adopts them.
    return 0


if __name__ == "__main__":
    sys.exit(main())
