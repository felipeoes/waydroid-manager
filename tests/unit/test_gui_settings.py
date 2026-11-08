# SPDX-License-Identifier: GPL-3.0-or-later
"""The toolbar's settings app forgets a settings window once it's closed, however it closes."""
import unittest
from unittest import mock

try:
    from waydroid_manager.gui import settings
except (ImportError, ValueError):     # no GTK 4 / libadwaita typelibs (CI)
    settings = None


@unittest.skipIf(settings is None, "needs GTK 4 and libadwaita")
class PresentAloneTest(unittest.TestCase):
    def present(self, minor):
        dlg = mock.Mock()
        with mock.patch.object(settings.Adw, "get_major_version", return_value=1), \
                mock.patch.object(settings.Adw, "get_minor_version", return_value=minor):
            settings.present_alone(dlg)
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


if __name__ == "__main__":
    unittest.main()
