# SPDX-License-Identifier: GPL-3.0-or-later
"""Main window: the instance list."""
import os

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from ..session import desktop  # noqa: E402
from .backend import Backend  # noqa: E402
from .dialogs import CloneDialog, InstanceDialog  # noqa: E402

ACTIVE = ("RUNNING", "FROZEN")
BUSY = ("STARTING", "STOPPING", "CLONING", "DELETING")
STATE_STYLE = {"RUNNING": "success", "FROZEN": "warning", "STOPPED": "dim-label"}
STATE_LABEL = {"RUNNING": "Running", "FROZEN": "Paused", "STOPPED": "Stopped", "STARTING": "Starting…",
               "STOPPING": "Stopping…", "CLONING": "Cloning…", "DELETING": "Deleting…"}
STOCK = {"id": "default", "name": "Stock Waydroid"}


def describe(info):
    parts = [STATE_LABEL.get(info["state"], info["state"].title())]
    if info["state"] in ACTIVE and info.get("ip"):
        parts.append(info["ip"])
    if info.get("width", "0") != "0":
        size = "{}×{}".format(info["width"], info["height"])
        if info.get("dpi", "0") != "0":
            size += " @ {} dpi".format(info["dpi"])
        parts.append(size)
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

    def __init__(self, win, info):
        super().__init__()
        self.win = win
        self.info = info
        self.dot = Gtk.Label(label="●", valign=Gtk.Align.CENTER)
        self.add_prefix(self.dot)
        self.spinner = Adw.Spinner(valign=Gtk.Align.CENTER, visible=False)
        self.add_suffix(self.spinner)
        self.play = _flat_button("media-playback-start-symbolic", "Start", lambda: win.start_or_show(self.info))
        self.add_suffix(self.play)
        self.stop = _flat_button("media-playback-stop-symbolic", "Stop", lambda: win.stop(self.info))
        self.add_suffix(self.stop)
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
        super().__init__(win, info)
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
        danger = Gio.Menu()
        danger.append("Delete…", "row.delete")
        m.append_section(None, danger)
        return m

    def update(self, info):
        self.set_title(GLib.markup_escape_text(info["name"]))
        super().update(info)


class StockRow(BaseRow):
    """The stock Waydroid instance, controlled through Waydroid's own CLI."""

    def __init__(self, win):
        super().__init__(win, dict(STOCK, state="STOPPED"))
        self.set_title("Stock Waydroid")
        self.add_action("clone", lambda: win.clone(self.info))
        self.update(self.info)

    def menu(self):
        m = Gio.Menu()
        m.append("Clone…", "row.clone")
        return m

    def subtitle(self):
        return "{} · default instance, managed by Waydroid".format(STATE_LABEL.get(self.info["state"], "Stopped"))


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
        header.pack_start(_flat_button("list-add-symbolic", "New instance", self.new_instance))
        menu = Gio.Menu()
        menu.append("Start all", "app.start-all")
        menu.append("Stop all", "app.stop-all")
        about = Gio.Menu()
        about.append("About", "app.about")
        menu.append_section(None, about)
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, tooltip_text="Menu"))
        view.add_top_bar(header)

        self.stack = Gtk.Stack()
        page = Adw.PreferencesPage()
        self.group = Adw.PreferencesGroup(title="Instances")
        self.group.set_header_suffix(_flat_button("list-add-symbolic", "New instance", self.new_instance))
        page.add(self.group)
        self.empty_row = Adw.ActionRow(title="No instances yet",
                                       subtitle="Create one to run another Android next to stock Waydroid")
        new_btn = Gtk.Button(label="New Instance", valign=Gtk.Align.CENTER, css_classes=["suggested-action"])
        new_btn.connect("clicked", lambda *_: self.new_instance())
        self.empty_row.add_suffix(new_btn)
        self.group.add(self.empty_row)
        stock = Adw.PreferencesGroup(title="Default")
        self.stock_row = StockRow(self)
        stock.add(self.stock_row)
        page.add(stock)
        self.stack.add_named(page, "list")

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
        self._update_stock()

    def _got_list(self, items):
        self.stack.set_visible_child_name("list")
        self.instances = sorted(items, key=lambda i: int(i["index"]))
        ids = {i["id"] for i in self.instances}
        for iid in list(self.rows):
            if iid not in ids:
                self.group.remove(self.rows.pop(iid))
        for info in self.instances:
            row = self.rows.get(info["id"])
            if row:
                row.update(info)
            else:
                row = InstanceRow(self, info)
                self.rows[info["id"]] = row
                self.group.add(row)
        self.empty_row.set_visible(not self.instances)

    def _update_stock(self):
        from .. import stockctl
        if "default" not in self.busy:
            self.stock_row.update(dict(STOCK, state=stockctl.state()))

    def toast(self, msg, timeout=4):
        t = Adw.Toast(title=GLib.markup_escape_text(msg))
        t.set_timeout(timeout)
        self.toasts.add_toast(t)

    def row_for(self, iid):
        return self.stock_row if iid == "default" else self.rows.get(iid)

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

    # -- actions -----------------------------------------------------------------
    def start_or_show(self, info):
        self._cli(info["id"], ["start", info["id"]], "Failed to start " + info["name"])

    def stop(self, info):
        self._cli(info["id"], ["stop", info["id"]], "Failed to stop " + info["name"])

    def start_all(self):
        for i in self.instances:
            if i["state"] == "STOPPED":
                self.start_or_show(i)

    def stop_all(self):
        for i in self.instances:
            if i["state"] in ACTIVE:
                self.stop(i)

    def new_instance(self):
        InstanceDialog("create", self._create).present(self)

    def _create(self, iid, values):
        self.toast("Creating '{}'…".format(values.get("name") or iid))

        def ok(*_):
            if values.get("window_labels", "true") == "true":
                desktop.write_launcher(iid, values.get("name") or iid)
            self.toast("Created '{}'".format(values.get("name") or iid))
            self.refresh()
        self.backend.call("Create", iid, values, ok=ok, fail=self.toast)

    def clone(self, info):
        taken = {i["id"] for i in self.instances}
        CloneDialog(info, taken, self._clone).present(self)

    def _clone(self, source, iid, values):
        name = values.get("name") or iid

        def do_clone(ok=True):
            if not ok:
                return
            self.toast("Copying “{}” into “{}”…".format(source["name"], name), timeout=10)

            def done(*_):
                desktop.write_launcher(iid, name)
                msg = "Cloned into “{}”".format(name)
                if values.get("reset_ids") == "true":
                    msg += " — new device identity on first start"
                self.toast(msg, timeout=6)
                self.refresh()
            self.backend.call("Create", iid, values, ok=done, fail=self.toast)

        if source["state"] in ACTIVE:
            self._cli(source["id"], ["stop", source["id"]], "Could not stop " + source["name"], then=do_clone)
        else:
            do_clone()

    def edit(self, iid):
        def got(info):
            InstanceDialog("edit", self._save, info=info).present(self)
        self.backend.call("Get", iid, ok=got, fail=self.toast, timeout=30)

    def _save(self, iid, values):
        def ok(*_):
            info = next((i for i in self.instances if i["id"] == iid), {})
            if "name" in values and os.path.exists(desktop.launcher_path(iid)):
                desktop.write_launcher(iid, values["name"])
            msg = "Saved"
            if info.get("state") in ACTIVE:
                msg += " — restart the instance to apply"
            self.toast(msg)
            self.refresh()
        self.backend.call("SetConfig", iid, values, ok=ok, fail=self.toast, timeout=60)

    def delete(self, info):
        dlg = Adw.AlertDialog(heading="Delete “{}”?".format(info["name"]),
                              body="The instance and all of its Android data (apps, accounts, files) "
                                   "will be permanently removed.")
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("delete", "Delete")
        dlg.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_default_response("cancel")

        def respond(_d, resp):
            if resp != "delete":
                return
            iid = info["id"]
            self.set_busy(iid, True)

            def ok(*_):
                desktop.remove_launcher(iid)
                self.busy.discard(iid)
                self.toast("Deleted '{}'".format(info["name"]))
                self.refresh()

            def fail(msg):
                self.set_busy(iid, False)
                self.toast(msg)
            self.backend.run_cli(["stop", iid], lambda *_: self.backend.call("Delete", iid, ok=ok, fail=fail))
        dlg.connect("response", respond)
        dlg.present(self)

    def install_apk(self, iid):
        fd = Gtk.FileDialog(title="Install APK")
        f = Gtk.FileFilter()
        f.set_name("Android packages")
        f.add_pattern("*.apk")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(f)
        fd.set_filters(filters)

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
        fd.open(self, None, picked)

    def toggle_launcher(self, info):
        if os.path.exists(desktop.launcher_path(info["id"])):
            desktop.remove_launcher(info["id"])
            self.toast("Removed “{}” from the app grid".format(info["name"]))
        else:
            desktop.write_launcher(info["id"], info["name"])
            self.toast("Added “{}” to the app grid".format(info["name"]))
