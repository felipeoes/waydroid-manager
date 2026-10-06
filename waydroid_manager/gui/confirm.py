# SPDX-License-Identifier: GPL-3.0-or-later
"""Ask before closing an instance window stops the instance.

Run by the instance session; prints its answer on stdout: "stop" or "cancel".
"""
import argparse
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib  # noqa: E402

# The manager's app id (NON_UNIQUE, so it never talks to a running manager)
APP_ID = "io.github.waydroidmanager"


def main(argv=None):
    p = argparse.ArgumentParser(prog="waydroid-manager-confirm")
    p.add_argument("--name", default="this instance")
    o = p.parse_args(argv)
    answer = ["cancel"]
    # A dialog shown without a parent has no application: the prgname is its app id, which gives it
    # the manager's icon in the dock
    GLib.set_prgname(APP_ID)
    app = Adw.Application(application_id=APP_ID, flags=Gio.ApplicationFlags.NON_UNIQUE)

    def activate(app):
        app.hold()
        dlg = Adw.AlertDialog(heading="Stop “{}”?".format(o.name),
                              body="Android will shut down and its open apps will close.")
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("stop", "Stop")
        dlg.set_response_appearance("stop", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_default_response("stop")
        dlg.set_close_response("cancel")

        def respond(_d, resp):
            answer[0] = resp
            app.quit()
        dlg.connect("response", respond)
        dlg.present(None)          # no parent: shown as its own small window

    app.connect("activate", activate)
    app.run(sys.argv[:1])
    print(answer[0], flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
