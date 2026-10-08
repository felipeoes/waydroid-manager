# SPDX-License-Identifier: GPL-3.0-or-later
"""Settings windows and full restarts from the instance toolbar, in one background process.

The first click starts it, in a unit of its own (client.open_settings). Later clicks reach it over
D-Bus, so their window opens at once instead of after a new GTK process's ~0.3 s start. It quits
once no instance is running and none of its windows is open.
"""
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
import dbus.mainloop.glib  # noqa: E402
from gi.repository import Adw, Gio, GLib  # noqa: E402

from .backend import Backend  # noqa: E402
from .confirm import APP_ID as MANAGER_ID  # noqa: E402
from .dialogs import InstanceDialog, restart_dialog, save_settings  # noqa: E402
from .window import ACTIVE  # noqa: E402

APP_ID = "io.github.waydroidmanager.Settings"     # client.SETTINGS_NAME


class SettingsApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.windows = {}       # instance id -> its InstanceDialog; None while its info is on the way
        self.busy = 0           # saves, restarts and error messages under way
        self.restarting = set()
        self.add_main_option("restart", 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             "Restart the instance", None)

    def do_startup(self):
        Adw.Application.do_startup(self)
        self.hold()             # runs on without a window, until quit_if_idle
        self.backend = Backend(self.quit_if_idle)       # called when an instance starts or stops
        for name, cb in (("open", self.open), ("restart", self.restart)):
            a = Gio.SimpleAction.new(name, GLib.VariantType.new("s"))
            a.connect("activate", lambda _a, iid, cb=cb: cb(iid.get_string()))
            self.add_action(a)

    def do_command_line(self, cmd):
        action = self.restart if cmd.get_options_dict().contains("restart") else self.open
        for iid in cmd.get_arguments()[1:]:
            action(iid)
        return 0

    def open(self, iid):
        if iid in self.windows:
            if self.windows[iid]:
                self.windows[iid].present(None)         # already open: raise it
            return
        self.windows[iid] = None

        def failed(msg):
            self.windows.pop(iid, None)
            self.alert("Could not open the settings", msg)
        self.backend.call("Get", iid, ok=self.show, fail=failed, timeout=30)

    def show(self, info):
        iid = info["id"]
        dlg = InstanceDialog("edit", lambda _iid, values: self.save(info, values), info=info)
        self.windows[iid] = dlg
        dlg.connect("closed", lambda *_: (self.windows.pop(iid, None), self.quit_if_idle()))
        dlg.present(None)          # no parent: shown as its own window

    def quit_if_idle(self):
        if self.windows or self.busy:
            return

        def listed(items):
            if not (self.windows or self.busy or any(i["state"] in ACTIVE for i in items)):
                self.quit()
        self.backend.call("List", ok=listed, fail=lambda _m: self.quit(), timeout=30)

    def done(self):
        """A save, restart or error message is over."""
        self.busy -= 1
        self.quit_if_idle()

    def alert(self, heading, msg, then=None):
        self.busy += 1
        lines = (msg or "").strip().splitlines()       # the CLI's error is its last line
        dlg = Adw.AlertDialog(heading=heading, body=lines[-1] if lines else "")
        dlg.add_response("close", "Close")
        dlg.connect("response", lambda *_: (then and then(), self.done()))
        dlg.present(None)

    def save(self, before, values):
        self.busy += 1
        save_settings(self.backend, before, values, lambda restart: self.saved(before["id"], restart),
                      lambda m: self.alert("Could not save the settings", m, self.done))

    def saved(self, iid, needs_restart):
        def got(info):          # its state now, and its new name
            if info["state"] in ACTIVE:
                restart_dialog(info["name"], lambda: (self.restart(iid), self.done()), self.done).present(None)
            else:
                self.done()
        if needs_restart:
            self.backend.call("Get", iid, ok=got, fail=lambda _m: self.done(), timeout=30)
        else:
            self.done()

    def restart(self, iid):
        if iid in self.restarting:
            return
        self.restarting.add(iid)
        self.busy += 1

        def done(ok, out):
            self.restarting.discard(iid)
            if not ok:
                self.alert("Could not restart #{}".format(iid), out)
            self.done()

        def got(info):
            if info["state"] not in ACTIVE:
                return done(False, "The instance is no longer running.")
            self.backend.restart(iid, done)
        self.backend.call("Get", iid, ok=got, fail=lambda msg: done(False, msg), timeout=30)


def main(argv=None):
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    # Its windows have no application of their own: the prgname is their app id, which gives them
    # the manager's icon in the dock
    GLib.set_prgname(MANAGER_ID)
    return SettingsApp().run(sys.argv[:1] + (sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    sys.exit(main())
