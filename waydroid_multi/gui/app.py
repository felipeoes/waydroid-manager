# SPDX-License-Identifier: GPL-3.0-or-later
"""GTK4/libadwaita instance manager."""
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
import dbus.mainloop.glib  # noqa: E402
from gi.repository import Adw, Gio, Gtk  # noqa: E402

from .. import HOMEPAGE, __version__  # noqa: E402

APP_ID = "io.github.waydroidmulti"


class Application(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.win = None

    def do_activate(self):
        if self.win is None:
            from .window import MainWindow
            self.win = MainWindow(self)
            for name, cb in (("start-all", self.win.start_all), ("stop-all", self.win.stop_all),
                             ("sync", self.win.sync_images), ("about", self.about)):
                a = Gio.SimpleAction.new(name, None)
                a.connect("activate", lambda _a, _p, cb=cb: cb())
                self.add_action(a)
            self.set_accels_for_action("app.quit", ["<Ctrl>q"])
            q = Gio.SimpleAction.new("quit", None)
            q.connect("activate", lambda *_: self.quit())
            self.add_action(q)
        self.win.present()

    def about(self):
        dlg = Adw.AboutDialog(application_name="Waydroid Multi-Instance Manager", application_icon="waydroid",
                              version=__version__, license_type=Gtk.License.GPL_3_0,
                              comments="Run several Waydroid instances side by side.")
        if HOMEPAGE:
            dlg.set_website(HOMEPAGE)
        dlg.present(self.win)


def main():
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    return Application().run(sys.argv[:1])


if __name__ == "__main__":
    sys.exit(main())
