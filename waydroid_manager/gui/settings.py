# SPDX-License-Identifier: GPL-3.0-or-later
"""An instance's Settings in a window of their own, for the Settings button of its window.

Started by the instance session in a unit of its own (client.open_settings).
"""
import argparse
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
import dbus.mainloop.glib  # noqa: E402
from gi.repository import Adw, Gio  # noqa: E402

from .backend import Backend  # noqa: E402
from .confirm import APP_ID  # noqa: E402
from .dialogs import InstanceDialog, restart_dialog, save_settings  # noqa: E402
from .window import ACTIVE  # noqa: E402


def main(argv=None):
    p = argparse.ArgumentParser(prog="waydroid-manager-settings")
    p.add_argument("id")
    o = p.parse_args(argv)
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    app = Adw.Application(application_id=APP_ID, flags=Gio.ApplicationFlags.NON_UNIQUE)
    backend = Backend(lambda: None)
    saving = [False]

    def alert(heading, msg):
        lines = (msg or "").strip().splitlines()       # the CLI's error is its last line
        dlg = Adw.AlertDialog(heading=heading, body=lines[-1] if lines else "")
        dlg.add_response("close", "Close")
        dlg.connect("response", lambda *_: app.quit())
        dlg.present(None)

    def restart(info):
        def stopped(ok, out):
            if not ok:
                return alert("Could not stop “{}”".format(info["name"]), out)
            backend.run_cli(["start", o.id], lambda ok, out: app.quit() if ok else
                            alert("Could not start “{}”".format(info["name"]), out))
        backend.run_cli(["stop", o.id], stopped)

    def saved(needs_restart):
        def got(info):          # its state now, and its new name
            if info["state"] in ACTIVE:
                restart_dialog(info["name"], lambda: restart(info), app.quit).present(None)
            else:
                app.quit()
        if needs_restart:
            backend.call("Get", o.id, ok=got, fail=lambda _m: app.quit(), timeout=30)
        else:
            app.quit()

    def save(before, values):
        saving[0] = True
        save_settings(backend, before, values, saved, lambda m: alert("Could not save the settings", m))

    def got(info):
        dlg = InstanceDialog("edit", lambda _iid, values: save(info, values), info=info)
        dlg.connect("closed", lambda *_: saving[0] or app.quit())
        dlg.present(None)          # no parent: shown as its own window

    def activate(app):
        app.hold()
        backend.call("Get", o.id, ok=got, fail=lambda m: alert("Could not open the settings", m), timeout=30)

    app.connect("activate", activate)
    app.run(sys.argv[:1])
    return 0


if __name__ == "__main__":
    sys.exit(main())
