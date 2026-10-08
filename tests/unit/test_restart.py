# SPDX-License-Identifier: GPL-3.0-or-later
"""Full restarts run stop/start in order, outside the instance session."""
import unittest
from unittest import mock

from waydroid_manager import client
from waydroid_manager.gui.backend import Backend


class RestartTest(unittest.TestCase):
    def test_start_waits_for_stop_and_failures_reach_the_caller(self):
        for stop_ok, start_ok in ((True, True), (False, True), (True, False)):
            with self.subTest(stop_ok=stop_ok, start_ok=start_ok):
                backend = Backend.__new__(Backend)
                backend.run_cli = mock.Mock()
                done = mock.Mock()
                backend.restart("0", done)
                self.assertEqual(backend.run_cli.call_args.args[0], ["stop", "0"])
                done.assert_not_called()
                backend.run_cli.call_args.args[1](stop_ok, "stop output")
                if stop_ok:
                    self.assertEqual(backend.run_cli.call_count, 2)
                    self.assertEqual(backend.run_cli.call_args.args[0], ["start", "0"])
                    done.assert_not_called()
                    backend.run_cli.call_args.args[1](start_ok, "start output")
                    done.assert_called_once_with(start_ok, "start output")
                else:
                    self.assertEqual(backend.run_cli.call_count, 1)
                    done.assert_called_once_with(False, "stop output")

    def test_settings_action_uses_existing_app_or_an_independent_unit(self):
        for restart in (False, True):
            for state in ("cold", "warm", "quitting", "unavailable"):
                with self.subTest(restart=restart, state=state), \
                        mock.patch.object(client.dbus, "SessionBus") as session_bus, \
                        mock.patch.object(client, "run_unit") as run_unit:
                    bus = session_bus.return_value
                    bus.name_has_owner.return_value = state != "cold"
                    if state == "unavailable":
                        session_bus.side_effect = client.dbus.DBusException("No session bus")
                    client.open_settings("1", restart=restart)
                    if state in ("warm", "quitting"):
                        self.assertEqual(bus.call_async.call_args.args[5],
                                         ("restart" if restart else "open", ["1"], {}))
                        run_unit.assert_not_called()
                        if state == "quitting":
                            bus.call_async.call_args.args[7](client.dbus.DBusException("App exited"))
                    if state != "warm":
                        run_unit.assert_called_once_with(
                            "settings-1", "waydroid-manager settings for 1",
                            [client.sys.executable, "-m", "waydroid_manager.gui.settings"]
                            + (["--restart"] if restart else []) + ["1"])


if __name__ == "__main__":
    unittest.main()
