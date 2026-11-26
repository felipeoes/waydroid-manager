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


@unittest.skipIf(settings is None, "needs GTK 4 and libadwaita")
class ArmOptionsTest(unittest.TestCase):
    def test_arm_options_offers_digitalis_only_on_android_16_and_17(self):
        for ver in ("11", "13", "14", "15", "", None):
            self.assertEqual([k for k, _ in dialogs.arm_options(ver)], ["houdini", "libndk", "none"])
        for ver in ("16", "17"):
            opts = dialogs.arm_options(ver)
            self.assertEqual([k for k, _ in opts], ["houdini", "libndk", "digitalis", "none"])
            self.assertIn(("digitalis", "Digitalis (ARM64 only)"), opts)
        self.assertIn(("digitalis", "Digitalis (ARM64 only)"), dialogs.arm_options("13", current="digitalis"))


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

    def test_switching_android_between_16_and_13_updates_arm_options(self):
        d = dialogs.InstanceDialog("create", lambda *a: None)
        self.assertNotIn("digitalis", [k for k, _ in d.arm_opts])
        dialogs._select(d.arm_row, d.arm_opts, "none")
        dialogs._select(d.android_row, dialogs.ANDROID, "16")
        self.assertIn("digitalis", [k for k, _ in d.arm_opts])
        self.assertEqual(d.values()["arm_translation"], "none")     # kept across model rebuild
        dialogs._select(d.arm_row, d.arm_opts, "digitalis")
        self.assertEqual(d.values()["arm_translation"], "digitalis")
        dialogs._select(d.android_row, dialogs.ANDROID, "17")
        self.assertEqual(d.values()["arm_translation"], "digitalis")
        dialogs._select(d.android_row, dialogs.ANDROID, "13")
        self.assertNotIn("digitalis", [k for k, _ in d.arm_opts])
        self.assertEqual(d.values()["arm_translation"], "houdini")

    def test_edit_dialog_preserves_digitalis_on_android_16(self):
        info = dict(self.INFO, android="16", arm_translation="digitalis")
        d = dialogs.InstanceDialog("edit", lambda *a: None, info=info)
        self.assertIn("digitalis", [k for k, _ in d.arm_opts])
        self.assertEqual(d.values()["arm_translation"], "digitalis")


if __name__ == "__main__":
    unittest.main()
