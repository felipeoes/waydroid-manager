# SPDX-License-Identifier: GPL-3.0-or-later
"""Non-blocking access to the daemon and the CLI for the GUI.

Instance management goes over D-Bus with async calls. Anything that makes
binder calls (start/show/app install) runs the CLI in a subprocess, because
python3-gbinder blocks the whole process while it waits.
"""
import os
import shutil
import sys

import dbus
from gi.repository import Gio, GLib

from .. import paths


def cli_argv(*args):
    exe = shutil.which("waydroid-multi")
    if exe:
        return [exe] + list(args)
    return [sys.executable, "-m", "waydroid_multi"] + list(args)


def _clean(e):
    msg = e.get_dbus_message() if isinstance(e, dbus.DBusException) else str(e)
    return (msg or str(e)).strip().splitlines()[-1]


class Backend:
    def __init__(self, on_change):
        self.on_change = on_change
        self.bus = dbus.SystemBus()
        self.iface = None
        self._refresh_pending = False
        for sig in ("StateChanged", "InstanceAdded", "InstanceRemoved", "ConfigChanged"):
            self.bus.add_signal_receiver(self._on_signal, signal_name=sig, dbus_interface=paths.DBUS_IFACE,
                                         bus_name=paths.DBUS_NAME, path=paths.DBUS_PATH)
        self.bus.watch_name_owner(paths.DBUS_NAME, lambda owner: self._on_signal())

    def _proxy(self):
        if self.iface is None:
            obj = self.bus.get_object(paths.DBUS_NAME, paths.DBUS_PATH)
            self.iface = dbus.Interface(obj, paths.DBUS_IFACE)
        return self.iface

    def _on_signal(self, *args):
        if not self._refresh_pending:
            self._refresh_pending = True
            GLib.timeout_add(150, self._fire_refresh)

    def _fire_refresh(self):
        self._refresh_pending = False
        self.on_change()
        return False

    def call(self, method, *args, ok=None, fail=None, timeout=3600):
        """Async D-Bus call; ok(result...) / fail(message)."""
        def reply(*res):
            if ok:
                ok(*[Backend.plain(r) for r in res])

        def error(e):
            self.iface = None if "ServiceUnknown" in str(getattr(e, "get_dbus_name", lambda: "")()) else self.iface
            if fail:
                fail(_clean(e))
        args = [dbus.Dictionary(a, signature="ss") if isinstance(a, dict) else a for a in args]
        try:
            getattr(self._proxy(), method)(*args, reply_handler=reply, error_handler=error, timeout=timeout)
        except dbus.DBusException as e:
            self.iface = None
            if fail:
                fail(_clean(e))

    @staticmethod
    def plain(v):
        if isinstance(v, (dbus.Array, list)):
            return [Backend.plain(x) for x in v]
        if isinstance(v, (dbus.Dictionary, dict)):
            return {str(k): str(x) for k, x in v.items()}
        return str(v)

    def run_cli(self, args, done=None):
        """Run 'waydroid-multi <args>' without blocking; done(ok, output)."""
        launcher = Gio.SubprocessLauncher.new(Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_MERGE)
        launcher.setenv("PYTHONPATH", os.path.dirname(paths.PKG_DIR), True)
        try:
            proc = launcher.spawnv(cli_argv(*args))
        except GLib.Error as e:
            if done:
                done(False, e.message)
            return

        def finished(p, res):
            try:
                _, out, _ = p.communicate_utf8_finish(res)
            except GLib.Error as e:
                out = e.message
            if done:
                done(p.get_successful(), (out or "").strip())
        proc.communicate_utf8_async(None, None, finished)
