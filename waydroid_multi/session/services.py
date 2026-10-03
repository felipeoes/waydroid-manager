# SPDX-License-Identifier: GPL-3.0-or-later
"""Host-side binder services for one instance, run inside its session process.

Equivalent to stock services/{clipboard,notification,user}_manager.py, but
registered on the session's main loop (no threads, no module globals) and
namespaced per instance: notifications are labelled with the instance name and
desktop entries never touch the stock instance's waydroid.*.desktop files.

Registration uses add_service_sync() (the async variant is broken in
python3-gbinder); only this instance's session process can be affected by a
stalled call.
"""
import logging
import shutil
import subprocess

import dbus

from .. import stock

log = logging.getLogger("waydroid-multi.session")


class BinderService:
    def __init__(self, sm, name, iface, handler):
        self.sm = sm
        self.name = name
        self.handler = handler
        self.obj = sm.new_local_object(iface, self._handle)
        self.hid = sm.add_presence_handler(self._presence)
        self._presence()

    def _presence(self):
        if self.sm.is_present():
            status = self.sm.add_service_sync(self.name, self.obj)
            if status:
                log.error("failed to register %s: %s", self.name, status)
            else:
                log.debug("registered %s", self.name)

    def _handle(self, req, code, flags):
        reader = req.init_reader()
        reply = self.obj.new_reply()
        try:
            ok = self.handler(code, reader, reply)
        except Exception:  # noqa: BLE001
            log.exception("%s: transaction %s failed", self.name, code)
            ok = False
        return reply, (0 if ok else -99999)

    def close(self):
        try:
            self.sm.remove_handler(self.hid)
            self.obj.drop()
        except Exception:  # noqa: BLE001
            pass


# -- clipboard ------------------------------------------------------------------

def clipboard_service(sm):
    if not (shutil.which("wl-copy") and shutil.which("wl-paste")):
        log.info("wl-clipboard not installed: clipboard sharing disabled")
        return None
    I = stock.tools().interfaces.IClipboard

    def handler(code, reader, reply):
        if code == I.TRANSACTION_sendClipboardData:
            text = reader.read_string16() or ""
            try:
                subprocess.run(["wl-copy", "--", text], timeout=2, check=False,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except subprocess.TimeoutExpired:
                pass
            reply.append_int32(0)
            return True
        if code == I.TRANSACTION_getClipboardData:
            try:
                r = subprocess.run(["wl-paste", "--no-newline", "--type", "text/plain;charset=utf-8"],
                                   capture_output=True, timeout=2)
                text = r.stdout.decode("utf-8", "replace") if r.returncode == 0 else ""
            except subprocess.TimeoutExpired:
                text = ""
            reply.append_int32(0)
            reply.append_string16(text)
            return True
        return False

    return BinderService(sm, I.SERVICE_NAME, I.INTERFACE, handler)


# -- notifications ----------------------------------------------------------------

class Notifications:
    def __init__(self, sm, inst_id, inst_name):
        t = stock.tools()
        self.I = t.interfaces.INotifications
        self.Callback = t.interfaces.INotificationCallback.INotificationCallback
        self.inst_id = inst_id
        self.inst_name = inst_name
        self.listeners = []
        self.pending_tokens = {}
        self.signals = []
        self.service = None
        try:
            self.proxy = dbus.Interface(dbus.SessionBus().get_object(
                "org.freedesktop.Notifications", "/org/freedesktop/Notifications"),
                "org.freedesktop.Notifications")
        except dbus.DBusException as e:
            log.info("no notification server: %s", e)
            return
        self.signals.append(self.proxy.connect_to_signal("ActivationToken", self._on_token))
        self.signals.append(self.proxy.connect_to_signal("ActionInvoked", self._on_action))
        self.service = BinderService(sm, self.I.SERVICE_NAME, self.I.INTERFACE, self._handle)

    def _on_token(self, nid, token):
        self.pending_tokens[int(nid)] = str(token)

    def _on_action(self, nid, action_id):
        token = self.pending_tokens.pop(int(nid), "")
        for listener in list(self.listeners):
            listener.onActionInvoked(int(nid), str(action_id), token)

    def _handle(self, code, reader, reply):
        I = self.I
        if code == I.TRANSACTION_registerListener:
            listener = self.Callback(reader.read_object())
            listener.addDeathHandler(lambda l: self.listeners.remove(l) if l in self.listeners else None)
            self.listeners.append(listener)
            reply.append_int32(0)
            return True
        if code == I.TRANSACTION_notify:
            # Wire format identical to stock tools/interfaces/INotifications.py
            _, replaces_id = reader.read_int32()
            app_name = reader.read_string16()
            package_name = reader.read_string16()
            summary = reader.read_string16()
            body = reader.read_string16()
            actions = []
            _, n = reader.read_int32()
            for _ in range(n):
                _, flag = reader.read_int32()
                if flag != I.kNullParcelableFlag:
                    _, _size = reader.read_int32()
                    actions.append((reader.read_string16(), reader.read_string16()))
            image = None
            _, flag = reader.read_int32()
            if flag != I.kNullParcelableFlag:
                _, _size = reader.read_int32()
                image = (reader.read_int32()[1], reader.read_int32()[1], reader.read_int32()[1],
                         reader.read_bool()[1], reader.read_byte_array())
            category = reader.read_string16()
            _, suppress_sound = reader.read_bool()
            _, expire_timeout = reader.read_int32()
            _, resident = reader.read_bool()
            _, transient = reader.read_bool()
            _, urgency = reader.read_byte()
            nid = self._notify(replaces_id, app_name, package_name, summary, body, actions, image,
                               category, suppress_sound, expire_timeout, resident, transient, urgency)
            reply.append_int32(0)
            reply.append_int32(nid)
            return True
        if code == I.TRANSACTION_closeNotification:
            _, nid = reader.read_int32()
            try:
                self.proxy.CloseNotification(nid)
            except dbus.DBusException as e:
                log.warning("close notification: %s", e)
            reply.append_int32(0)
            return True
        return False

    def _notify(self, replaces_id, app_name, package_name, summary, body, actions, image, category,
                suppress_sound, expire_timeout, resident, transient, urgency):
        hints = {
            "desktop-entry": "waydroid-multi.{}".format(self.inst_id),
            "resident": dbus.Boolean(resident),
            "transient": dbus.Boolean(transient),
            "urgency": dbus.Byte(urgency),
            "suppress-sound": dbus.Boolean(suppress_sound),
        }
        if category:
            hints["category"] = category
        if image:
            w, h, stride, alpha, data = image
            hints["image-data"] = dbus.Struct([w, h, stride, alpha, 8, 4 if alpha else 3,
                                               dbus.Array(data, signature="y")])
        flat = [s for a in actions for s in a]
        label = "{} ({})".format(app_name or package_name, self.inst_name)
        try:
            return int(self.proxy.Notify(label, replaces_id, "", summary, body, flat, hints, expire_timeout))
        except dbus.DBusException as e:
            log.warning("failed to post notification: %s", e)
            return self.I.ID_NONE

    def close(self):
        for s in self.signals:
            s.remove()
        if self.service:
            self.service.close()


# -- user monitor (boot readiness, app changes) ---------------------------------------

def user_monitor_service(sm, on_unlocked, on_package):
    I = stock.tools().interfaces.IUserMonitor

    def handler(code, reader, reply):
        if code == I.TRANSACTION_userUnlocked:
            _, uid = reader.read_int32()
            on_unlocked(uid)
            reply.append_int32(0)
            return True
        if code == I.TRANSACTION_packageStateChanged:
            _, mode = reader.read_int32()
            pkg = reader.read_string16()
            _, uid = reader.read_int32()
            on_package(mode, pkg, uid)
            reply.append_int32(0)
            return True
        return False

    return BinderService(sm, I.SERVICE_NAME, I.INTERFACE, handler)
