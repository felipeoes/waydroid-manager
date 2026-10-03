# SPDX-License-Identifier: GPL-3.0-or-later
"""Main window: the instance list."""
import os
import subprocess

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from ..session import desktop  # noqa: E402
from .backend import Backend  # noqa: E402
from .dialogs import InstanceDialog  # noqa: E402

ACTIVE = ("RUNNING", "FROZEN")
STATE_STYLE = {"RUNNING": "success", "FROZEN": "warning", "STOPPED": "dim-label"}
STATE_LABEL = {"RUNNING": "Running", "FROZEN": "Paused", "STOPPED": "Stopped", "STARTING": "Starting…",
               "STOPPING": "Stopping…", "CLONING": "Cloning…", "DELETING": "Deleting…"}


def describe(info):
    parts = [STATE_LABEL.get(info["state"], info["state"].title())]
    if info["state"] in ACTIVE:
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


class InstanceRow(Adw.ActionRow):
    def __init__(self, win, info):
        super().__init__()
        self.win = win
        self.info = info
        self.dot = Gtk.Label(label="●", valign=Gtk.Align.CENTER)
        self.add_prefix(self.dot)

        self.spinner = Adw.Spinner(valign=Gtk.Align.CENTER)
        self.spinner.set_visible(False)
        self.add_suffix(self.spinner)
        self.play = Gtk.Button(valign=Gtk.Align.CENTER)
        self.play.add_css_class("flat")
        self.play.connect("clicked", lambda *_: win.start_or_show(self.info["id"]))
        self.add_suffix(self.play)
        self.stop = Gtk.Button(icon_name="media-playback-stop-symbolic", valign=Gtk.Align.CENTER,
                               tooltip_text="Stop")
        self.stop.add_css_class("flat")
        self.stop.connect("clicked", lambda *_: win.stop(self.info["id"]))
        self.add_suffix(self.stop)

        menu = Gio.Menu()
        menu.append("Settings…", "row.settings")
        menu.append("Clone…", "row.clone")
        menu.append("Install APK…", "row.install")
        menu.append("Add/remove app grid launcher", "row.launcher")
        section = Gio.Menu()
        section.append("Delete…", "row.delete")
        menu.append_section(None, section)
        more = Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=menu, valign=Gtk.Align.CENTER,
                              tooltip_text="More")
        more.add_css_class("flat")
        self.add_suffix(more)

        group = Gio.SimpleActionGroup()
        for name, cb in (("settings", lambda: win.edit(self.info["id"])),
                         ("clone", lambda: win.new_instance(clone_from=self.info["id"])),
                         ("install", lambda: win.install_apk(self.info["id"])),
                         ("launcher", lambda: win.toggle_launcher(self.info)),
                         ("delete", lambda: win.delete(self.info))):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", lambda _a, _p, cb=cb: cb())
            group.add_action(a)
        self.insert_action_group("row", group)
        self.update(info)

    def update(self, info):
        self.info = info
        st = info["state"]
        self.set_title(GLib.markup_escape_text(info["name"]))
        self.set_subtitle(GLib.markup_escape_text(describe(info)))
        for c in ("success", "warning", "dim-label", "accent"):
            self.dot.remove_css_class(c)
        self.dot.add_css_class(STATE_STYLE.get(st, "accent"))
        busy = st in ("STARTING", "STOPPING", "CLONING", "DELETING") or info["id"] in self.win.busy
        self.spinner.set_visible(busy)
        self.play.set_visible(not busy)
        self.stop.set_visible(st in ACTIVE and not busy)
        if st in ACTIVE:
            self.play.set_icon_name("view-reveal-symbolic")
            self.play.set_tooltip_text("Show window")
        else:
            self.play.set_icon_name("media-playback-start-symbolic")
            self.play.set_tooltip_text("Start")


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
        new = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="New instance")
        new.connect("clicked", lambda *_: self.new_instance())
        header.pack_start(new)
        menu = Gio.Menu()
        menu.append("Start all", "app.start-all")
        menu.append("Stop all", "app.stop-all")
        menu.append("Sync images from stock Waydroid", "app.sync")
        about = Gio.Menu()
        about.append("About", "app.about")
        menu.append_section(None, about)
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, tooltip_text="Menu"))
        view.add_top_bar(header)
        self.banner = Adw.Banner()
        self.banner.connect("button-clicked", lambda *_: self.sync_images())
        view.add_top_bar(self.banner)

        self.stack = Gtk.Stack()
        self.empty = Adw.StatusPage(icon_name="waydroid", title="No instances yet",
                                    description="Create an instance to run another Android next to stock Waydroid.")
        btn = Gtk.Button(label="New Instance", halign=Gtk.Align.CENTER)
        btn.add_css_class("pill")
        btn.add_css_class("suggested-action")
        btn.connect("clicked", lambda *_: self.new_instance())
        self.empty.set_child(btn)
        self.stack.add_named(self.empty, "empty")

        page = Adw.PreferencesPage()
        self.group = Adw.PreferencesGroup(title="Instances")
        page.add(self.group)
        stock = Adw.PreferencesGroup(title="Stock Waydroid")
        self.stock_row = Adw.ActionRow(title="Default instance", subtitle="Managed by Waydroid itself")
        show = Gtk.Button(icon_name="view-reveal-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Show")
        show.add_css_class("flat")
        show.connect("clicked", lambda *_: subprocess.Popen(["waydroid", "show-full-ui"],
                                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        self.stock_row.add_suffix(show)
        stock.add(self.stock_row)
        page.add(stock)
        self.stack.add_named(page, "list")

        self.error_page = Adw.StatusPage(icon_name="dialog-error-symbolic", title="Daemon not available",
                                         description="Start it with: sudo systemctl start waydroid-multi")
        retry = Gtk.Button(label="Retry", halign=Gtk.Align.CENTER)
        retry.add_css_class("pill")
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
        self.backend.call("List", ok=self._got_list, fail=self._list_failed, timeout=30)
        self.backend.call("GetInfo", ok=self._got_info, fail=lambda m: None, timeout=30)

    def _list_failed(self, msg):
        self.stack.set_visible_child_name("error")

    def _got_info(self, info):
        warn = info.get("warnings", "")
        if "images sync" in warn:
            self.banner.set_title("Stock Waydroid has newer images.")
            self.banner.set_button_label("Sync images")
            self.banner.set_revealed(True)
        else:
            self.banner.set_revealed(False)

    def _got_list(self, items):
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
        self.stack.set_visible_child_name("list" if self.instances else "empty")
        self._update_stock()

    def _update_stock(self):
        try:
            launcher = Gio.SubprocessLauncher.new(Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_SILENCE)
            proc = launcher.spawnv(["waydroid", "status"])
        except GLib.Error:
            return

        def done(p, res):
            try:
                _, out, _ = p.communicate_utf8_finish(res)
            except GLib.Error:
                return
            state = "Stopped"
            for line in (out or "").splitlines():
                if line.startswith("Container:"):
                    state = STATE_LABEL.get(line.split(":", 1)[1].strip(), "Running")
            self.stock_row.set_subtitle("{} · managed by Waydroid itself".format(state))
        proc.communicate_utf8_async(None, None, done)

    def toast(self, msg, timeout=4):
        t = Adw.Toast(title=GLib.markup_escape_text(msg))
        t.set_timeout(timeout)
        self.toasts.add_toast(t)

    def set_busy(self, iid, on):
        (self.busy.add if on else self.busy.discard)(iid)
        row = self.rows.get(iid)
        if row:
            row.update(row.info)

    # -- actions -----------------------------------------------------------------
    def start_or_show(self, iid):
        self.set_busy(iid, True)

        def done(ok, out):
            self.set_busy(iid, False)
            if not ok:
                self.toast(out.splitlines()[-1] if out else "Failed to start " + iid)
            self.refresh()
        self.backend.run_cli(["start", iid], done)

    def stop(self, iid):
        self.set_busy(iid, True)

        def done(ok, out):
            self.set_busy(iid, False)
            if not ok:
                self.toast(out.splitlines()[-1] if out else "Failed to stop " + iid)
            self.refresh()
        self.backend.run_cli(["stop", iid], done)

    def start_all(self):
        for i in self.instances:
            if i["state"] == "STOPPED":
                self.start_or_show(i["id"])

    def stop_all(self):
        for i in self.instances:
            if i["state"] in ACTIVE:
                self.stop(i["id"])

    def new_instance(self, clone_from=None):
        if clone_from:
            info = next((i for i in self.instances if i["id"] == clone_from), None)
            if info and info["state"] != "STOPPED":
                self.toast("Stop '{}' before cloning it".format(info["name"]))
                return
        dlg = InstanceDialog("create", self.instances, self._create, clone_from=clone_from)
        dlg.present(self)

    def _create(self, iid, values):
        cloning = "clone_from" in values
        self.toast("Cloning into '{}'…".format(iid) if cloning else "Creating '{}'…".format(iid))

        def ok(*_):
            name = values.get("name") or iid
            if values.get("window_labels", "true") == "true":
                desktop.write_launcher(iid, name)
            msg = "Created '{}'".format(name)
            if cloning and values.get("reset_ids") == "true":
                msg += " — new device identity on first start"
            self.toast(msg)
            self.refresh()
        self.backend.call("Create", iid, values, ok=ok, fail=self.toast)

    def edit(self, iid):
        def got(info):
            InstanceDialog("edit", self.instances, self._save, info=info).present(self)
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
            self.toast("Installing {}…".format(os.path.basename(path)), timeout=8)
            self.set_busy(iid, True)

            def done(ok, out):
                self.set_busy(iid, False)
                self.toast("Installed {}".format(os.path.basename(path)) if ok
                           else (out.splitlines()[-1] if out else "Install failed"))
                self.refresh()
            self.backend.run_cli(["app", "install", iid, path], done)
        fd.open(self, None, picked)

    def toggle_launcher(self, info):
        if os.path.exists(desktop.launcher_path(info["id"])):
            desktop.remove_launcher(info["id"])
            self.toast("Removed launcher for '{}'".format(info["name"]))
        else:
            desktop.write_launcher(info["id"], info["name"])
            self.toast("Added '{}' to the app grid".format(info["name"]))

    def sync_images(self):
        self.toast("Syncing images from stock Waydroid…", timeout=10)
        self.backend.call("SyncImages", ok=lambda iid: (self.toast("Image set " + iid + " ready"), self.refresh()),
                          fail=self.toast)
