# SPDX-License-Identifier: GPL-3.0-or-later
"""Pick an APK for the instance window's Install APK button.

Run by the instance session; prints the chosen path on stdout, nothing if cancelled.
"""
import argparse
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from .confirm import APP_ID  # noqa: E402


def apk_dialog(title):
    """File dialog showing only .apk files (also used by the manager's Install APK…)."""
    f = Gtk.FileFilter()
    f.set_name("Android packages")
    f.add_pattern("*.apk")
    filters = Gio.ListStore.new(Gtk.FileFilter)
    filters.append(f)
    return Gtk.FileDialog(title=title, filters=filters)


def main(argv=None):
    p = argparse.ArgumentParser(prog="waydroid-multi-pickapk")
    p.add_argument("--name", default="this instance")
    o = p.parse_args(argv)
    chosen = [""]
    app = Adw.Application(application_id=APP_ID, flags=Gio.ApplicationFlags.NON_UNIQUE)

    def activate(app):
        app.hold()

        def picked(dialog, res):
            try:
                chosen[0] = dialog.open_finish(res).get_path() or ""
            except GLib.Error:
                pass                   # cancelled
            app.quit()
        apk_dialog("Install APK in “{}”".format(o.name)).open(None, None, picked)

    app.connect("activate", activate)
    app.run(sys.argv[:1])
    print(chosen[0], flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
