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

# (action, icon name, tooltip)
TITLE_BUTTONS = [("minimize", "window-minimize-symbolic"), ("close", "window-close-symbolic")]
TOOLBAR = [
    ("back", "go-previous-symbolic"),
    ("home", "go-home-symbolic"),
    ("recents", "view-paged-symbolic"),
    None,
    ("volume_up", "audio-volume-high-symbolic"),
    ("volume_down", "audio-volume-low-symbolic"),
    None,
    ("screenshot", "camera-photo-symbolic"),
    ("zoom_in", "zoom-in-symbolic"),
    ("zoom_out", "zoom-out-symbolic"),
    ("fullscreen", "view-fullscreen-symbolic"),
]
SEPARATOR_H = 9
GRIP_H = 24

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

def toolbar_layout(height):
    """[(action|None, y0, y1)] for the entries that fit, plus the resize grip at the bottom."""
    out = []
    y = 4
    limit = height - GRIP_H
    # Drop optional buttons from the end if the window is short
    entries = list(TOOLBAR)
    while entries and sum(SEPARATOR_H if e is None else BUTTON_H for e in entries) > limit - 4:
        entries.pop()
        while entries and entries[-1] is None:
            entries.pop()
    for e in entries:
        h = SEPARATOR_H if e is None else BUTTON_H
        out.append((None if e is None else e[0], y, y + h))
        y += h
    out.append(("resize", height - GRIP_H, height))
    return out


def hit_toolbar(x, y, height):
    for action, y0, y1 in toolbar_layout(height):
        if action and y0 <= y < y1:
            return action
    return None


def render_toolbar(height, scale, theme="dark", hover=None, pressed=None, fullscreen=False):
    t = THEMES.get(theme, THEMES["dark"])
    w = TOOLBAR_W
    surf, cr = _surface(w, height, scale)
    cr.set_source_rgb(*t["bg"])
    cr.paint()
    icons = dict(e for e in TOOLBAR if e)
    for action, y0, y1 in toolbar_layout(height):
        cy = (y0 + y1) / 2
        if action is None:
            cr.set_source_rgba(*t["sep"])
            cr.rectangle(10, cy - 0.5, w - 20, 1)
            cr.fill()
            continue
        if action == "resize":
            cr.set_source_rgba(*t["dim"])
            cr.set_line_width(1.2)
            for d in (6, 11, 16):
                cr.move_to(w - 6, y1 - 6 - d)
                cr.line_to(w - 6 - d, y1 - 6)
            cr.stroke()
            continue
        if action == hover:
            cr.set_source_rgba(*(t["press"] if action == pressed else t["hover"]))
            _rounded(cr, 4, y0 + 2, w - 8, y1 - y0 - 4, 6)
            cr.fill()
        if action in ("back", "home", "recents"):
            _draw_nav(cr, action, w / 2, cy, t["fg"])
            continue
        name = icons[action]
        if action == "fullscreen" and fullscreen:
            name = "view-restore-symbolic"
        _draw_icon(cr, name, w / 2, cy, 16, t["fg"])
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
