# SPDX-License-Identifier: GPL-3.0-or-later
"""Minimal Wayland wire-format helpers for the window proxy.

Only what the proxy needs: reading arguments from a message payload and
building messages. Argument types follow the Wayland wire format:
  i int32, u uint32, f fixed 24.8, s string, o object, n new_id, a array.
(fd arguments travel out of band and never appear in the payload.)
"""
import struct

HEADER = struct.Struct("=II")
MAX_MSG = 4096


class ProtocolError(Exception):
    pass


def fixed_to_float(v):
    return v / 256.0


def float_to_fixed(f):
    return int(round(f * 256.0))


class Reader:
    def __init__(self, payload):
        self.p = payload
        self.off = 0

    def _take(self, n):
        if self.off + n > len(self.p):
            raise ProtocolError("truncated message")
        b = self.p[self.off:self.off + n]
        self.off += n
        return b

    def u(self):
        return struct.unpack("=I", self._take(4))[0]

    def i(self):
        return struct.unpack("=i", self._take(4))[0]

    def f(self):
        return self.i()

    o = u
    n = u

    def s(self):
        n = self.u()
        if n == 0:
            return None
        raw = bytes(self._take((n + 3) & ~3))
        if n > len(raw) or raw[n - 1:n] != b"\0":
            raise ProtocolError("bad string")
        return raw[:n - 1].decode("utf-8", "replace")

    def a(self):
        n = self.u()
        raw = bytes(self._take((n + 3) & ~3))
        return raw[:n]


def _enc(kind, v):
    if kind in "uon":
        return struct.pack("=I", v & 0xffffffff)
    if kind in "if":
        return struct.pack("=i", v)
    if kind == "s":
        if v is None:
            return struct.pack("=I", 0)
        raw = v.encode("utf-8") + b"\0"
        return struct.pack("=I", len(raw)) + raw + b"\0" * ((-len(raw)) % 4)
    if kind == "a":
        return struct.pack("=I", len(v)) + v + b"\0" * ((-len(v)) % 4)
    raise ValueError(kind)


def msg(obj, opcode, sig="", *args):
    """Build a message: msg(obj, op, "iis", 1, 2, "x")."""
    if len(sig) != len(args):
        raise ValueError("signature/argument mismatch")
    payload = b"".join(_enc(k, v) for k, v in zip(sig, args))
    size = HEADER.size + len(payload)
    if size > MAX_MSG:
        raise ProtocolError("message too large")
    return HEADER.pack(obj, (size << 16) | opcode) + payload


def parse(stream_bytes):
    """Split a byte string into (obj, opcode, payload) tuples (tests/debugging)."""
    out = []
    pos = 0
    while pos + 8 <= len(stream_bytes):
        obj, w = HEADER.unpack_from(stream_bytes, pos)
        size = w >> 16
        out.append((obj, w & 0xffff, stream_bytes[pos + 8:pos + size]))
        pos += size
    return out


# -- protocol constants used by the proxy ---------------------------------------

# requests
WL_DISPLAY_SYNC, WL_DISPLAY_GET_REGISTRY = 0, 1
WL_REGISTRY_BIND = 0
WL_COMPOSITOR_CREATE_SURFACE, WL_COMPOSITOR_CREATE_REGION = 0, 1
WL_REGION_DESTROY = 0
WL_SURFACE_DESTROY, WL_SURFACE_ATTACH, WL_SURFACE_DAMAGE, WL_SURFACE_FRAME = 0, 1, 2, 3
WL_SURFACE_SET_OPAQUE_REGION, WL_SURFACE_SET_INPUT_REGION, WL_SURFACE_COMMIT = 4, 5, 6
WL_SURFACE_SET_BUFFER_SCALE, WL_SURFACE_DAMAGE_BUFFER = 8, 9
WL_SUBCOMPOSITOR_GET_SUBSURFACE = 1
WL_SUBSURFACE_DESTROY, WL_SUBSURFACE_SET_POSITION = 0, 1
WL_SUBSURFACE_PLACE_ABOVE, WL_SUBSURFACE_PLACE_BELOW, WL_SUBSURFACE_SET_DESYNC = 2, 3, 5
WP_VIEWPORTER_GET_VIEWPORT = 1
WP_VIEWPORT_DESTROY, WP_VIEWPORT_SET_SOURCE, WP_VIEWPORT_SET_DESTINATION = 0, 1, 2
XDG_WM_BASE_GET_XDG_SURFACE = 2
XDG_SURFACE_DESTROY, XDG_SURFACE_GET_TOPLEVEL, XDG_SURFACE_SET_WINDOW_GEOMETRY = 0, 1, 3
XDG_SURFACE_ACK_CONFIGURE = 4
XDG_TOPLEVEL_DESTROY, XDG_TOPLEVEL_SET_TITLE, XDG_TOPLEVEL_SET_APP_ID = 0, 2, 3
XDG_TOPLEVEL_MOVE, XDG_TOPLEVEL_RESIZE = 5, 6
XDG_TOPLEVEL_SET_MIN_SIZE = 8
XDG_TOPLEVEL_SET_MAXIMIZED, XDG_TOPLEVEL_UNSET_MAXIMIZED = 9, 10
XDG_TOPLEVEL_SET_FULLSCREEN, XDG_TOPLEVEL_UNSET_FULLSCREEN, XDG_TOPLEVEL_SET_MINIMIZED = 11, 12, 13
WL_SEAT_GET_POINTER, WL_SEAT_GET_KEYBOARD, WL_SEAT_GET_TOUCH = 0, 1, 2
WP_FRACTIONAL_SCALE_MANAGER_GET = 1
WL_DATA_DEVICE_MANAGER_CREATE_DATA_SOURCE, WL_DATA_DEVICE_MANAGER_GET_DATA_DEVICE = 0, 1
WL_DATA_DEVICE_SET_SELECTION = 1
WL_DATA_OFFER_RECEIVE, WL_DATA_OFFER_DESTROY = 1, 2
WL_SHM_CREATE_POOL = 0
WL_SHM_POOL_CREATE_BUFFER, WL_SHM_POOL_DESTROY = 0, 1
WL_BUFFER_DESTROY = 0
WP_SINGLE_PIXEL_CREATE_U32_RGBA = 1
WP_CURSOR_SHAPE_MANAGER_GET_POINTER = 1
WP_CURSOR_SHAPE_DEVICE_SET_SHAPE = 1

# events
WL_DISPLAY_EV_ERROR, WL_DISPLAY_EV_DELETE_ID = 0, 1
WL_REGISTRY_EV_GLOBAL, WL_REGISTRY_EV_GLOBAL_REMOVE = 0, 1
WL_CALLBACK_EV_DONE = 0
WL_OUTPUT_EV_MODE, WL_OUTPUT_EV_SCALE = 1, 3
WP_FRACTIONAL_SCALE_EV_PREFERRED = 0
XDG_SURFACE_EV_CONFIGURE = 0
XDG_TOPLEVEL_EV_CONFIGURE, XDG_TOPLEVEL_EV_CLOSE = 0, 1
WL_POINTER_EV_ENTER, WL_POINTER_EV_LEAVE, WL_POINTER_EV_MOTION, WL_POINTER_EV_BUTTON = 0, 1, 2, 3
WL_POINTER_EV_FRAME = 5
WL_KEYBOARD_EV_ENTER, WL_KEYBOARD_EV_KEY = 1, 3
WL_TOUCH_EV_DOWN, WL_TOUCH_EV_UP, WL_TOUCH_EV_MOTION, WL_TOUCH_EV_FRAME, WL_TOUCH_EV_CANCEL = 0, 1, 2, 3, 4
WL_DATA_DEVICE_EV_DATA_OFFER, WL_DATA_DEVICE_EV_ENTER, WL_DATA_DEVICE_EV_MOTION = 0, 1, 3
WL_DATA_DEVICE_EV_SELECTION = 5
WL_DATA_OFFER_EV_OFFER = 0
WL_DATA_SOURCE_EV_CANCELLED = 2

# enums
XDG_TOPLEVEL_STATE_MAXIMIZED, XDG_TOPLEVEL_STATE_FULLSCREEN, XDG_TOPLEVEL_STATE_RESIZING = 1, 2, 3
RESIZE_EDGE = {"top": 1, "bottom": 2, "left": 4, "top_left": 5, "bottom_left": 6, "right": 8,
               "top_right": 9, "bottom_right": 10}
CURSOR_SHAPE = {"default": 1, "pointer": 4, "move": 13, "e_resize": 18, "n_resize": 19, "ne_resize": 20,
                "nw_resize": 21, "s_resize": 22, "se_resize": 23, "sw_resize": 24, "w_resize": 25}
EDGE_CURSOR = {"top": "n_resize", "bottom": "s_resize", "left": "w_resize", "right": "e_resize",
               "top_left": "nw_resize", "top_right": "ne_resize", "bottom_left": "sw_resize",
               "bottom_right": "se_resize"}
BTN_LEFT = 0x110
WL_SHM_FORMAT_ARGB8888 = 0
