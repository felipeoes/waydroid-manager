# SPDX-License-Identifier: GPL-3.0-or-later
"""Create / clone / settings dialogs."""
import os

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Pango", "1.0")
from gi.repository import Adw, GLib, Gtk, Pango  # noqa: E402

from .. import catalog, devices  # noqa: E402
from ..instance import host_memory_bytes, setting_default  # noqa: E402

# LDPlayer-style presets: (width, height, dpi)
TABLET = [(960, 540, 160), (1280, 720, 240), (1600, 900, 240), (1920, 1080, 280), (2560, 1440, 360)]
PHONE = [(540, 960, 240), (720, 1280, 320), (900, 1600, 320), (1080, 1920, 440), (1440, 2560, 560)]
ACTIONS = [("stop", "Stop the instance"), ("freeze", "Freeze (pause)"), ("none", "Keep running")]
IDLE = [("freeze", "Freeze (pause)"), ("none", "Keep running"), ("stop", "Stop the instance")]
ARM = [("houdini", "Houdini"), ("libndk", "libndk"), ("none", "Off")]
ANDROID = [(k, catalog.label(k)) for k in catalog.VERSIONS]
ROOT_VERSIONS = ("11", "13")    # Magisk Delta works there only


def cpu_options():
    n = os.cpu_count() or 2
    return [(str(c), "{} core{}".format(c, "s" if c > 1 else "")) for c in (1, 2, 3, 4, 6, 8) if c <= n]


def memory_options():
    total = host_memory_bytes()
    opts = [("1G", 1), ("1536M", 1.5), ("2G", 2), ("3G", 3), ("4G", 4), ("6G", 6), ("8G", 8), ("12G", 12),
            ("16G", 16)]
    return [(v, "{:g} GB".format(gb)) for v, gb in opts if gb * 1024 ** 3 <= total]


def resolution_presets(kind):
    """[(label, width, height, dpi)] for 'phone' or 'tablet'. Windows are zoomed to fit
    the screen, so every preset is usable on any monitor."""
    presets = TABLET if kind == "tablet" else PHONE
    return [("{} × {} · {} dpi".format(w, h, d), w, h, d) for w, h, d in presets]


def classify(width, height, dpi):
    """Which form factor/preset index an existing size belongs to."""
    for kind in ("tablet", "phone"):
        for i, (_, w, h, d) in enumerate(resolution_presets(kind)):
            if (w, h, d) == (width, height, dpi):
                return kind, i
    return "custom", 0


def _wrapping(row):
    """Options wrap onto more lines instead of ending in "…" when they don't fit."""
    f = Gtk.SignalListItemFactory()
    f.connect("setup", lambda _f, item: item.set_child(
        Gtk.Label(wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, max_width_chars=22, xalign=0)))
    f.connect("bind", lambda _f, item: item.get_child().set_label(item.get_item().get_string()))
    row.set_factory(f)
    return row


def _combo(title, options, subtitle=None):
    row = _wrapping(Adw.ComboRow(title=title))
    if subtitle:
        row.set_subtitle(subtitle)
    row.set_model(Gtk.StringList.new([label for _, label in options]))
    return row


def _select(row, options, value):
    for i, (v, _) in enumerate(options):
        if v == value:
            row.set_selected(i)
            return True
    return False


def _with_current(options, value, fmt):
    """Keep an existing non-preset value selectable."""
    if value and value not in dict(options):
        return options + [(value, fmt(value))]
    return options


def _spin(title, lo, hi, step, value, digits=0, subtitle=None):
    adj = Gtk.Adjustment(lower=lo, upper=hi, step_increment=step, page_increment=step * 10, value=value)
    row = Adw.SpinRow(title=title, adjustment=adj, digits=digits)
    if subtitle:
        row.set_subtitle(subtitle)
    return row


class _Dialog(Adw.Dialog):
    def _frame(self, title, submit_label):
        self.set_title(title)
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(False)
        header.set_show_start_title_buttons(False)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        header.pack_start(cancel)
        self.submit = Gtk.Button(label=submit_label, css_classes=["suggested-action"])
        self.submit.connect("clicked", self._on_submit)
        header.pack_end(self.submit)
        view.add_top_bar(header)
        page = Adw.PreferencesPage()
        view.set_content(page)
        self.set_child(view)
        return page


class InstanceDialog(_Dialog):
    """mode: 'create' (fresh instance from the stock image) or 'edit'."""

    def __init__(self, mode, on_submit, info=None):
        super().__init__()
        self.mode = mode
        self.on_submit = on_submit
        self.info = info or {}
        self.set_content_width(540)
        self.set_content_height(760)
        page = self._frame("New Instance" if mode == "create" else
                           "#{} {} — Settings".format(self.info.get("id", ""), self.info.get("name", "")),
                           "Create" if mode == "create" else "Save")

        # -- general
        g = Adw.PreferencesGroup(title="General")
        page.add(g)
        self.name_row = Adw.EntryRow(title="Name")
        self.name_row.set_text(self.info.get("name", ""))
        g.add(self.name_row)
        if mode == "create":
            g.set_description("A fresh Android device; it gets the next free number. "
                              "To copy an existing instance, use Clone.")
            self.android_row = _combo("Android version", ANDROID)
            _select(self.android_row, ANDROID, catalog.DEFAULT)
            self.android_row.connect("notify::selected", lambda *_: self._android_changed())
            g.add(self.android_row)
        else:
            g.add(Adw.ActionRow(title="Android version", subtitle=catalog.label(self.info["android"])
                                if self.info.get("android") in catalog.VERSIONS else "Stock Waydroid's own"))

        # -- display
        g = Adw.PreferencesGroup(title="Display", description="Takes effect at the next start")
        page.add(g)
        cw = int(self.info.get("width") or setting_default("width"))
        ch = int(self.info.get("height") or setting_default("height"))
        cdpi = int(self.info.get("dpi") or setting_default("dpi"))
        kind, idx = classify(cw, ch, cdpi)
        type_row = Adw.ActionRow(title="Device type")
        self.kind = Adw.ToggleGroup(valign=Gtk.Align.CENTER)
        for name, label in (("phone", "Phone"), ("tablet", "Tablet"), ("custom", "Custom")):
            self.kind.add(Adw.Toggle(name=name, label=label))
        type_row.add_suffix(self.kind)
        g.add(type_row)
        self.res_row = _wrapping(Adw.ComboRow(title="Resolution"))
        g.add(self.res_row)
        self.width_row = _spin("Width", 240, 7680, 2, cw)
        self.height_row = _spin("Height", 240, 7680, 2, ch)
        self.dpi_row = _spin("Density (DPI)", 80, 640, 10, cdpi)
        for r in (self.width_row, self.height_row, self.dpi_row):
            g.add(r)
        self.kind.set_active_name(kind)
        self._kind_changed(select=idx)
        self.kind.connect("notify::active-name", lambda *_: self._kind_changed())

        # -- device
        g = Adw.PreferencesGroup(title="Device", description="What apps see as this device (next start)")
        page.add(g)
        self.dev_keys = list(devices.PRESETS)
        self.device_row = _wrapping(Adw.ComboRow(title="Device model"))
        self.device_row.set_model(Gtk.StringList.new([devices.label(k) for k in self.dev_keys]))
        cur = self.info.get("device_model", "waydroid")
        self.device_row.set_selected(self.dev_keys.index(cur) if cur in self.dev_keys else 0)
        g.add(self.device_row)
        self.custom_rows = {}
        for f in devices.FIELDS:
            r = Adw.EntryRow(title=f.capitalize())
            r.set_text(self.info.get("prop:ro.product.waydroid." + f, ""))
            g.add(r)
            self.custom_rows[f] = r
        self.device_row.connect("notify::selected", lambda *_: self._device_changed())
        self._device_changed()

        # -- performance
        g = Adw.PreferencesGroup(title="Performance", description="Limits apply at the next start")
        page.add(g)
        cur = self.info.get("cpus") or setting_default("cpus")
        self.cpu_opts = _with_current(cpu_options(), cur, lambda v: "{} cores".format(v))
        self.cpu_row = _combo("CPU", self.cpu_opts)
        _select(self.cpu_row, self.cpu_opts, cur)
        g.add(self.cpu_row)
        cur = self.info.get("memory") or setting_default("memory")
        self.mem_opts = _with_current(memory_options(), cur, lambda v: v.replace("G", " GB").replace("M", " MB"))
        self.mem_row = _combo("Memory", self.mem_opts)
        _select(self.mem_row, self.mem_opts, cur)
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
        self.writable_row = Adw.SwitchRow(title="Writable system",
                                          subtitle="Lets Android change its system files. "
                                                   "Restart the instance to apply.")
        self.writable_row.set_active(self.info.get("system_writable", "false") == "true")
        g.add(self.writable_row)
        self.root_row = Adw.SwitchRow(title="Root",
                                      subtitle="Installs Magisk Delta; needs internet the first time. "
                                               "Restart the instance to apply.")
        self.root_row.set_active(self.info.get("root", "false") == "true")
        g.add(self.root_row)
        self.arm_row = _combo("ARM translation", ARM, subtitle="Runs ARM-only apps; needs internet the first "
                                                              "time. Restart the instance to apply.")
        _select(self.arm_row, ARM, self.info.get("arm_translation", "houdini"))
        g.add(self.arm_row)
        self._android_changed()
        if mode == "edit":
            self.apps_row = Adw.SwitchRow(title="App shortcuts in the app grid",
                                          subtitle="Create launchers for this instance's apps")
            self.apps_row.set_active(self.info.get("desktop_apps", "false") == "true")
            g.add(self.apps_row)

        # -- properties (edit only)
        if mode == "edit":
            self.prop_group = Adw.PreferencesGroup(title="Android properties",
                                                   description="Overrides for vendor/waydroid.prop (next start)")
            add = Gtk.Button(icon_name="list-add-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Add property",
                             css_classes=["flat"])
            add.connect("clicked", lambda *_: self._add_prop_row("", ""))
            self.prop_group.set_header_suffix(add)
            page.add(self.prop_group)
            self.prop_rows = []
            self.orig_props = {k[5:]: v for k, v in self.info.items()
                               if k.startswith("prop:") and not k.startswith("prop:ro.product.waydroid.")}
            for k, v in sorted(self.orig_props.items()):
                self._add_prop_row(k, v)

    # -- helpers
    def _add_prop_row(self, key, value):
        box = Gtk.Box(spacing=6, margin_top=6, margin_bottom=6, margin_start=12, margin_end=6)
        k = Gtk.Entry(text=key, placeholder_text="ro.some.property", hexpand=True)
        v = Gtk.Entry(text=value, placeholder_text="value", hexpand=True)
        rm = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text="Remove", css_classes=["flat"])
        row = Gtk.ListBoxRow(activatable=False, child=box)
        box.append(k)
        box.append(v)
        box.append(rm)
        entry = (row, k, v)
        rm.connect("clicked", lambda *_: (self.prop_group.remove(row), self.prop_rows.remove(entry)))
        self.prop_group.add(row)
        self.prop_rows.append(entry)

    def _kind_changed(self, select=None):
        kind = self.kind.get_active_name() or "tablet"
        custom = kind == "custom"
        self.res_row.set_visible(not custom)
        for r in (self.width_row, self.height_row, self.dpi_row):
            r.set_visible(custom)
        if not custom:
            self.presets = resolution_presets(kind)
            self.res_row.set_model(Gtk.StringList.new([p[0] for p in self.presets]))
            if select is None:
                select = 1   # 1280x720 tablet / 720x1280 phone
            self.res_row.set_selected(min(select, len(self.presets) - 1))

    def _device_changed(self):
        custom = self.dev_keys[self.device_row.get_selected()] == "custom"
        for r in self.custom_rows.values():
            r.set_visible(custom)

    def android(self):
        return ANDROID[self.android_row.get_selected()][0] if self.mode == "create" else self.info.get("android")

    def _android_changed(self):
        key = self.android()
        ok = key in ROOT_VERSIONS
        self.root_row.set_sensitive(ok)
        if not ok:
            self.root_row.set_active(False)
        if self.mode == "create":
            self.android_row.set_subtitle("About {:.1f} GB to download the first time".format(catalog.get(key, "gb")))

    def values(self):
        v = {}
        name = self.name_row.get_text().strip()
        if name:
            v["name"] = name
        if self.mode == "create":
            v["android"] = self.android()
        if self.kind.get_active_name() == "custom":
            w, h, dpi = (int(self.width_row.get_value()), int(self.height_row.get_value()),
                         int(self.dpi_row.get_value()))
        else:
            _, w, h, dpi = self.presets[self.res_row.get_selected()]
        v["width"], v["height"], v["dpi"] = str(w), str(h), str(dpi)
        key = self.dev_keys[self.device_row.get_selected()]
        v["device_model"] = key
        if key == "custom":
            for f, r in self.custom_rows.items():
                v["prop:ro.product.waydroid." + f] = r.get_text().strip()
        v["cpus"] = self.cpu_opts[self.cpu_row.get_selected()][0]
        v["memory"] = self.mem_opts[self.mem_row.get_selected()][0]
        v["close_action"] = ACTIONS[self.close_row.get_selected()][0]
        v["idle_action"] = IDLE[self.idle_row.get_selected()][0]
        v["system_writable"] = "true" if self.writable_row.get_active() else "false"
        v["root"] = "true" if self.root_row.get_active() else "false"
        v["arm_translation"] = ARM[self.arm_row.get_selected()][0]
        if self.mode == "edit":
            v["desktop_apps"] = "true" if self.apps_row.get_active() else "false"
            seen = set()
            for _, k, val in self.prop_rows:
                pk = k.get_text().strip()
                if pk:
                    seen.add(pk)
                    if self.orig_props.get(pk) != val.get_text():
                        v["prop:" + pk] = val.get_text()
            for pk in self.orig_props:
                if pk not in seen:
                    v["prop:" + pk] = ""
        return v

    def _on_submit(self, *_):
        if self.mode == "create":
            self.on_submit(self.values())
        else:
            self.on_submit(self.info["id"], self.values())
        self.close()


class CloneDialog(_Dialog):
    """Copy an instance (or stock Waydroid) with its apps, data and settings."""

    def __init__(self, source, on_submit):
        super().__init__()
        self.source = source          # dict with id, name, state
        self.on_submit = on_submit
        self.set_content_width(460)
        page = self._frame("Clone “{}”".format(source["name"]), "Clone")
        what = "apps, accounts and data" if source["id"] == "0" else "apps, accounts, data and settings"
        g = Adw.PreferencesGroup(description="Creates a new instance (next free number) with a copy of the "
                                             "{} of “{}”.".format(what, source["name"]))
        page.add(g)
        self.name_row = Adw.EntryRow(title="Name")
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

    def _on_submit(self, *_):
        values = {"clone_from": self.source["id"],
                  "reset_ids": "true" if self.reset_row.get_active() else "false"}
        name = self.name_row.get_text().strip()
        if name:
            values["name"] = name
        self.on_submit(self.source, values)
        self.close()


def show_error(parent, msg):
    parent.add_toast(Adw.Toast(title=GLib.markup_escape_text(msg)))
