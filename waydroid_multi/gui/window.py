# SPDX-License-Identifier: GPL-3.0-or-later
"""Main window: the instance list."""
import os

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from .. import paths  # noqa: E402
from ..instance import RESTART_SETTINGS  # noqa: E402
from ..session import desktop  # noqa: E402
from .backend import Backend  # noqa: E402
from .dialogs import CloneDialog, InstanceDialog  # noqa: E402
from .pickapk import apk_dialog  # noqa: E402

ACTIVE = ("RUNNING", "FROZEN")
BUSY = ("STARTING", "STOPPING", "CLONING", "DELETING")
STATE_STYLE = {"RUNNING": "success", "FROZEN": "warning", "STOPPED": "dim-label"}
STATE_LABEL = {"RUNNING": "Running", "FROZEN": "Paused", "STOPPED": "Stopped", "STARTING": "Starting…",
               "STOPPING": "Stopping…", "CLONING": "Cloning…", "DELETING": "Deleting…"}
# install.sh puts the uninstaller next to the package (PREFIX/lib/waydroid-multi/)
UNINSTALL_SCRIPT = os.path.join(os.path.dirname(paths.PKG_DIR), "uninstall.sh")


def gigabytes(n):
    try:
        return "{:.1f} GB".format(int(n) / 1024 ** 3)
    except (TypeError, ValueError):
        return ""


def describe(info):
    parts = []
    if info.get("index"):
        parts.append("#" + info["index"])
    parts.append(STATE_LABEL.get(info["state"], info["state"].title()))
    lim = []
    if info.get("cpus"):
        lim.append("{} CPU".format(info["cpus"]))
    if info.get("memory"):
        lim.append(info["memory"].replace("G", " GB"))
    if lim:
        parts.append(", ".join(lim))
    return " · ".join(parts)


def _flat_button(icon, tooltip, cb):
    b = Gtk.Button(icon_name=icon, valign=Gtk.Align.CENTER, tooltip_text=tooltip)
    b.add_css_class("flat")
    b.connect("clicked", lambda *_: cb())
    return b


class BaseRow(Adw.ActionRow):
    """Status dot, spinner, start/show and stop buttons, and a ⋮ menu built on demand."""

    def __init__(self, win, info, check=False):
        super().__init__()
        self.win = win
        self.info = info
        self.check = None
        if check:  # for batch actions; clicking the row toggles it too
            self.check = Gtk.CheckButton(valign=Gtk.Align.CENTER)
            self.check.connect("toggled", lambda *_: win.selection_changed())
            self.add_prefix(self.check)
            self.set_activatable_widget(self.check)
        self.dot = Gtk.Label(label="●", valign=Gtk.Align.CENTER)
        self.add_prefix(self.dot)
        self.spinner = Adw.Spinner(valign=Gtk.Align.CENTER, visible=False)
        self.add_suffix(self.spinner)
        self.play = _flat_button("media-playback-start-symbolic", "Start", lambda: win.start_or_show(self.info))
        self.add_suffix(self.play)
        self.stop = _flat_button("media-playback-stop-symbolic", "Stop", lambda: win.stop(self.info))
        self.add_suffix(self.stop)
        # Disk space used, next to ⋮ so it lines up across rows; filled in by update() (instances only: the daemon measures it)
        self.disk = Gtk.Box(spacing=4, valign=Gtk.Align.CENTER, visible=False, css_classes=["dim-label"],
                            tooltip_text="Disk space used by this instance")
        self.disk.append(Gtk.Image(icon_name="drive-harddisk-symbolic"))
        self.disk_label = Gtk.Label(width_chars=6, xalign=1)
        self.disk.append(self.disk_label)
        self.add_suffix(self.disk)
        self.more = Gtk.MenuButton(icon_name="view-more-symbolic", valign=Gtk.Align.CENTER, tooltip_text="More")
        self.more.add_css_class("flat")
        # Rebuilt every time it opens so labels reflect the current state
        self.more.set_create_popup_func(lambda btn: btn.set_menu_model(self.menu()))
        self.add_suffix(self.more)
        self.actions = Gio.SimpleActionGroup()
        self.insert_action_group("row", self.actions)

    def add_action(self, name, cb):
        a = Gio.SimpleAction.new(name, None)
        a.connect("activate", lambda *_: cb())
        self.actions.add_action(a)

    def menu(self):
        return Gio.Menu()

    def update(self, info):
        self.info = info
        st = info["state"]
        self.set_subtitle(GLib.markup_escape_text(self.subtitle()))
        for c in ("success", "warning", "dim-label", "accent"):
            self.dot.remove_css_class(c)
        self.dot.add_css_class(STATE_STYLE.get(st, "accent"))
        self.disk_label.set_label(gigabytes(info.get("disk_used")))
        self.disk.set_visible(bool(self.disk_label.get_label()))
        busy = st in BUSY or info["id"] in self.win.busy
        self.spinner.set_visible(busy)
        self.play.set_visible(not busy)
        self.stop.set_visible(st in ACTIVE and not busy)
        if st in ACTIVE:
            self.play.set_icon_name("view-reveal-symbolic")
            self.play.set_tooltip_text("Show window")
        else:
            self.play.set_icon_name("media-playback-start-symbolic")
            self.play.set_tooltip_text("Start")

    def subtitle(self):
        return describe(self.info)


class InstanceRow(BaseRow):
    def __init__(self, win, info):
        # #0 (stock Waydroid) can't be deleted, so it stays out of the batch selection
        super().__init__(win, info, check=info["id"] != "0")
        self.add_action("settings", lambda: win.edit(self.info["id"]))
        self.add_action("clone", lambda: win.clone(self.info))
        self.add_action("install", lambda: win.install_apk(self.info["id"]))
        self.add_action("launcher", lambda: win.toggle_launcher(self.info))
        self.add_action("delete", lambda: win.delete(self.info))
        self.update(info)

    def menu(self):
        m = Gio.Menu()
        m.append("Settings…", "row.settings")
        m.append("Clone…", "row.clone")
        m.append("Install APK…", "row.install")
        has = os.path.exists(desktop.launcher_path(self.info["id"]))
        m.append("Remove from app grid" if has else "Add to app grid", "row.launcher")
        if self.info["id"] == "0":
            return m
        danger = Gio.Menu()
        danger.append("Delete…", "row.delete")
        m.append_section(None, danger)
        return m

    def update(self, info):
        self.set_title(GLib.markup_escape_text(info["name"]))
        super().update(info)


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Waydroid Instances")
        self.set_default_size(720, 560)
        self.rows = {}
        self.instances = []
        self.busy = set()
        self.backend = Backend(self.refresh)

        self.toasts = Adw.ToastOverlay()
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        menu = Gio.Menu()
        menu.append("Start all", "app.start-all")
        menu.append("Stop all", "app.stop-all")
        about = Gio.Menu()
        about.append("About", "app.about")
        if os.path.exists(UNINSTALL_SCRIPT):
            about.append("Uninstall Waydroid Multi…", "app.uninstall")
        menu.append_section(None, about)
        header.pack_start(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, tooltip_text="Menu"))
        view.add_top_bar(header)


        self.stack = Gtk.Stack()
        # Like an Adw.PreferencesPage, but not capped at its 600 px: the list uses the window's width
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24, margin_top=24, margin_bottom=24,
                       margin_start=12, margin_end=12)
        # Default (stock Waydroid, #0) first, then the instances
        self.stock_group = Adw.PreferencesGroup(title="Default", visible=False)
        page.append(self.stock_group)
        self.group = Adw.PreferencesGroup(title="Instances")
        new_btn = Gtk.Button(child=Adw.ButtonContent(icon_name="list-add-symbolic", label="New Instance"),
                             valign=Gtk.Align.CENTER, css_classes=["flat"])
        new_btn.connect("clicked", lambda *_: self.new_instance())
        self.group.set_header_suffix(new_btn)
        page.append(self.group)
        # Batch actions on the checked instances: the list's first row, its checkbox in line with
        # theirs (an invisible dot stands in for the status dot)
        self.batch_row = Adw.ActionRow(title="Select all", visible=False)
        self.select_all = Gtk.CheckButton(valign=Gtk.Align.CENTER)
        self.select_all.connect("toggled", self._toggle_all)
        self.batch_row.add_prefix(self.select_all)
        self.batch_row.add_prefix(Gtk.Label(label="●", valign=Gtk.Align.CENTER, opacity=0))
        self.batch_row.set_activatable_widget(self.select_all)
        for attr, label, style, cb in (("batch_start", "Start", "suggested-action", self.start_selected),
                                       ("batch_stop", "Stop", None, self.stop_selected),
                                       ("batch_delete", "Delete", "destructive-action", self.delete_selected)):
            b = Gtk.Button(label=label, valign=Gtk.Align.CENTER, css_classes=[style] if style else [])
            b.connect("clicked", lambda _b, cb=cb: cb())
            self.batch_row.add_suffix(b)
            setattr(self, attr, b)
        self.group.add(self.batch_row)
        self.empty_row = Adw.ActionRow(title="No instances yet",
                                       subtitle="Create one to run another Android next to stock Waydroid")
        new_btn = Gtk.Button(label="New Instance", valign=Gtk.Align.CENTER, css_classes=["suggested-action"])
        new_btn.connect("clicked", lambda *_: self.new_instance())
        self.empty_row.add_suffix(new_btn)
        self.group.add(self.empty_row)
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True,
                                    child=Adw.Clamp(maximum_size=1200, child=page))
        self.stack.add_named(scroll, "list")

        self.error_page = Adw.StatusPage(icon_name="dialog-error-symbolic", title="Daemon not available",
                                         description="Start it with: sudo systemctl start waydroid-multi")
        retry = Gtk.Button(label="Retry", halign=Gtk.Align.CENTER, css_classes=["pill"])
        retry.connect("clicked", lambda *_: self.refresh())
        self.error_page.set_child(retry)
        self.stack.add_named(self.error_page, "error")

        self.toasts.set_child(self.stack)
        view.set_content(self.toasts)
        self.set_content(view)
        self.refresh()
        GLib.timeout_add_seconds(5, self._tick)

    # -- data --------------------------------------------------------------------
    def _tick(self):
        self.refresh()
        return True

    def refresh(self):
        self.backend.call("List", ok=self._got_list, fail=lambda m: self.stack.set_visible_child_name("error"),
                          timeout=30)

    def _got_list(self, items):
        self.stack.set_visible_child_name("list")
        self.instances = sorted(items, key=lambda i: int(i["index"]))
        others = [i for i in self.instances if i["id"] != "0"]
        if not getattr(self, "_launchers_cleaned", False):
            desktop.cleanup_launchers([i["id"] for i in self.instances])
            self._launchers_cleaned = True
        ids = {i["id"] for i in self.instances}
        for iid in list(self.rows):
            if iid not in ids:
                (self.stock_group if iid == "0" else self.group).remove(self.rows.pop(iid))
        for info in self.instances:
            row = self.rows.get(info["id"])
            if row:
                row.update(info)
            else:
                row = InstanceRow(self, info)
                self.rows[info["id"]] = row
                (self.stock_group if info["id"] == "0" else self.group).add(row)
        self.stock_group.set_visible("0" in ids)
        self.empty_row.set_visible(not others)
        self.batch_row.set_visible(bool(others))
        self.selection_changed()
        if not getattr(self, "_fitted", False):
            # Open as tall as the list (scrolling past 800 px), not with empty space below it
            self._fitted = True
            width = self.get_width() or 720
            height = self.get_content().measure(Gtk.Orientation.VERTICAL, width)[1]
            self.set_default_size(width, min(height, 800))

    def toast(self, msg, timeout=4):
        t = Adw.Toast(title=GLib.markup_escape_text(msg))
        t.set_timeout(timeout)
        self.toasts.add_toast(t)

    def row_for(self, iid):
        return self.rows.get(iid)

    def set_busy(self, iid, on):
        (self.busy.add if on else self.busy.discard)(iid)
        row = self.row_for(iid)
        if row:
            row.update(row.info)

    def _cli(self, iid, args, fail_msg, then=None):
        self.set_busy(iid, True)

        def done(ok, out):
            self.set_busy(iid, False)
            if not ok:
                self.toast(out.splitlines()[-1] if out else fail_msg)
            self.refresh()
            if then:
                then(ok)
        self.backend.run_cli(args, done)

    # -- selection / batch -------------------------------------------------------
    def selected(self):
        return [r.info for r in self.checkable() if r.check.get_active()]

    def checkable(self):
        return [r for r in self.rows.values() if r.check]

    def selection_changed(self):
        sel = self.selected()
        n = len(sel)
        self.batch_row.set_subtitle("{} selected".format(n) if n else "")
        self.batch_start.set_sensitive(any(i["state"] == "STOPPED" for i in sel))
        self.batch_stop.set_sensitive(any(i["state"] in ACTIVE for i in sel))
        self.batch_delete.set_sensitive(n > 0)
        all_on = bool(self.checkable()) and n == len(self.checkable())
        if self.select_all.get_active() != all_on:
            self.select_all.handler_block_by_func(self._toggle_all)
            self.select_all.set_active(all_on)
            self.select_all.handler_unblock_by_func(self._toggle_all)

    def _toggle_all(self, btn):
        on = btn.get_active()  # read once: each row's toggle re-syncs this button mid-loop
        for row in self.checkable():
            row.check.set_active(on)

    def start_selected(self):
        todo = [i for i in self.selected() if i["state"] == "STOPPED"]
        for info in todo:
            self.start_or_show(info)
        if todo:
            self.toast("Starting {} instance{}…".format(len(todo), "s" if len(todo) > 1 else ""))

    def stop_selected(self):
        for info in self.selected():
            if info["state"] in ACTIVE:
                self.stop(info)

    def delete_selected(self):
        sel = self.selected()
        if not sel:
            return
        names = ", ".join("“{}”".format(i["name"]) for i in sel[:5]) + ("…" if len(sel) > 5 else "")
        dlg = Adw.AlertDialog(heading="Delete {} instance{}?".format(len(sel), "s" if len(sel) > 1 else ""),
                              body="{} and all their Android data will be permanently removed.".format(names))
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("delete", "Delete")
        dlg.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_default_response("cancel")

        def respond(_d, resp):
            if resp == "delete":
                for info in sel:
                    self._delete_now(info)
        dlg.connect("response", respond)
        dlg.present(self)

    # -- actions -----------------------------------------------------------------
    def start_or_show(self, info):
        self._cli(info["id"], ["start", info["id"]], "Failed to start " + info["name"])

    def stop(self, info):
        self._cli(info["id"], ["stop", info["id"]], "Failed to stop " + info["name"])

    def start_all(self):
        for i in self.instances:
            # not #0: starting it closes a plain Waydroid session the user may have open
            if i["state"] == "STOPPED" and i["id"] != "0":
                self.start_or_show(i)

    def stop_all(self):
        for i in self.instances:
            if i["state"] in ACTIVE:
                self.stop(i)

    def new_instance(self):
        InstanceDialog("create", self._create).present(self)

    def _create(self, values):
        name = values.get("name") or "new instance"
        self.toast("Creating “{}”…".format(name))

        def ok(iid):
            info_name = values.get("name") or "Instance {}".format(iid)
            desktop.write_launcher(iid, info_name)
            self.toast("Created #{} “{}”".format(iid, info_name))
            self.refresh()
        self.backend.call("Create", values, ok=ok, fail=self.toast)

    def clone(self, info):
        CloneDialog(info, self._clone).present(self)

    def _clone(self, source, values):
        def do_clone(ok=True):
            if not ok:
                return
            self.toast("Copying “{}”…".format(source["name"]), timeout=10)

            def done(iid):
                name = values.get("name") or "Instance {}".format(iid)
                desktop.write_launcher(iid, name)
                msg = "Cloned into #{} “{}”".format(iid, name)
                if values.get("reset_ids") == "true":
                    msg += " — new device identity on first start"
                self.toast(msg, timeout=6)
                self.refresh()
            self.backend.call("Create", values, ok=done, fail=self.toast)

        if source["state"] in ACTIVE or source["id"] == "0":  # 'stop 0' also stops stock Waydroid itself
            self._cli(source["id"], ["stop", source["id"]], "Could not stop " + source["name"], then=do_clone)
        else:
            do_clone()

    def edit(self, iid):
        def got(info):
            InstanceDialog("edit", lambda iid, values: self._save(iid, values, info), info=info).present(self)
        self.backend.call("Get", iid, ok=got, fail=self.toast, timeout=30)

    def _save(self, iid, values, before):
        # the dialog sends every field: only what it changed may need a restart
        changed = {k for k, v in values.items() if before.get(k, "") != v}
        restart = any(k in RESTART_SETTINGS or k.startswith("prop:") for k in changed)

        def ok(*_):
            info = next((i for i in self.instances if i["id"] == iid), {})
            if "name" in values and os.path.exists(desktop.launcher_path(iid)):
                desktop.write_launcher(iid, values["name"])
            self.refresh()
            if restart and info.get("state") in ACTIVE:
                self.ask_restart(info)
            else:
                self.toast("Saved")
        self.backend.call("SetConfig", iid, values, ok=ok, fail=self.toast, timeout=60)

    def ask_restart(self, info):
        dlg = Adw.AlertDialog(heading="Restart “{}”?".format(info["name"]),
                              body="The new settings apply when the instance restarts.")
        dlg.add_response("later", "Restart Later")
        dlg.add_response("restart", "Restart Now")
        dlg.set_response_appearance("restart", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("restart")

        def respond(_d, resp):
            if resp == "restart":
                self._cli(info["id"], ["stop", info["id"]], "Failed to stop " + info["name"],
                          then=lambda ok: ok and self.start_or_show(info))
        dlg.connect("response", respond)
        dlg.present(self)

    def uninstall(self):
        running = [i for i in self.instances if i.get("state") in ACTIVE]
        body = ("Removes the program, its background service and the instances' app grid entries. "
                "Your normal Waydroid is not touched.\n\nYou will be asked for an administrator password.")
        if running:
            body += " Running instances will be stopped."
        dlg = Adw.AlertDialog(heading="Uninstall Waydroid Multi?", body=body)
        purge = Gtk.CheckButton(label="Also delete all instances and their Android data")
        dlg.set_extra_child(purge)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("uninstall", "Uninstall")
        dlg.set_response_appearance("uninstall", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_default_response("cancel")

        def respond(_d, resp):
            if resp == "uninstall":
                self._uninstall_now(purge.get_active())
        dlg.connect("response", respond)
        dlg.present(self)

    def _uninstall_now(self, purge):
        argv = ["pkexec", UNINSTALL_SCRIPT] + (["--purge"] if purge else [])
        try:
            proc = Gio.Subprocess.new(argv, Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_MERGE)
        except GLib.Error as e:
            self.toast("Could not run the uninstaller: " + e.message)
            return
        self.toast("Uninstalling…")

        def done(p, res):
            try:
                _ok, out, _err = p.communicate_utf8_finish(res)
            except GLib.Error as e:
                out = e.message
            status = p.get_exit_status() if p.get_if_exited() else -1
            if status in (126, 127):     # pkexec: authentication cancelled or failed
                self.toast("Uninstall cancelled")
                return
            if status == 0:
                dlg = Adw.AlertDialog(heading="Waydroid Multi was uninstalled",
                                      body="Instances and their data were deleted." if purge else
                                      "Your instances were kept in /var/lib/waydroid-multi. Reinstall to "
                                      "use them again.")
                dlg.add_response("close", "Close")
                dlg.connect("response", lambda *_: self.get_application().quit())
            else:
                lines = (out or "").strip().splitlines()
                dlg = Adw.AlertDialog(heading="Uninstall failed",
                                      body="\n".join(lines[-8:]) or "exit status {}".format(status))
                dlg.add_response("close", "Close")
            dlg.present(self)
        proc.communicate_utf8_async(None, None, done)

    def delete(self, info):
        dlg = Adw.AlertDialog(heading="Delete “{}”?".format(info["name"]),
                              body="The instance and all of its Android data (apps, accounts, files) "
                                   "will be permanently removed.")
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("delete", "Delete")
        dlg.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_default_response("cancel")

        def respond(_d, resp):
            if resp == "delete":
                self._delete_now(info)
        dlg.connect("response", respond)
        dlg.present(self)

    def _delete_now(self, info):
        iid = info["id"]
        self.set_busy(iid, True)

        def ok(*_):
            desktop.remove_launcher(iid)
            self.busy.discard(iid)
            self.toast("Deleted “{}”".format(info["name"]))
            self.refresh()

        def fail(msg):
            self.set_busy(iid, False)
            self.toast(msg)
        self.backend.run_cli(["stop", iid], lambda *_: self.backend.call("Delete", iid, ok=ok, fail=fail))

    def install_apk(self, iid):
        def picked(dialog, res):
            try:
                gfile = dialog.open_finish(res)
            except GLib.Error:
                return
            path = gfile.get_path()
            name = os.path.basename(path)
            self.toast("Installing {}…".format(name), timeout=8)

            def then(ok):
                if ok:
                    self.toast("Installed {}".format(name))
            self._cli(iid, ["app", "install", iid, path], "Install failed", then=then)
        apk_dialog("Install APK").open(self, None, picked)

    def toggle_launcher(self, info):
        if os.path.exists(desktop.launcher_path(info["id"])):
            desktop.remove_launcher(info["id"])
            self.toast("Removed “{}” from the app grid".format(info["name"]))
        else:
            desktop.write_launcher(info["id"], info["name"])
            self.toast("Added “{}” to the app grid".format(info["name"]))
