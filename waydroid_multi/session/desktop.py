# SPDX-License-Identifier: GPL-3.0-or-later
"""Desktop entries for instances.

Each instance gets a launcher whose desktop-file id equals the app_id the
Wayland proxy assigns to its window ("waydroid-multi.<id>"), so GNOME shows
every instance as its own app. #0 runs stock Waydroid's data, so its launcher
is named plain "Waydroid" and stock's own entry is hidden for the user: one
Waydroid icon, which opens #0. (Overriding stock's entry under its own id
instead doesn't work: GNOME Shell's app grid keeps launching the old one.) Per-app entries are opt-in and namespaced
("waydroid-multi.<id>.<pkg>.desktop") so they never collide with the stock
instance's "waydroid.<pkg>.desktop" files.
"""
import os
import shlex
import shutil
import sys

from .. import paths


def _exec_prefix():
    exe = shutil.which("waydroid-multi")
    if exe:
        return [exe]
    pp = os.path.dirname(paths.PKG_DIR)
    return ["env", "PYTHONPATH=" + pp, sys.executable, "-m", "waydroid_multi"]


def _cmd(*args):
    return " ".join(shlex.quote(a) for a in list(_exec_prefix()) + list(args))


def _escape(v):
    return v.replace("\\", "\\\\").replace("\n", " ").replace("\r", " ")


def launcher_path(iid):
    return os.path.join(paths.user_applications_dir(), "waydroid-multi.{}.desktop".format(iid))


STOCK_LAUNCHER = "Waydroid.desktop"   # stock Waydroid's entry, hidden while #0 exists
MARK = "X-WaydroidMulti=true"           # our override, so uninstall removes only that


def hide_stock_launcher():
    """Hide stock's Waydroid icon for this user: #0's is the Waydroid icon. A window of stock
    Waydroid run by hand still gets its name and icon from this entry."""
    p = os.path.join(paths.user_applications_dir(), STOCK_LAUNCHER)
    text = ("[Desktop Entry]\nType=Application\nName=Waydroid\nExec=waydroid\nIcon=waydroid\n"
            "NoDisplay=true\n" + MARK + "\n")
    try:
        with open(p) as f:
            old = f.read()
        if old == text or MARK not in old.splitlines():
            return      # done, or the user's own override
    except FileNotFoundError:
        os.makedirs(paths.user_applications_dir(), exist_ok=True)
    with open(p, "w") as f:
        f.write(text)


def write_launcher(iid, name):
    os.makedirs(paths.user_applications_dir(), exist_ok=True)
    text = "\n".join([
        "[Desktop Entry]",
        "Type=Application",
        "Name=" + ("Waydroid" if iid == "0" else "{} (Waydroid)".format(_escape(name))),
        "Comment=Waydroid instance '{}' (waydroid-multi)".format(iid),
        "Exec=" + _cmd("start", iid),
        "Icon=waydroid",
        "Categories=X-WayDroid-App;",
        "StartupWMClass=waydroid-multi.{}".format(iid),
        "Actions=stop;",
        "",
        "[Desktop Action stop]",
        "Name=Stop instance",
        "Exec=" + _cmd("stop", iid),
        "",
    ])
    p = launcher_path(iid)
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, p)
    return p


def cleanup_launchers(existing_ids):
    """Remove launchers of instances that no longer exist (e.g. 0.1 name-based ids). With #0,
    stock's Waydroid icon is hidden, and #0's launcher from 0.4 gets its new name."""
    d = paths.user_applications_dir()
    keep = set(existing_ids)
    if "0" in keep:
        hide_stock_launcher()
        try:
            with open(launcher_path("0")) as f:
                old = "Name=Waydroid\n" not in f.read()
        except FileNotFoundError:
            old = False
        if old:
            write_launcher("0", "")
    try:
        names = os.listdir(d)
    except FileNotFoundError:
        return
    for f in names:
        if f.startswith("waydroid-multi.") and f.endswith(".desktop"):
            iid = f[len("waydroid-multi."):-len(".desktop")].split(".")[0]
            if iid not in keep:
                try:
                    os.unlink(os.path.join(d, f))
                except OSError:
                    pass


def remove_launcher(iid):
    for p in [launcher_path(iid)] + app_entries(iid):
        try:
            os.unlink(p)
        except FileNotFoundError:
            pass


def app_entries(iid):
    d = paths.user_applications_dir()
    prefix = "waydroid-multi.{}.".format(iid)
    try:
        return [os.path.join(d, f) for f in os.listdir(d) if f.startswith(prefix) and f.endswith(".desktop")]
    except FileNotFoundError:
        return []


def write_app_entry(iid, inst_name, app, icons_dir):
    pkg = app["packageName"]
    if not any(c.strip() == "android.intent.category.LAUNCHER" for c in app.get("categories", [])):
        remove_app_entry(iid, pkg)
        return
    p = os.path.join(paths.user_applications_dir(), "waydroid-multi.{}.{}.desktop".format(iid, pkg))
    icon = os.path.join(icons_dir, pkg + ".png")
    text = "\n".join([
        "[Desktop Entry]",
        "Type=Application",
        "Name={} ({})".format(_escape(app.get("name", pkg)), _escape(inst_name)),
        "Exec=" + _cmd("app", "launch", iid, pkg),
        "Icon=" + (icon if os.path.exists(icon) else "waydroid"),
        "Categories=X-WayDroid-App;",
        "StartupWMClass=waydroid-multi.{}.{}".format(iid, pkg),
        "",
    ])
    with open(p, "w") as f:
        f.write(text)


def remove_app_entry(iid, pkg):
    p = os.path.join(paths.user_applications_dir(), "waydroid-multi.{}.{}.desktop".format(iid, pkg))
    try:
        os.unlink(p)
    except FileNotFoundError:
        pass
