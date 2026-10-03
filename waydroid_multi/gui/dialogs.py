# SPDX-License-Identifier: GPL-3.0-or-later
"""Create / clone / settings dialogs."""
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402

from ..instance import ID_RE  # noqa: E402

LANDSCAPE = [(1920, 1080), (1600, 900), (1280, 720), (1024, 576), (960, 540), (854, 480), (640, 360)]
PORTRAIT = [(1080, 1920), (900, 1600), (720, 1280), (540, 960), (450, 800), (405, 720), (360, 640)]
MEMORY = [("", "Unlimited"), ("2G", "2 GB"), ("3G", "3 GB"), ("4G", "4 GB"), ("6G", "6 GB"), ("8G", "8 GB")]
ACTIONS = [("stop", "Stop the instance"), ("freeze", "Freeze (pause)"), ("none", "Keep running")]
IDLE = [("freeze", "Freeze (pause)"), ("none", "Keep running"), ("stop", "Stop the instance")]


def monitor_area():
    """Logical size of the largest monitor, minus room for panels/decorations."""
    w, h = 1366, 768
    display = Gdk.Display.get_default()
    if display:
        mons = display.get_monitors()
        best = None
        for i in range(mons.get_n_items()):
            g = mons.get_item(i).get_geometry()
            if best is None or g.width * g.height > best[0] * best[1]:
                best = (g.width, g.height)
        if best:
            w, h = best
    return w - 40, h - 90


def size_presets():
    """[(label, width, height)] that fit the screen; (0, 0) = Android default."""
    aw, ah = monitor_area()
    out = [("Automatic (fills the screen)", 0, 0)]
    for w, h in LANDSCAPE:
        if w <= aw and h <= ah:
            out.append(("Landscape {}×{}".format(w, h), w, h))
    fit_h = ah - ah % 2
    fit_w = (fit_h * 9 // 16) & ~1
    portrait = [(w, h) for w, h in PORTRAIT if w <= aw and h <= ah]
    if (fit_w, fit_h) not in portrait:
        portrait.insert(0, (fit_w, fit_h))
    for w, h in portrait:
        out.append(("Portrait {}×{}".format(w, h), w, h))
    out.append(("Custom", -1, -1))
    return out


def _combo(title, options, subtitle=None):
    row = Adw.ComboRow(title=title)
    if subtitle:
        row.set_subtitle(subtitle)
    row.set_model(Gtk.StringList.new([label for _, label in options]))
    return row


def _select(row, options, value):
    for i, (v, _) in enumerate(options):
        if v == value:
            row.set_selected(i)
            return


def _spin(title, lo, hi, step, value, digits=0, subtitle=None):
    adj = Gtk.Adjustment(lower=lo, upper=hi, step_increment=step, page_increment=step * 10, value=value)
    row = Adw.SpinRow(title=title, adjustment=adj, digits=digits)
    if subtitle:
        row.set_subtitle(subtitle)
    return row


class InstanceDialog(Adw.Dialog):
    """mode: 'create' (fresh instance from the stock image) or 'edit'."""

    def __init__(self, mode, on_submit, info=None):
        super().__init__()
        self.mode = mode
        self.on_submit = on_submit
        self.info = info or {}
        self.set_content_width(520)
        self.set_content_height(720)
        self.set_title("New Instance" if mode == "create" else "Settings — " + self.info.get("name", ""))

        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(False)
        header.set_show_start_title_buttons(False)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        header.pack_start(cancel)
        self.submit = Gtk.Button(label="Create" if mode == "create" else "Save")
        self.submit.add_css_class("suggested-action")
        self.submit.connect("clicked", self._on_submit)
        header.pack_end(self.submit)
        view.add_top_bar(header)

        self.toast = Adw.ToastOverlay()
        page = Adw.PreferencesPage()
        self.toast.set_child(page)
        view.set_content(self.toast)
        self.set_child(view)

        # -- general
        g = Adw.PreferencesGroup(title="General")
        page.add(g)
        if mode == "create":
            self.id_row = Adw.EntryRow(title="ID (a-z, 0-9, _)")
            self.id_row.connect("changed", self._validate)
            g.add(self.id_row)
        self.name_row = Adw.EntryRow(title="Display name")
        self.name_row.set_text(self.info.get("name", ""))
        g.add(self.name_row)
        if mode == "create":
            g.set_description("A fresh Android from the stock Waydroid image. "
                              "To copy an existing instance, use Clone instead.")

        # -- display
        g = Adw.PreferencesGroup(title="Display", description="Takes effect at the next start")
        page.add(g)
        self.presets = size_presets()
        self.size_row = Adw.ComboRow(title="Window size")
        self.size_row.set_model(Gtk.StringList.new([p[0] for p in self.presets]))
        self.size_row.connect("notify::selected", self._size_changed)
        g.add(self.size_row)
        cw, ch = int(self.info.get("width", "0")), int(self.info.get("height", "0"))
        self.width_row = _spin("Width", 240, 7680, 2, cw or 960)
        self.height_row = _spin("Height", 240, 7680, 2, ch or 540)
        g.add(self.width_row)
        g.add(self.height_row)
        idx = len(self.presets) - 1
        for i, (_, w, h) in enumerate(self.presets):
            if (w, h) == (cw, ch):
                idx = i
        self.size_row.set_selected(idx)
        self._size_changed()
        self.dpi_row = _spin("Density (DPI)", 0, 640, 10, int(self.info.get("dpi", "0")),
                             subtitle="0 = automatic")
        g.add(self.dpi_row)

        # -- performance
        g = Adw.PreferencesGroup(title="Performance", description="Limits apply at the next start")
        page.add(g)
        cpus = self.info.get("cpus", "") or "0"
        self.cpu_row = _spin("CPU cores", 0, 64, 0.5, float(cpus), digits=1, subtitle="0 = unlimited")
        g.add(self.cpu_row)
        self.mem_row = _combo("Memory (soft limit)", MEMORY)
        _select(self.mem_row, MEMORY, self.info.get("memory", ""))
        g.add(self.mem_row)

        # -- behavior
        g = Adw.PreferencesGroup(title="Behavior")
        page.add(g)
        self.close_row = _combo("When the window is closed", ACTIONS)
        _select(self.close_row, ACTIONS, self.info.get("close_action", "stop"))
        g.add(self.close_row)
        self.idle_row = _combo("When Android goes idle", IDLE)
        _select(self.idle_row, IDLE, self.info.get("idle_action", "freeze"))
        g.add(self.idle_row)
        self.labels_row = Adw.SwitchRow(title="Label window with instance name",
                                        subtitle="Separate dock icon and title per instance")
        self.labels_row.set_active(self.info.get("window_labels", "true") == "true")
        g.add(self.labels_row)
        if mode == "edit":
            self.apps_row = Adw.SwitchRow(title="App shortcuts in the app grid",
                                          subtitle="Create launchers for this instance's apps")
            self.apps_row.set_active(self.info.get("desktop_apps", "false") == "true")
            g.add(self.apps_row)

        # -- properties (edit only)
        if mode == "edit":
            self.prop_group = Adw.PreferencesGroup(title="Android properties",
                                                   description="Overrides for vendor/waydroid.prop (next start)")
            add = Gtk.Button(icon_name="list-add-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Add property")
            add.add_css_class("flat")
            add.connect("clicked", lambda *_: self._add_prop_row("", ""))
            self.prop_group.set_header_suffix(add)
            page.add(self.prop_group)
            self.prop_rows = []
            self.orig_props = {k[5:]: v for k, v in self.info.items() if k.startswith("prop:")}
            for k, v in sorted(self.orig_props.items()):
                self._add_prop_row(k, v)
        self._validate()

    # -- helpers
    def _add_prop_row(self, key, value):
        box = Gtk.Box(spacing=6, margin_top=6, margin_bottom=6, margin_start=12, margin_end=6)
        k = Gtk.Entry(text=key, placeholder_text="ro.some.property", hexpand=True)
        v = Gtk.Entry(text=value, placeholder_text="value", hexpand=True)
        rm = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text="Remove")
        rm.add_css_class("flat")
        row = Gtk.ListBoxRow(activatable=False, child=box)
        box.append(k)
        box.append(v)
        box.append(rm)
        entry = (row, k, v)
        rm.connect("clicked", lambda *_: (self.prop_group.remove(row), self.prop_rows.remove(entry)))
        self.prop_group.add(row)
        self.prop_rows.append(entry)

    def _size_changed(self, *_):
        custom = self.presets[self.size_row.get_selected()][1] == -1
        self.width_row.set_visible(custom)
        self.height_row.set_visible(custom)

    def _validate(self, *_):
        ok = True
        if self.mode == "create":
            text = self.id_row.get_text()
            ok = bool(ID_RE.match(text)) and text != "default"
            if text and not ok:
                self.id_row.add_css_class("error")
            else:
                self.id_row.remove_css_class("error")
        self.submit.set_sensitive(ok)

    def values(self):
        v = {}
        name = self.name_row.get_text().strip()
        if name:
            v["name"] = name
        _, w, h = self.presets[self.size_row.get_selected()]
        if w == -1:
            w, h = int(self.width_row.get_value()), int(self.height_row.get_value())
        v["width"], v["height"] = str(w), str(h)
        v["dpi"] = str(int(self.dpi_row.get_value()))
        cpus = self.cpu_row.get_value()
        v["cpus"] = ("%g" % cpus) if cpus > 0 else ""
        v["memory"] = MEMORY[self.mem_row.get_selected()][0]
        v["close_action"] = ACTIONS[self.close_row.get_selected()][0]
        v["idle_action"] = IDLE[self.idle_row.get_selected()][0]
        v["window_labels"] = "true" if self.labels_row.get_active() else "false"
        if self.mode == "edit":
            v["desktop_apps"] = "true" if self.apps_row.get_active() else "false"
            seen = set()
            for _, k, val in self.prop_rows:
                key = k.get_text().strip()
                if key:
                    seen.add(key)
                    if self.orig_props.get(key) != val.get_text():
                        v["prop:" + key] = val.get_text()
            for key in self.orig_props:
                if key not in seen:
                    v["prop:" + key] = ""
        return v

    def _on_submit(self, *_):
        v = self.values()
        if self.mode == "create":
            self.on_submit(self.id_row.get_text(), v)
        else:
            self.on_submit(self.info["id"], v)
        self.close()

    def show_error(self, msg):
        self.toast.add_toast(Adw.Toast(title=GLib.markup_escape_text(msg)))


def suggest_id(base, taken):
    base = "".join(c if (c.isalnum() and c.isascii()) or c == "_" else "_" for c in base.lower()).strip("_")
    if not base or not base[0].isalpha():
        base = "copy_" + base
    base = base[:26]
    for n in range(2, 1000):
        cand = "{}_{}".format(base, n)
        if cand not in taken:
            return cand
    return base


class CloneDialog(Adw.Dialog):
    """Copy an instance (or stock Waydroid) with its apps, data and settings."""

    def __init__(self, source, taken_ids, on_submit):
        super().__init__()
        self.source = source          # dict with id, name, state
        self.on_submit = on_submit
        self.set_content_width(460)
        self.set_title("Clone “{}”".format(source["name"]))

        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(False)
        header.set_show_start_title_buttons(False)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        header.pack_start(cancel)
        self.submit = Gtk.Button(label="Clone")
        self.submit.add_css_class("suggested-action")
        self.submit.connect("clicked", self._on_submit)
        header.pack_end(self.submit)
        view.add_top_bar(header)

        page = Adw.PreferencesPage()
        what = "apps, accounts and data" if source["id"] == "default" else "apps, accounts, data and settings"
        g = Adw.PreferencesGroup(description="Creates a new instance with a copy of the {} of “{}”."
                                 .format(what, source["name"]))
        page.add(g)
        self.id_row = Adw.EntryRow(title="New ID (a-z, 0-9, _)")
        self.id_row.set_text(suggest_id(source["id"] if source["id"] != "default" else "stock", taken_ids))
        self.id_row.connect("changed", self._validate)
        g.add(self.id_row)
        self.name_row = Adw.EntryRow(title="Display name")
        self.name_row.set_text("{} (copy)".format(source["name"]))
        g.add(self.name_row)
        self.reset_row = Adw.SwitchRow(title="New device identity",
                                       subtitle="New Android ID and Google services ID, so the copy "
                                                "counts as a separate device")
        self.reset_row.set_active(True)
        g.add(self.reset_row)
        if source.get("state") in ("RUNNING", "FROZEN"):
            note = Adw.PreferencesGroup()
            row = Adw.ActionRow(title="“{}” is running".format(source["name"]),
                                subtitle="It will be stopped before copying.")
            row.add_prefix(Gtk.Image(icon_name="dialog-warning-symbolic"))
            note.add(row)
            page.add(note)
        view.set_content(page)
        self.set_child(view)
        self._validate()

    def _validate(self, *_):
        text = self.id_row.get_text()
        ok = bool(ID_RE.match(text)) and text != "default"
        (self.id_row.remove_css_class if ok or not text else self.id_row.add_css_class)("error")
        self.submit.set_sensitive(ok)

    def _on_submit(self, *_):
        values = {"clone_from": self.source["id"],
                  "reset_ids": "true" if self.reset_row.get_active() else "false"}
        name = self.name_row.get_text().strip()
        if name:
            values["name"] = name
        self.on_submit(self.source, self.id_row.get_text(), values)
        self.close()
