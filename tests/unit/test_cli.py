# SPDX-License-Identifier: GPL-3.0-or-later
"""Full UI startup restores Android's bars after an individual app was launched."""
import argparse
import unittest
from unittest import mock

from waydroid_manager import cli


class FullUITest(unittest.TestCase):
    def test_start_and_show_restore_full_ui_unless_background(self):
        for command, background in ((cli.cmd_start, False), (cli.cmd_show, False), (cli.cmd_start, True)):
            for state in ("STOPPED", "RUNNING", "FROZEN"):
                with self.subTest(command=command.__name__, background=background, state=state), \
                        mock.patch.object(cli, "daemon") as daemon, \
                        mock.patch.object(cli, "start_session"), \
                        mock.patch.object(cli, "wait_state", return_value="RUNNING"), \
                        mock.patch.object(cli, "platform_service") as platform, \
                        mock.patch.object(cli, "statusbar_service", return_value=None):
                    daemon.return_value.get.return_value = {"state": state, "ip": "192.168.241.11"}
                    command(argparse.Namespace(id="1", background=background, wait=False, timeout=180))
                    if background:
                        platform.assert_not_called()
                    else:
                        platform.assert_called_once_with("1", timeout=180 if state == "STOPPED" else 15)
                        platform.return_value.setprop.assert_called_once_with("waydroid.active_apps", "Waydroid")
                        platform.return_value.settingsPutString.assert_called_once_with(2, "policy_control", "null*")


if __name__ == "__main__":
    unittest.main()
