# SPDX-License-Identifier: GPL-3.0-or-later
"""Offer to let devices through the host's own firewall, which drops their network.

Run by the instance session; prints its answer on stdout: "allow", "later" or "never".
"""
import argparse
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib  # noqa: E402

from .adwcompat import present_alone  # noqa: E402
from .confirm import APP_ID  # noqa: E402


def main(argv=None):
    p = argparse.ArgumentParser(prog="waydroid-manager-firewall")
    p.add_argument("--name", default="this instance")
    p.add_argument("--blocked", default="", help="the blocking tables, as GetInfo's firewall")
    o = p.parse_args(argv)
    answer = ["later"]
    GLib.set_prgname(APP_ID)    # the manager's icon in the dock, as in confirm.py
    app = Adw.Application(application_id=APP_ID, flags=Gio.ApplicationFlags.NON_UNIQUE)

    def activate(app):
        app.hold()
        dlg = Adw.AlertDialog(
            heading="“{}” has no internet".format(o.name),
            body="Your firewall drops the network traffic of Waydroid Manager's devices ({}). Waydroid "
                 "Manager can add rules that let them through while devices run; your firewall's own "
                 "configuration stays as it is.".format(o.blocked.replace("\n", "; ")))
        dlg.add_response("never", "Don't Ask Again")
        dlg.add_response("later", "Not Now")
        dlg.add_response("allow", "Allow…")
        dlg.set_response_appearance("allow", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("allow")
        dlg.set_close_response("later")

        def respond(_d, resp):
            answer[0] = resp
            app.quit()
        dlg.connect("response", respond)
        present_alone(dlg)          # no parent: shown as its own small window

    app.connect("activate", activate)
    app.run(sys.argv[:1])
    print(answer[0], flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
