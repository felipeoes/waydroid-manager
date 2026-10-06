# SPDX-License-Identifier: GPL-3.0-or-later
"""Drawing and hit-testing for the instance window frame (title bar + toolbar).

Pure rendering: returns ARGB8888 pixel data (cairo FORMAT_ARGB32, which is
wl_shm ARGB8888 on little-endian hosts). The proxy owns the Wayland side.
"""
import glob
import os

TITLE_H = 32          # logical px
TOOLBAR_W = 40
BUTTON_H = 36
BORDER = 8            # invisible resize border around the window

# (action, icon name); None = drawn here
TITLE_BUTTONS = [("minimize", "window-minimize-symbolic"), ("close", "window-close-symbolic")]
TOOLBAR = [
    ("settings", "emblem-system-symbolic"),
    None,
    ("volume_up", "audio-volume-high-symbolic"),
    ("volume_down", "audio-volume-low-symbolic"),
    None,
    ("screenshot", "camera-photo-symbolic"),
    ("install", None),
    ("fullscreen", "view-fullscreen-symbolic"),
]
NAV = ["back", "home", "recents"]   # at the toolbar's bottom, like Android's navigation bar
TOOLTIPS = {"settings": "Settings", "back": "Back", "home": "Home", "recents": "Recent apps",
            "volume_up": "Volume up", "volume_down": "Volume down", "screenshot": "Screenshot",
            "install": "Install APK", "fullscreen": "Fullscreen (F11)"}
SEPARATOR_H = 9

THEMES = {
    "dark": {"bg": (0.19, 0.19, 0.19), "fg": (1, 1, 1, 0.92), "dim": (1, 1, 1, 0.55),
             "hover": (1, 1, 1, 0.10), "press": (1, 1, 1, 0.18), "close_hover": (0.88, 0.11, 0.14, 1),
             "sep": (1, 1, 1, 0.10)},
    "light": {"bg": (0.92, 0.92, 0.92), "fg": (0, 0, 0, 0.80), "dim": (0, 0, 0, 0.50),
              "hover": (0, 0, 0, 0.07), "press": (0, 0, 0, 0.14), "close_hover": (0.88, 0.11, 0.14, 1),
              "sep": (0, 0, 0, 0.10)},
}

_icon_cache = {}


def _icon_path(name):
    for base in ("/usr/share/icons/Adwaita/symbolic", "/usr/share/icons/Adwaita/scalable",
                 "/usr/share/icons/hicolor/symbolic", "/usr/share/icons/hicolor/scalable"):
        hits = glob.glob(os.path.join(base, "*", name + ".svg"))
        if hits:
            return hits[0]
    return None


def _icon_surface(name, px):
    """A cairo A8-ish surface of the icon at px×px (used as a mask), or None."""
    key = (name, px)
    if key in _icon_cache:
        return _icon_cache[key]
    surf = None
    path = _icon_path(name)
    if path:
        try:
            import cairo
            import gi
            gi.require_version("Rsvg", "2.0")
            from gi.repository import Rsvg
            handle = Rsvg.Handle.new_from_file(path)
            surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, px, px)
            cr = cairo.Context(surf)
            vp = Rsvg.Rectangle()
            vp.x, vp.y, vp.width, vp.height = 0, 0, px, px
            handle.render_document(cr, vp)
            surf.flush()
        except Exception:  # noqa: BLE001
            surf = None
    _icon_cache[key] = surf
    return surf


def _draw_icon(cr, name, cx, cy, size, rgba):
    surf = _icon_surface(name, int(size))
    cr.set_source_rgba(*rgba)
    if surf is not None:
        cr.mask_surface(surf, cx - size / 2, cy - size / 2)
    else:
        cr.arc(cx, cy, size / 4, 0, 6.2832)
        cr.fill()


def _surface(w, h, scale):
    import cairo
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, max(1, int(w * scale)), max(1, int(h * scale)))
    cr = cairo.Context(surf)
    cr.scale(scale, scale)
    return surf, cr


def _finish(surf):
    surf.flush()
    return bytes(surf.get_data()), surf.get_width(), surf.get_height(), surf.get_stride()


# -- title bar --------------------------------------------------------------------

def title_buttons(width):
    """[(action, x0, x1)] for the title bar buttons, right-aligned."""
    out = []
    x = width
    for action, _icon in reversed(TITLE_BUTTONS):
        out.insert(0, (action, x - TITLE_H, x))
        x -= TITLE_H
    return out


def hit_title(x, y, width):
    for action, x0, x1 in title_buttons(width):
        if x0 <= x < x1:
            return action
    return "move"


def render_title(width, scale, text, theme="dark", hover=None, pressed=None):
    t = THEMES.get(theme, THEMES["dark"])
    surf, cr = _surface(width, TITLE_H, scale)
    cr.set_source_rgb(*t["bg"])
    cr.paint()
    buttons = title_buttons(width)
    for action, x0, x1 in buttons:
        if action == hover:
            color = t["close_hover"] if action == "close" else (t["press"] if action == pressed else t["hover"])
            cr.set_source_rgba(*color)
            cr.arc((x0 + x1) / 2, TITLE_H / 2, 12, 0, 6.2832)
            cr.fill()
        icon_color = (1, 1, 1, 1) if (action == "close" and action == hover) else t["fg"]
        name = dict(TITLE_BUTTONS)[action]
        _draw_icon(cr, name, (x0 + x1) / 2, TITLE_H / 2, 16, icon_color)
    # Title text, ellipsized before the buttons
    try:
        import gi
        gi.require_version("Pango", "1.0")
        gi.require_version("PangoCairo", "1.0")
        from gi.repository import Pango, PangoCairo
        layout = PangoCairo.create_layout(cr)
        layout.set_font_description(Pango.FontDescription.from_string("Sans Bold 10"))
        layout.set_text(text, -1)
        layout.set_ellipsize(Pango.EllipsizeMode.END)
        avail = max(10, (buttons[0][1] if buttons else width) - 24)
        layout.set_width(int(avail * Pango.SCALE))
        _, logical = layout.get_pixel_extents()
        cr.set_source_rgba(*t["fg"])
        cr.move_to(12, (TITLE_H - logical.height) / 2)
        PangoCairo.show_layout(cr, layout)
    except Exception:  # noqa: BLE001
        cr.set_source_rgba(*t["fg"])
        cr.select_font_face("Sans")
        cr.set_font_size(13)
        cr.move_to(12, TITLE_H / 2 + 5)
        cr.show_text(text)
    return _finish(surf)


# -- toolbar ------------------------------------------------------------------------

def toolbar_layout(height, scroll=0):
    """[(action|None, y0, y1)]: the entries from the top, moved up by scroll, and the navigation
    buttons at the bottom. On a short window the entries run under the navigation buttons; up
    to toolbar_end they are shown and clickable, and scrolling brings the rest up."""
    y = 4 - scroll
    out = []
    for e in TOOLBAR:
        h = SEPARATOR_H if e is None else BUTTON_H
        out.append((None if e is None else e[0], y, y + h))
        y += h
    nav_top = toolbar_end(height) + 4
    return out + [(a, nav_top + i * BUTTON_H, nav_top + (i + 1) * BUTTON_H) for i, a in enumerate(NAV)]


def toolbar_end(height):
    """Where the scrolling entries stop, above the navigation buttons."""
    return height - 8 - BUTTON_H * len(NAV)


def toolbar_scroll_max(height):
    return max(0, 8 + sum(SEPARATOR_H if e is None else BUTTON_H for e in TOOLBAR) - toolbar_end(height))


def hit_toolbar(x, y, height, scroll=0):
    end = toolbar_end(height)
    for action, y0, y1 in toolbar_layout(height, scroll):
        if action and y0 <= y < y1 and (action in NAV or y < end):
            return action
    return None


def render_toolbar(height, scale, theme="dark", hover=None, pressed=None, fullscreen=False, scroll=0):
    import cairo
    t = THEMES.get(theme, THEMES["dark"])
    w = TOOLBAR_W
    end = toolbar_end(height)
    surf, cr = _surface(w, height, scale)
    cr.set_source_rgb(*t["bg"])
    cr.paint()
    icons = dict(e for e in TOOLBAR if e)
    for action, y0, y1 in toolbar_layout(height, scroll):
        cy = (y0 + y1) / 2
        cr.save()
        if action not in NAV:
            cr.rectangle(0, 0, w, end)
            cr.clip()
        if action is None:
            cr.set_source_rgba(*t["sep"])
            cr.rectangle(10, cy - 0.5, w - 20, 1)
            cr.fill()
        else:
            if action == hover:
                cr.set_source_rgba(*(t["press"] if action == pressed else t["hover"]))
                _rounded(cr, 4, y0 + 2, w - 8, y1 - y0 - 4, 6)
                cr.fill()
            if action in NAV:
                _draw_nav(cr, action, w / 2, cy, t["fg"])
            elif action == "install":
                _draw_apk(cr, w / 2, cy, t["fg"])
            else:
                name = "view-restore-symbolic" if action == "fullscreen" and fullscreen else icons[action]
                _draw_icon(cr, name, w / 2, cy, 16, t["fg"])
        cr.restore()
    # Entries cut off at either end fade out there: they scroll
    fades = ([(4, 4 + 14)] if scroll > 0 else []) + ([(end, end - 14)] if scroll < toolbar_scroll_max(height) else [])
    for y0, y1 in fades:
        g = cairo.LinearGradient(0, y0, 0, y1)
        g.add_color_stop_rgba(0, *t["bg"], 1)
        g.add_color_stop_rgba(1, *t["bg"], 0)
        cr.set_source(g)
        cr.rectangle(0, min(y0, y1) - 4, w, 18)
        cr.fill()
    return _finish(surf)


def _draw_nav(cr, action, cx, cy, rgba):
    """Android navigation bar glyphs: ◁ back, ○ home, □ recents (outlined)."""
    import math
    cr.set_source_rgba(*rgba)
    cr.set_line_width(1.6)
    cr.set_line_join(1)   # round
    if action == "back":
        cr.move_to(cx + 5, cy - 6)
        cr.line_to(cx - 6, cy)
        cr.line_to(cx + 5, cy + 6)
        cr.close_path()
    elif action == "home":
        cr.arc(cx, cy, 6.5, 0, 2 * math.pi)
    else:
        _rounded(cr, cx - 6, cy - 6, 12, 12, 1.5)
    cr.stroke()


def _draw_apk(cr, cx, cy, rgba):
    """Install APK: "APK" in an outlined box."""
    cr.set_source_rgba(*rgba)
    cr.set_line_width(1.4)
    _rounded(cr, cx - 11, cy - 7, 22, 14, 2.5)
    cr.stroke()
    _text(cr, "APK", cx, cy, 7.5, bold=True)


def _text(cr, text, cx, cy, size, bold=False):
    """Text centred on (cx, cy), in the current source colour."""
    import cairo
    cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD if bold else cairo.FONT_WEIGHT_NORMAL)
    cr.set_font_size(size)
    e = cr.text_extents(text)
    cr.move_to(cx - e.x_bearing - e.width / 2, cy - e.y_bearing - e.height / 2)
    cr.show_text(text)


# -- tooltip ------------------------------------------------------------------------

TIP_FONT = 12
TIP_PAD_X, TIP_PAD_Y = 8, 6


def tooltip_size(text):
    import cairo
    cr = cairo.Context(cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 1))
    cr.select_font_face("Sans")
    cr.set_font_size(TIP_FONT)
    fe = cr.font_extents()
    return int(cr.text_extents(text).x_advance + 2 * TIP_PAD_X + 0.99), int(fe[0] + fe[1] + 2 * TIP_PAD_Y + 0.99)


def render_tooltip(text, scale):
    """A hovered toolbar button's label, dark like GNOME's tooltips in either theme."""
    w, h = tooltip_size(text)
    surf, cr = _surface(w, h, scale)
    cr.set_source_rgba(0.1, 0.1, 0.1, 0.92)
    _rounded(cr, 0, 0, w, h, 6)
    cr.fill()
    cr.set_source_rgba(1, 1, 1, 1)
    _text(cr, text, w / 2, h / 2, TIP_FONT)
    return _finish(surf)


def _rounded(cr, x, y, w, h, r):
    import math
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


def border_edge(x, y, w, h, b=BORDER, corner=16):
    """Which resize edge a point on the invisible border surface (w×h) is on, or None."""
    near_l, near_r = x < b + corner, x >= w - b - corner
    near_t, near_b = y < b + corner, y >= h - b - corner
    if near_t and near_l:
        return "top_left"
    if near_t and near_r:
        return "top_right"
    if near_b and near_l:
        return "bottom_left"
    if near_b and near_r:
        return "bottom_right"
    if y < b:
        return "top"
    if y >= h - b:
        return "bottom"
    if x < b:
        return "left"
    if x >= w - b:
        return "right"
    return None
