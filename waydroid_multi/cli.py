# SPDX-License-Identifier: GPL-3.0-or-later
"""waydroid-multi command line interface."""
import argparse
import os
import shutil
import stat
import subprocess
import sys
import time

from . import __version__, paths
from .client import Daemon, DaemonError, platform_service, start_session, statusbar_service, stop_session, session_active
from .instance import SETTINGS, RESTART_SETTINGS, validate_id

ACTIVE = ("RUNNING", "FROZEN")


def die(msg, code=1):
    print("error: " + str(msg), file=sys.stderr)
    sys.exit(code)


def daemon():
    try:
        return Daemon()
    except DaemonError as e:
        die("{}\nIs it installed and running? (sudo systemctl start waydroid-multi)".format(e))


def wait_state(d, iid, wanted, timeout=120):
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            last = d.get(iid)["state"]
        except DaemonError:
            last = "?"
        if last in wanted:
            return last
        if not session_active(iid) and last == "STOPPED" and "STOPPED" not in wanted:
            # the session exited (e.g. failed to start)
            time.sleep(1)
            if not session_active(iid):
                return last
        time.sleep(0.5)
    return last


def stock_state():
    try:
        import dbus
        cm = dbus.Interface(dbus.SystemBus().get_object("id.waydro.Container", "/ContainerManager"),
                            "id.waydro.ContainerManager")
        s = cm.GetSession(timeout=5)
        return str(s.get("state", "RUNNING")) if s else "STOPPED"
    except Exception:  # noqa: BLE001
        return "STOPPED"


# -- commands --------------------------------------------------------------------

def cmd_list(o):
    d = daemon()
    rows = [("ID", "NAME", "STATE", "IP", "SIZE", "LIMITS")]
    rows.append(("default", "stock Waydroid", stock_state(), "192.168.240.x", "-", "-"))
    for i in sorted(d.list(), key=lambda x: int(x["index"])):
        size = "{}x{}".format(i["width"], i["height"]) if i["width"] != "0" else "auto"
        if i["dpi"] != "0":
            size += "@" + i["dpi"]
        lim = []
        if i["cpus"]:
            lim.append(i["cpus"] + " cpu")
        if i["cpuset"]:
            lim.append("cpus " + i["cpuset"])
        if i["memory"]:
            lim.append(i["memory"] + " mem")
        rows.append((i["id"], i["name"], i["state"], i["ip"], size, ", ".join(lim) or "-"))
    widths = [max(len(r[c]) for r in rows) for c in range(len(rows[0]))]
    for r in rows:
        print("  ".join(v.ljust(w) for v, w in zip(r, widths)).rstrip())


def settings_from_args(o):
    s = {}
    for key in ("name", "width", "height", "dpi", "cpus", "cpuset", "memory", "close_action", "idle_action"):
        v = getattr(o, key, None)
        if v is not None:
            s[key] = str(v)
    if getattr(o, "no_window_labels", False):
        s["window_labels"] = "false"
    for kv in getattr(o, "prop", None) or []:
        if "=" not in kv:
            die("--prop expects KEY=VALUE")
        k, v = kv.split("=", 1)
        s["prop:" + k] = v
    return s


def cmd_create(o):
    try:
        validate_id(o.id)
    except ValueError as e:
        die(e)
    d = daemon()
    opts = settings_from_args(o)
    if o.clone_from:
        opts["clone_from"] = o.clone_from
        opts["reset_ids"] = "false" if o.keep_ids else "true"
        print("Cloning {} into '{}'...".format(o.clone_from, o.id))
    try:
        d.create(o.id, opts)
    except DaemonError as e:
        die(e)
    info = d.get(o.id)
    if not o.no_launcher:
        from .session import desktop
        desktop.write_launcher(o.id, info["name"])
    print("Created instance '{}' (IP {}).".format(o.id, info["ip"]))
    if o.clone_from and not o.keep_ids:
        print("Device identity will be reset on first start. If this is a GAPPS image, register the new\n"
              "GSF ID afterwards: waydroid-multi gsf-id {}  ->  https://www.google.com/android/uncertified".format(o.id))
    print("Start it with: waydroid-multi start {}".format(o.id))


def cmd_clone(o):
    o.id, o.clone_from = o.new, o.src
    cmd_create(o)


def cmd_delete(o):
    d = daemon()
    for iid in o.ids:
        if not o.yes:
            ans = input("Delete instance '{}' and ALL its data? [y/N] ".format(iid))
            if ans.strip().lower() not in ("y", "yes"):
                continue
        stop_session(iid)
        try:
            d.delete(iid)
        except DaemonError as e:
            die(e)
        from .session import desktop
        desktop.remove_launcher(iid)
        print("Deleted '{}'.".format(iid))


def cmd_config(o):
    d = daemon()
    if o.action in (None, "show"):
        info = d.get(o.id)
        for k in SETTINGS:
            print("{:14} {:12} # {}".format(k, info.get(k, ""), SETTINGS[k][2]))
        props = {k[5:]: v for k, v in info.items() if k.startswith("prop:")}
        for k, v in sorted(props.items()):
            print("prop {} = {}".format(k, v))
        return
    if o.action == "get":
        info = d.get(o.id)
        key = o.args[0] if o.args else die("config get KEY")
        print(info.get(key, info.get("prop:" + key, "")))
        return
    values = {}
    if o.action == "set":
        if len(o.args) < 2 or len(o.args) % 2:
            die("config set KEY VALUE [KEY VALUE ...]")
        for k, v in zip(o.args[::2], o.args[1::2]):
            values[k] = v
    elif o.action == "prop":
        if len(o.args) != 2:
            die("config prop KEY VALUE   (empty VALUE removes it)")
        values["prop:" + o.args[0]] = o.args[1]
    try:
        d.set_config(o.id, values)
    except DaemonError as e:
        die(e)
    if "name" in values:
        from .session import desktop
        if os.path.exists(desktop.launcher_path(o.id)):
            desktop.write_launcher(o.id, values["name"])
    state = d.get(o.id)["state"]
    if state in ACTIVE and (set(values) & RESTART_SETTINGS or any(k.startswith("prop:") for k in values)):
        print("Saved. Restart the instance for the change to take effect.")
    else:
        print("Saved.")


def show_full_ui(iid):
    p = platform_service(iid)
    p.setprop("waydroid.active_apps", "Waydroid")
    p.settingsPutString(2, "policy_control", "null*")
    sb = statusbar_service(iid)
    if sb:  # refresh display contents, as stock does
        sb.expand()
        time.sleep(0.5)
        sb.collapse()


def ensure_running(d, iid, background=False):
    info = d.get(iid)
    if info["state"] == "FROZEN":
        d.unfreeze(iid)
        return False
    if info["state"] == "RUNNING":
        return False
    if info["state"] not in ("STOPPED",):
        die("instance '{}' is {}".format(iid, info["state"]))
    start_session(iid, background=background)
    st = wait_state(d, iid, ACTIVE)
    if st not in ACTIVE:
        die("instance '{}' failed to start (state {}). See: journalctl --user -u {}"
            .format(iid, st, "waydroid-multi-session-{}".format(iid)))
    return True


def cmd_start(o):
    d = daemon()
    info = d.get(o.id)
    if info["state"] in ACTIVE:
        if o.background:
            print("'{}' is already running.".format(o.id))
            return
        if info["state"] == "FROZEN":
            d.unfreeze(o.id)
        show_full_ui(o.id)
        return
    print("Starting '{}'...".format(o.id))
    ensure_running(d, o.id, background=o.background)
    print("'{}' is running (IP {}).".format(o.id, info["ip"]))
    if o.wait:
        cmd_wait(o)


def cmd_show(o):
    d = daemon()
    started = ensure_running(d, o.id)
    if not started:
        show_full_ui(o.id)


def cmd_stop(o):
    d = daemon()
    ids = [i["id"] for i in d.list() if i["state"] != "STOPPED"] if o.all else o.ids
    if not ids:
        if not o.all:
            die("give an instance id or --all")
        return
    for iid in ids:
        stop_session(iid)
        try:
            if d.get(iid)["state"] != "STOPPED":
                d.stop(iid)
        except DaemonError as e:
            die(e)
        print("Stopped '{}'.".format(iid))


def cmd_status(o):
    d = daemon()
    if not o.id:
        info = d.info()
        for k, v in info.items():
            print("{:14} {}".format(k, v))
        return
    info = d.get(o.id)
    for k in sorted(info):
        print("{:26} {}".format(k, info[k]))


def cmd_wait(o):
    deadline = time.time() + o.timeout
    d = daemon()
    while time.time() < deadline:
        if d.get(o.id)["state"] not in ACTIVE:
            time.sleep(1)
            continue
        try:
            p = platform_service(o.id, timeout=max(1, int(deadline - time.time())))
            if p.getprop("sys.boot_completed", "") == "1":
                print("'{}' has booted.".format(o.id))
                return
        except DaemonError:
            pass
        time.sleep(1)
    die("timed out waiting for '{}' to boot".format(o.id))


def cmd_app(o):
    d = daemon()
    if o.subaction == "install":
        if not os.path.isfile(o.apk):
            die("no such file: " + o.apk)
        ensure_running(d, o.id, background=True)
        cmd_wait(argparse.Namespace(id=o.id, timeout=180))
        try:
            print(d.install_apk(o.id, o.apk))
        except DaemonError as e:
            die(e)
        return
    if o.subaction == "launch":
        ensure_running(d, o.id)
        p = platform_service(o.id, timeout=180)
        p.setprop("waydroid.active_apps", o.package)
        p.launchApp(o.package)
        multi = p.getprop("persist.waydroid.multi_windows", "false")
        p.settingsPutString(2, "policy_control", "immersive.status=*" if multi == "false" else "immersive.full=*")
        return
    if d.get(o.id)["state"] not in ACTIVE:
        die("instance '{}' is not running".format(o.id))
    if d.get(o.id)["state"] == "FROZEN":
        d.unfreeze(o.id)
    p = platform_service(o.id)
    if o.subaction == "list":
        for app in p.getAppsInfo() or []:
            print("{:50} {}".format(app["packageName"], app["name"]))
    elif o.subaction == "remove":
        if p.removeApp(o.package) != 0:
            die("failed to remove " + o.package)
    elif o.subaction == "intent":
        p.launchIntent(o.action, o.uri)


def cmd_prop(o):
    d = daemon()
    if d.get(o.id)["state"] not in ACTIVE:
        die("instance '{}' is not running".format(o.id))
    p = platform_service(o.id)
    if o.subaction == "get":
        print(p.getprop(o.key, "") or "")
    else:
        p.setprop(o.key, o.value)


def cmd_adb(o):
    if not shutil.which("adb"):
        die("adb is not installed")
    ip = daemon().get(o.id)["ip"]
    sub = "connect" if o.subaction == "connect" else "disconnect"
    subprocess.run(["adb", sub, ip + ":5555"])


def reexec_as_root():
    if os.geteuid() != 0:
        os.execvp("sudo", ["sudo", "env", "PYTHONDONTWRITEBYTECODE=1", "PYTHONPATH=" + os.path.dirname(paths.PKG_DIR),
                           sys.executable, "-m", "waydroid_multi"] + sys.argv[1:])


def cmd_shell(o, logcat=False):
    reexec_as_root()
    from .daemon.util import android_attach_env, lxc_state
    if lxc_state(o.id) == "FROZEN":
        subprocess.run(["lxc-unfreeze", "-P", paths.LXC_PATH, "-n", paths.container_name(o.id)])
    elif lxc_state(o.id) != "RUNNING":
        die("instance '{}' is not running".format(o.id))
    env = android_attach_env(o.id)
    cmd = ["lxc-attach", "-P", paths.LXC_PATH, "-n", paths.container_name(o.id), "--clear-env"]
    for k, v in env.items():
        cmd += ["--set-var", "{}={}".format(k, v)]
    argv = (["/system/bin/logcat"] + o.args) if logcat else (o.command or ["/system/bin/sh"])
    cmd += ["--"] + argv
    try:
        mode = stat.S_IMODE(os.fstat(sys.stdout.fileno()).st_mode)
    except OSError:
        mode = None
    try:
        rc = subprocess.run(cmd).returncode
    except KeyboardInterrupt:
        rc = 130
    finally:
        if mode is not None:  # lxc-attach changes our stdout's mode
            try:
                os.fchmod(sys.stdout.fileno(), mode)
            except OSError:
                pass
    sys.exit(rc)


def cmd_log(o):
    if o.id:
        os.execvp("journalctl", ["journalctl", "--user", "-u", "waydroid-multi-session-{}".format(o.id),
                                 "-n", str(o.lines)] + (["-f"] if o.follow else []))
    os.execvp("journalctl", ["journalctl", "-u", "waydroid-multi", "-n", str(o.lines)] + (["-f"] if o.follow else []))


def cmd_images(o):
    d = daemon()
    if o.subaction == "sync":
        print("Syncing images from stock Waydroid (this copies ~3 GB once per image version)...")
        print("Current image set:", d.sync_images())
    else:
        info = d.info()
        print("current: {}\nstock:   {}".format(info["image"] or "(none)", info["stock_image"]))


def cmd_gsf(o):
    gid = daemon().gsf_id(o.id)
    if not gid:
        die("no GSF ID yet (is it a GAPPS image, running and booted? Google services may need a minute)")
    print(gid)
    print("Register it at https://www.google.com/android/uncertified to use the Play Store.", file=sys.stderr)


def cmd_doctor(o):
    ok = True

    def check(cond, good, bad):
        nonlocal ok
        print(("  [ok]   " + good) if cond else ("  [!!]   " + bad))
        ok &= bool(cond)

    from . import stock
    try:
        v = stock.version()
        check(stock.check_version() is None, "stock Waydroid " + v, stock.check_version() or "")
    except RuntimeError as e:
        check(False, "", str(e))
    check(os.path.exists("/dev/binderfs/binder-control"), "binderfs mounted", "binderfs not mounted (daemon mounts it on start)")
    check(shutil.which("lxc-start"), "LXC installed", "LXC not installed")
    check(shutil.which("dnsmasq"), "dnsmasq installed", "dnsmasq not installed")
    check(shutil.which("wl-copy"), "wl-clipboard installed", "wl-clipboard missing: no clipboard sharing")
    check(os.environ.get("WAYLAND_DISPLAY"), "Wayland session", "WAYLAND_DISPLAY not set")
    try:
        info = Daemon().info()
        check(True, "daemon {} running".format(info["version"]), "")
        check(not info["warnings"], "no warnings", info["warnings"])
        check(info["image"], "image store: " + (info["image"] or ""), "no image set yet: run 'waydroid-multi images sync'")
        from .netconfig import NetConfig
        bad = NetConfig.load().overlapping_routes()
        check(not bad, "subnet {} is free".format(info["subnet"]), "subnet overlaps: " + ", ".join(bad))
    except DaemonError as e:
        check(False, "", "daemon not reachable: {}".format(e))
    sys.exit(0 if ok else 1)


def cmd_session(o):
    from .session.main import main
    sys.exit(main(o.id, background=o.background))


def cmd_gui(o):
    from .gui.app import main
    sys.exit(main())


# -- parser ----------------------------------------------------------------------

def add_settings(p, create=True):
    p.add_argument("--name", help="display name")
    p.add_argument("--width", type=int, help="window width in px")
    p.add_argument("--height", type=int, help="window height in px")
    p.add_argument("--dpi", type=int, help="screen density")
    p.add_argument("--cpus", help="CPU quota in cores (e.g. 2)")
    p.add_argument("--cpuset", help="pin to host CPUs (e.g. 0-3)")
    p.add_argument("--memory", help="soft memory limit (e.g. 4G)")
    p.add_argument("--close-action", dest="close_action", choices=("stop", "freeze", "none"))
    p.add_argument("--idle-action", dest="idle_action", choices=("stop", "freeze", "none"))
    p.add_argument("--no-window-labels", action="store_true", help="don't label windows (no Wayland proxy)")
    p.add_argument("--prop", action="append", metavar="KEY=VALUE", help="Android property override")
    p.add_argument("--no-launcher", action="store_true", help="don't create a desktop launcher")


def parser():
    p = argparse.ArgumentParser(prog="waydroid-multi", description="Run several Waydroid instances side by side.")
    p.add_argument("-V", "--version", action="version", version="waydroid-multi " + __version__)
    sub = p.add_subparsers(dest="cmd", metavar="COMMAND")

    sub.add_parser("list", help="list instances").set_defaults(fn=cmd_list)

    c = sub.add_parser("create", help="create an instance")
    c.add_argument("id")
    c.add_argument("--clone-from", metavar="SRC", help="copy data from an instance or 'default' (stock)")
    c.add_argument("--keep-ids", action="store_true", help="keep the source's device identity when cloning")
    add_settings(c)
    c.set_defaults(fn=cmd_create)

    c = sub.add_parser("clone", help="clone an instance (or 'default') into a new one")
    c.add_argument("src")
    c.add_argument("new")
    c.add_argument("--keep-ids", action="store_true")
    add_settings(c)
    c.set_defaults(fn=cmd_clone)

    c = sub.add_parser("delete", help="delete instances and their data")
    c.add_argument("ids", nargs="+")
    c.add_argument("-y", "--yes", action="store_true")
    c.set_defaults(fn=cmd_delete)

    c = sub.add_parser("config", help="show or change instance settings")
    c.add_argument("id")
    c.add_argument("action", nargs="?", choices=("show", "get", "set", "prop"))
    c.add_argument("args", nargs="*")
    c.set_defaults(fn=cmd_config)

    c = sub.add_parser("start", help="start an instance and open its window")
    c.add_argument("id")
    c.add_argument("--background", action="store_true", help="don't open the window")
    c.add_argument("--wait", action="store_true", help="wait until Android has booted")
    c.add_argument("--timeout", type=int, default=180)
    c.set_defaults(fn=cmd_start)

    c = sub.add_parser("show", help="show an instance's window (starting it if needed)")
    c.add_argument("id")
    c.set_defaults(fn=cmd_show)

    c = sub.add_parser("stop", help="stop instances")
    c.add_argument("ids", nargs="*")
    c.add_argument("--all", action="store_true")
    c.set_defaults(fn=cmd_stop)

    c = sub.add_parser("status", help="daemon or instance status")
    c.add_argument("id", nargs="?")
    c.set_defaults(fn=cmd_status)

    c = sub.add_parser("wait", help="wait until an instance has booted")
    c.add_argument("id")
    c.add_argument("--timeout", type=int, default=180)
    c.set_defaults(fn=cmd_wait)

    c = sub.add_parser("app", help="manage apps in an instance")
    asub = c.add_subparsers(dest="subaction", required=True)
    x = asub.add_parser("install")
    x.add_argument("id")
    x.add_argument("apk")
    x = asub.add_parser("remove")
    x.add_argument("id")
    x.add_argument("package")
    x = asub.add_parser("launch")
    x.add_argument("id")
    x.add_argument("package")
    x = asub.add_parser("list")
    x.add_argument("id")
    x = asub.add_parser("intent")
    x.add_argument("id")
    x.add_argument("action")
    x.add_argument("uri")
    c.set_defaults(fn=cmd_app)

    c = sub.add_parser("prop", help="get/set Android properties at runtime")
    psub = c.add_subparsers(dest="subaction", required=True)
    x = psub.add_parser("get")
    x.add_argument("id")
    x.add_argument("key")
    x = psub.add_parser("set")
    x.add_argument("id")
    x.add_argument("key")
    x.add_argument("value")
    c.set_defaults(fn=cmd_prop)

    c = sub.add_parser("adb", help="connect adb to an instance")
    c.add_argument("subaction", choices=("connect", "disconnect"))
    c.add_argument("id")
    c.set_defaults(fn=cmd_adb)

    c = sub.add_parser("shell", help="root shell inside an instance (uses sudo)")
    c.add_argument("id")
    c.add_argument("command", nargs=argparse.REMAINDER)
    c.set_defaults(fn=cmd_shell)

    c = sub.add_parser("logcat", help="Android logcat of an instance (uses sudo)")
    c.add_argument("id")
    c.add_argument("args", nargs=argparse.REMAINDER)
    c.set_defaults(fn=lambda o: cmd_shell(o, logcat=True))

    c = sub.add_parser("log", help="session log of an instance, or the daemon log")
    c.add_argument("id", nargs="?")
    c.add_argument("-n", "--lines", type=int, default=60)
    c.add_argument("-f", "--follow", action="store_true")
    c.set_defaults(fn=cmd_log)

    c = sub.add_parser("images", help="manage the image store")
    c.add_argument("subaction", choices=("sync", "list"))
    c.set_defaults(fn=cmd_images)

    c = sub.add_parser("gsf-id", help="print the Google Services Framework ID (for Play Store registration)")
    c.add_argument("id")
    c.set_defaults(fn=cmd_gsf)

    sub.add_parser("doctor", help="check the host setup").set_defaults(fn=cmd_doctor)

    c = sub.add_parser("session", help=argparse.SUPPRESS)
    c.add_argument("id")
    c.add_argument("--background", action="store_true")
    c.set_defaults(fn=cmd_session)

    sub.add_parser("gui", help="open the instance manager window").set_defaults(fn=cmd_gui)
    return p


def main(argv=None):
    p = parser()
    o = p.parse_args(argv)
    if not getattr(o, "fn", None):
        p.print_help()
        return 0
    try:
        o.fn(o)
    except DaemonError as e:
        die(e)
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
