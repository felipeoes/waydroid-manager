# SPDX-License-Identifier: GPL-3.0-or-later
"""Stock Waydroid's own runtime, which shares its data with instance #0.

Only one of them may run at a time: callers stop stock Waydroid before #0
starts or is cloned.
"""
import os
import subprocess
import time

UNIT = "waydroid-multi-stock-ui.service"  # started by 0.2 and older


CGROUP = "/sys/fs/cgroup/lxc.payload.waydroid"


def container_state():
    """Stock container state read from its cgroup.

    Deliberately does not ask the stock container service: stock Waydroid
    1.6.x leaks a pipe and an epoll fd per status query (run_core never closes
    its selector), so polling 'waydroid status' eventually exhausts its fds.
    """
    try:
        with open(os.path.join(CGROUP, "cgroup.events")) as f:
            for line in f:
                if line.startswith("frozen "):
                    return "FROZEN" if line.split()[1] == "1" else "RUNNING"
        return "RUNNING"
    except OSError:
        return "STOPPED"


def session_running():
    try:
        import dbus
        return bool(dbus.SessionBus().name_has_owner("id.waydro.Session"))
    except Exception:  # noqa: BLE001
        return False


def status():
    return {"session": "RUNNING" if session_running() else "STOPPED", "container": container_state()}


def state():
    """RUNNING, FROZEN or STOPPED."""
    return container_state()


def stop(timeout=60):
    subprocess.run(["waydroid", "session", "stop"], capture_output=True, timeout=timeout)
    subprocess.run(["systemctl", "--user", "stop", UNIT], capture_output=True)
    _wait(lambda: container_state() == "STOPPED", 30)


def _wait(cond, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(1)
    return False
