# SPDX-License-Identifier: GPL-3.0-or-later
"""libadwaita helpers that work across its versions (Ubuntu 24.04 has 1.5)."""
import gi

gi.require_version("Adw", "1")
from gi.repository import Adw  # noqa: E402


def present_alone(dlg):
    """Show dlg as a window of its own. Before libadwaita 1.7 such a dialog never emits closed
    when its window is closed (only sheets did), so an alert dialog gives no response either:
    emit it when the window goes, or a window closed by the window manager would leave its
    caller waiting (settings.SettingsApp.windows, confirm.py's answer)."""
    dlg.present(None)
    if (Adw.get_major_version(), Adw.get_minor_version()) < (1, 7):
        dlg.get_root().connect("destroy", lambda _w: dlg.emit("closed"))
