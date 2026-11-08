# SPDX-License-Identifier: GPL-3.0-or-later
"""The toolbar's settings app forgets a settings window once it's closed, however it closes; the
settings window keeps a device that matches the display as one."""
import unittest
from unittest import mock

try:
    from gi.repository import Gtk
    from waydroid_manager.gui import adwcompat, dialogs, settings
    DISPLAY = Gtk.init_check()
except (ImportError, ValueError):     # no GTK 4 / libadwaita typelibs (CI)
    settings = None
    DISPLAY = False


@unittest.skipIf(settings is None, "needs GTK 4 and libadwaita")
class PresentAloneTest(unittest.TestCase):
    def present(self, minor):
        dlg = mock.Mock()
        with mock.patch.object(adwcompat.Adw, "get_major_version", return_value=1), \
                mock.patch.object(adwcompat.Adw, "get_minor_version", return_value=minor):
            adwcompat.present_alone(dlg)
        dlg.present.assert_called_once_with(None)
        return dlg

    def test_window_destroy_emits_closed_before_libadwaita_1_7(self):
        dlg = self.present(5)
        signal, on_destroy = dlg.get_root.return_value.connect.call_args.args
        self.assertEqual(signal, "destroy")
        dlg.emit.assert_not_called()
        on_destroy(None)
        dlg.emit.assert_called_once_with("closed")

    def test_libadwaita_1_7_emits_closed_itself(self):
        dlg = self.present(7)
        dlg.get_root.return_value.connect.assert_not_called()


@unittest.skipUnless(DISPLAY, "needs GTK 4, libadwaita and a display")
class MatchDisplayTest(unittest.TestCase):
    INFO = {"id": "3", "name": "T", "android": "13", "width": "0", "height": "0", "dpi": "320", "state": "STOPPED"}

    def test_saved_as_0x0_with_its_dpi(self):
        d = dialogs.InstanceDialog("edit", lambda *a: None, info=dict(self.INFO))
        self.assertEqual(d._kind(), "display")
        v = d.values()
        self.assertEqual((v["width"], v["height"], v["dpi"]), ("0", "0", "320"))
        self.assertEqual(d.width_row.get_value(), 1280)     # Custom starts from the default size

    def test_new_devices_get_a_size(self):
        self.assertNotEqual(dialogs.InstanceDialog("create", lambda *a: None)._kind(), "display")


if __name__ == "__main__":
    unittest.main()
