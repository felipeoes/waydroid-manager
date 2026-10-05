# SPDX-License-Identifier: GPL-3.0-or-later
"""Desktop entries for instances.

Each instance gets a launcher whose desktop-file id equals the app_id the
Wayland proxy assigns to its window ("waydroid-multi.<id>"), so GNOME shows
every instance as its own app. #0 runs stock Waydroid's data, so its launcher
instead overrides stock's own "Waydroid" entry for the user (same id, in
~/.local/share/applications): one Waydroid icon, which opens #0. Per-app entries are opt-in and namespaced
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


STOCK_LAUNCHER = "Waydroid.desktop"   # stock Waydroid's entry, overridden by #0's
MARK = "X-WaydroidMulti=true"           # ours, so uninstall removes only our override


def launcher_path(iid):
    name = STOCK_LAUNCHER if iid == "0" else "waydroid-multi.{}.desktop".format(iid)
    return os.path.join(paths.user_applications_dir(), name)


def has_launcher(iid):
    """Shown in the app grid (#0's is hidden, not removed: stock's entry would show again)."""
    try:
        with open(launcher_path(iid)) as f:
            return "NoDisplay=true" not in f.read().splitlines()
    except FileNotFoundError:
        return False


def write_launcher(iid, name, hidden=False):
    os.makedirs(paths.user_applications_dir(), exist_ok=True)
    stock = iid == "0"
    text = "\n".join([
        "[Desktop Entry]",
        "Type=Application",
        "Name=" + ("Waydroid" if stock else "{} (Waydroid)".format(_escape(name))),
        "Comment=Waydroid instance '{}' (waydroid-multi)".format(iid),
        "Exec=" + _cmd("start", iid),
        "Icon=waydroid",
        "Categories=X-WayDroid-App;",
        "StartupWMClass=" + ("Waydroid" if stock else "waydroid-multi.{}".format(iid)),
        "Actions=stop;",
    ] + (["NoDisplay=true"] if hidden else []) + ([MARK] if stock else []) + [
        "",
        "[Desktop Action stop]",
        "Name=Stop instance",
        "Exec=" + _cmd("stop", iid),
        "",
    ])
    if stock:   # 0.4 and earlier: #0 had its own icon next to stock's
        try:
            os.unlink(os.path.join(paths.user_applications_dir(), "waydroid-multi.0.desktop"))
        except FileNotFoundError:
            pass
    p = launcher_path(iid)
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, p)
    return p


def cleanup_launchers(existing_ids):
    """Remove launchers of instances that no longer exist (e.g. 0.1 name-based ids), and
    replace stock's Waydroid icon with #0's."""
    d = paths.user_applications_dir()
    keep = set(existing_ids)
    if "0" in keep and not os.path.exists(launcher_path("0")):
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
    if iid == "0":
        write_launcher(iid, "", hidden=True)
    for p in ([] if iid == "0" else [launcher_path(iid)]) + app_entries(iid):
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
