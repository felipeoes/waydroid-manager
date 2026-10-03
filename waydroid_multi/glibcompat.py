# SPDX-License-Identifier: GPL-3.0-or-later
"""GLib helpers that work across PyGObject versions."""
from gi.repository import GLib

try:
    import gi
    gi.require_version("GLibUnix", "2.0")
    from gi.repository import GLibUnix
    _signal_add = GLibUnix.signal_add
except (ImportError, ValueError, AttributeError):  # older PyGObject
    _signal_add = GLib.unix_signal_add


def signal_add(signum, callback, *args):
    """Run callback(*args) on the main loop when signum arrives."""
    return _signal_add(GLib.PRIORITY_HIGH, signum, lambda *_: callback(*args) or False)
