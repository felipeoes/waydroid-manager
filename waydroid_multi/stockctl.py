# SPDX-License-Identifier: GPL-3.0-or-later
"""Control the stock ("default") Waydroid instance through its own CLI.

Handles the state stock Waydroid itself gets stuck in when Android is shut
down from inside: the session process stays alive while the container is
STOPPED, and ``waydroid show-full-ui`` then waits forever for Android.
"""
import os
import shutil
import subprocess
import time

UNIT = "waydroid-multi-stock-ui.service"


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


def _wait(cond, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(1)
    return False


def start_or_show(timeout=90):
    """Show the stock window, starting (or un-wedging) stock Waydroid if needed."""
    st = status()
    if st["session"] == "RUNNING" and st["container"] not in ("RUNNING", "FROZEN"):
        # Stale session (Android was powered off): clear it before starting again
        stop()
        _wait(lambda: status()["session"] != "RUNNING", 30)
        st = status()
    if st["container"] in ("RUNNING", "FROZEN"):
        subprocess.Popen(["waydroid", "show-full-ui"], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, start_new_session=True)
        return True
    # No session: 'waydroid show-full-ui' becomes the session process, so run it
    # outside our own process tree.
    if shutil.which("systemd-run"):
        subprocess.run(["systemctl", "--user", "reset-failed", UNIT], capture_output=True)
        r = subprocess.run(["systemd-run", "--user", "--unit=" + UNIT, "--collect",
                            "--description=stock Waydroid session", "waydroid", "show-full-ui"],
                           capture_output=True)
        if r.returncode != 0:
            subprocess.Popen(["waydroid", "show-full-ui"], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, start_new_session=True)
    else:
        subprocess.Popen(["waydroid", "show-full-ui"], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, start_new_session=True)
    return _wait(lambda: status()["container"] in ("RUNNING", "FROZEN"), timeout)
