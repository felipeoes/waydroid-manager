# SPDX-License-Identifier: GPL-3.0-or-later
"""Wayland proxy between an instance's hwcomposer (HWC) and the real compositor.

It gives every instance window an identity and a frame:

* **Labels**: ``xdg_toplevel.set_title/set_app_id`` become "Waydroid · <name>"
  and "waydroid-multi.<id>[.<pkg>]", so each instance is its own app in the dock.
* **Zoom**: Android keeps rendering at its resolution while the window is shown
  smaller or larger. The HWC's own viewport/subsurface/geometry requests are
  scaled and input coordinates are scaled back. The HWC scale is normalised
  to 1 (``wl_output.scale`` and fractional scale are rewritten), and configure
  sizes are hidden from it (any real size triggers an Android display hotplug).
* **Frame** (full-UI window only): a title bar (move, minimize, close), an
  attached side toolbar (Back, Home, Recents, volume, screenshot,
  fullscreen) and an invisible resize border. These are proxy-owned
  subsurfaces; events for them are never forwarded to the HWC, which aborts on
  unknown object ids.

Toolbar keys are delivered as synthetic ``wl_keyboard.key`` events (the HWC
forwards evdev codes and needs no focus; codes >= 239 are dropped, hence
Alt+Tab for Recents).

Run as a separate process (no gbinder here):
  python3 -m waydroid_multi.session.wlproxy --listen SOCK --upstream SOCK --id ID --name NAME \\
      [--width W --height H --zoom auto|PCT --frame on|off --theme dark|light --close-action stop|freeze|none]
Events are written to stdout, one per line: "ready", "close", "zoom <pct>", "action <name>".
"""
import argparse
import array
import collections
import errno
import math
import os
import selectors
import signal
import socket
import struct
import sys
import time

from . import frame as fr
from . import wlschema
from .wlproto import (HEADER, MAX_MSG, ProtocolError, Reader, msg)
from . import wlproto as P

MAX_FDS_PER_MSG = 28        # libwayland's MAX_FDS_OUT; receivers truncate beyond this
RECV_SIZE = 65536
HIGH_WATER = 4 * 1024 * 1024
FULL_UI_APP_ID = "Waydroid"
MY_ID_BASE = 0xfe000000      # proxy-created objects in client space (mapped to real ids by Translator)
FULL_DAMAGE = 0x7fffffff
ZOOM_MIN, ZOOM_MAX = 0.25, 2.0
PANEL_ALLOWANCE = 64          # room for a desktop panel when fitting to the screen


def read_string(payload, off):
    r = Reader(payload)
    r.off = off
    s = r.s()
    return s, r.off


def encode_string(s):
    return P._enc("s", s)


def build_message(obj, opcode, payload):
    size = HEADER.size + len(payload)
    if size > MAX_MSG:
        raise ProtocolError("message too large")
    return HEADER.pack(obj, (size << 16) | opcode) + payload


# -- stream plumbing ----------------------------------------------------------------

class Stream:
    """One direction of a proxied connection: frames, filters and queues output.

    The handler returns None (forward unchanged) or a list of byte strings
    (replacement messages; [] drops the message). Proxy-originated messages are
    added with inject(); those carrying fds wait until no partial message is
    buffered, so every fd stays ahead of (or with) the message that uses it.
    """

    def __init__(self, handler, post_feed=None, translate=None):
        self.handler = handler
        self.post_feed = post_feed
        self.translate = translate   # applied when a message is appended for sending
        self.inbuf = bytearray()
        self.out = bytearray()
        self.fds = []
        self.deferred = []
        self.feeding = False

    def feed(self, data, fds):
        self.fds.extend(fds)
        self.inbuf += data
        self.feeding = True
        try:
            self._process()
        finally:
            self.feeding = False
        self._drain_deferred()
        if self.post_feed:
            self.post_feed()

    def _process(self):
        buf = self.inbuf
        pos = 0
        while len(buf) - pos >= HEADER.size:
            obj, word = HEADER.unpack_from(buf, pos)
            size = word >> 16
            if size < HEADER.size or size % 4:
                raise ProtocolError("bad message size {}".format(size))
            if len(buf) - pos < size:
                break
            payload = bytes(buf[pos + HEADER.size:pos + size])
            repl = self.handler(obj, word & 0xffff, payload)
            if repl is None:
                self._append(bytes(buf[pos:pos + size]))
            else:
                for m in repl:
                    self._append(m)
            pos += size
        if pos:
            del self.inbuf[:pos]

    def inject(self, data, fds=()):
        """Queue a proxy-originated message.

        While this stream is being processed, or behind anything already
        deferred, or (for fd-carrying messages) while a partial message is
        buffered, it waits: messages processed later in the same read may own
        fds that are already queued, and fds must stay in message order.
        """
        if self.feeding or self.deferred or (fds and self.inbuf):
            self.deferred.append((data, list(fds)))
            if not self.feeding:
                self._drain_deferred()
            return
        self._append(data, fds)

    def _append(self, data, fds=()):
        if self.translate is not None:
            data = self.translate(data)
            if data is None:
                for fd in fds:
                    os.close(fd)
                return
        self.fds.extend(fds)
        self.out += data

    def _drain_deferred(self):
        """Send deferred messages in order. Only a message carrying fds must wait
        for an empty inbuf (a partial message may own fds already queued)."""
        while self.deferred:
            data, fds = self.deferred[0]
            if fds and self.inbuf:
                break
            self.deferred.pop(0)
            self._append(data, fds)

    def pending(self):
        # fds alone cannot be sent: they ride on the bytes that follow them
        return bool(self.out)


def recv_with_fds(sock):
    fds = array.array("i")
    data, ancdata, flags, _ = sock.recvmsg(RECV_SIZE, socket.CMSG_SPACE(253 * fds.itemsize))
    for level, ctype, cdata in ancdata:
        if level == socket.SOL_SOCKET and ctype == socket.SCM_RIGHTS:
            usable = len(cdata) - (len(cdata) % fds.itemsize)
            fds.frombytes(cdata[:usable])
    if flags & socket.MSG_CTRUNC:
        for fd in fds:
            os.close(fd)
        raise ProtocolError("file descriptors truncated")
    return data, list(fds)


def flush(sock, stream):
    """Write as much queued output as possible. Returns True when drained."""
    while stream.out:
        fds = stream.fds[:MAX_FDS_PER_MSG]
        # With more fds than one message may carry, send one byte per batch
        chunk = bytes(stream.out[:1] if len(stream.fds) > MAX_FDS_PER_MSG else stream.out[:RECV_SIZE])
        anc = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", fds))] if fds else []
        try:
            n = sock.sendmsg([chunk], anc)
        except (BlockingIOError, InterruptedError):
            return False
        for fd in fds:
            os.close(fd)
        del stream.fds[:len(fds)]
        del stream.out[:n]
    return True


# -- object id translation -----------------------------------------------------------

SERVER_ID_START = 0xff000000
HIDDEN = ()      # Translator.event result for a deliberately hidden global


class Translator:
    """Maps object ids between the HWC's id space ("client space", which also
    holds the proxy's own objects at MY_ID_BASE+) and the compositor's.

    libwayland-server only accepts new client ids that extend its map
    contiguously, so every object gets a compositor id allocated here, in the
    order messages are actually sent. Server-created ids (>= 0xff000000) are
    the same in both spaces. Globals whose protocol is unknown are hidden from
    the HWC, so every message it can send is translatable.
    """

    def __init__(self, schema, log=None):
        self.schema = schema
        self.c2s = {1: 1}
        self.s2c = {1: 1}
        self.iface = {1: "wl_display"}       # client-space id -> interface
        self.top = 2                          # next never-used compositor id
        self.free = []                        # compositor ids released by delete_id
        self.hidden = set()                   # hidden global names
        self.log = log or (lambda m: None)

    def _alloc(self):
        if self.free:
            return self.free.pop()
        i = self.top
        self.top += 1
        return i

    def _bind_new(self, cid, iface):
        sid = self._alloc()
        self.c2s[cid] = sid
        self.s2c[sid] = cid
        self.iface[cid] = iface
        return sid

    @staticmethod
    def _skip(p, off, kind):
        if kind in ("int", "uint", "fixed", "object", "new_id"):
            return off + 4
        if kind in ("string", "array"):
            (n,) = struct.unpack_from("=I", p, off)
            return off + 4 + ((n + 3) & ~3)
        return off            # fd: out of band

    def request(self, data):
        """Client-space request -> compositor-space bytes, or None to drop."""
        obj, word = HEADER.unpack_from(data)
        op = word & 0xffff
        sobj = self.c2s.get(obj)
        iface = self.schema.get(self.iface.get(obj))
        if sobj is None or iface is None or op >= len(iface.requests):
            self.log("dropping request {}.{} on unknown object {}".format(self.iface.get(obj), op, obj))
            return None
        p = bytearray(data)
        struct.pack_into("=I", p, 0, sobj)
        off = 8
        try:
            for arg in iface.requests[op]:
                t = arg.type
                if t == "object":
                    (cid,) = struct.unpack_from("=I", p, off)
                    if cid:
                        sid = self.c2s.get(cid)
                        if sid is None:
                            self.log("dropping {}.{}: unknown object argument {}".format(iface.name, op, cid))
                            return None
                        struct.pack_into("=I", p, off, sid)
                elif t == "new_id":
                    if arg.interface is None:          # wl_registry.bind: string iface, uint version, new id
                        (n,) = struct.unpack_from("=I", p, off)
                        name = bytes(p[off + 4:off + 4 + n - 1]).decode("utf-8", "replace")
                        off += 4 + ((n + 3) & ~3) + 4
                        (cid,) = struct.unpack_from("=I", p, off)
                        struct.pack_into("=I", p, off, self._bind_new(cid, name))
                    else:
                        (cid,) = struct.unpack_from("=I", p, off)
                        struct.pack_into("=I", p, off, self._bind_new(cid, arg.interface))
                off = self._skip(p, off, t)
        except struct.error:
            return None
        return bytes(p)

    def event(self, obj, op, payload):
        """Compositor-space event -> (client obj, client payload), or None to drop."""
        cobj = obj if obj >= SERVER_ID_START else self.s2c.get(obj)
        iface = self.schema.get(self.iface.get(cobj)) if cobj is not None else None
        if iface is None or op >= len(iface.events):
            return None
        p = bytearray(payload)
        if iface.name == "wl_display" and op == P.WL_DISPLAY_EV_DELETE_ID:
            (sid,) = struct.unpack_from("=I", p, 0)
            cid = self.s2c.pop(sid, None)
            if cid is None:
                return None
            self.c2s.pop(cid, None)
            self.iface.pop(cid, None)
            self.free.append(sid)
            struct.pack_into("=I", p, 0, cid)
            return cobj, bytes(p)
        if iface.name == "wl_registry":
            r = Reader(payload)
            if op == P.WL_REGISTRY_EV_GLOBAL:
                name, gi = r.u(), r.s()
                if gi not in self.schema:
                    self.hidden.add(name)      # the HWC must never bind what we cannot translate
                    return HIDDEN
            elif op == P.WL_REGISTRY_EV_GLOBAL_REMOVE and r.u() in self.hidden:
                return HIDDEN
        off = 0
        try:
            for arg in iface.events[op]:
                t = arg.type
                if t == "object":
                    (sid,) = struct.unpack_from("=I", p, off)
                    if sid and sid < SERVER_ID_START:
                        cid = self.s2c.get(sid)
                        if cid is None:
                            return None
                        struct.pack_into("=I", p, off, cid)
                elif t == "new_id":
                    (sid,) = struct.unpack_from("=I", p, off)
                    # server-created objects keep their id in both spaces
                    self.c2s[sid] = sid
                    self.s2c[sid] = sid
                    self.iface[sid] = arg.interface
                off = self._skip(p, off, t)
        except struct.error:
            return None
        return cobj, bytes(p)


# -- protocol state ---------------------------------------------------------------

class Surf:
    __slots__ = ("id", "viewport", "parent", "sub", "dirty", "req_dest", "req_pos", "children")

    def __init__(self, sid):
        self.id = sid
        self.viewport = None   # HWC's wp_viewport on this surface
        self.parent = None     # parent surface id (for subsurfaces)
        self.sub = None        # wl_subsurface id
        self.dirty = False     # HWC has pending (uncommitted) state on it
        self.req_dest = None   # destination the HWC asked for (unscaled)
        self.req_pos = None    # subsurface position the HWC asked for (unscaled)
        self.children = []


class Window:
    """The HWC's full-UI toplevel and our frame around it."""

    def __init__(self, toplevel, xdg_surface, surface):
        self.toplevel = toplevel
        self.xdg_surface = xdg_surface
        self.surface = surface
        self.committed = False
        self.req_geometry = None
        self.fullscreen = False
        self.fs_size = None
        self.fill_size = None    # maximized/tiled: window size given by the compositor
        self.maximized = False
        self.resizing = False
        self.pending_apply = False
        self.frame = None        # dict of our objects once created
        self.saved_zoom = None


TRACE = os.environ.get("WDM_PROXY_TRACE") == "1"
RECENT = collections.deque(maxlen=300)     # last messages, dumped with any error
LOG_PATH = None


def log_error(what):
    """Append a traceback and the recent message history to the proxy log."""
    import traceback
    text = "{} {}\n{}recent messages (oldest first):\n{}\n\n".format(
        time.strftime("%Y-%m-%d %H:%M:%S"), what, traceback.format_exc(),
        "\n".join("  " + r for r in RECENT))
    sys.stderr.write("wlproxy: " + what + "\n")
    if LOG_PATH:
        try:
            with open(LOG_PATH, "a") as f:
                f.write(text)
        except OSError:
            pass


def trace(direction, obj, op, iface, note=""):
    if TRACE:
        sys.stderr.write("{} {}@{}.{} {}\n".format(direction, iface or "?", obj, op, note))


class Session:
    """Protocol brain of one proxied connection (one HWC client)."""

    def __init__(self, cfg, emit):
        self.cfg = cfg
        self.emit = emit
        self.c2s = None          # Streams, set by the connection
        self.s2c = None
        self.objs = {}           # client object id -> interface name
        self.registries = set()
        self.globals = {}        # name -> (interface, version)
        self.surfaces = {}       # id -> Surf
        self.viewports = {}      # viewport id -> surface id
        self.subsurfaces = {}    # subsurface id -> surface id
        self.xdg_surfaces = {}   # xdg_surface id -> surface id
        self.toplevels = {}      # toplevel id -> {"xdg": id, "surface": id, "app_id": str|None}
        self.seat = None
        self.pointer = None
        self.keyboard = None
        self.touch = None
        self.outputs = {}        # output id -> {"mode": (w,h), "scale": int}
        self.surface_output = None
        self.window = None
        self.zoom = None         # current zoom factor (float), None until known
        self.res = (cfg.width, cfg.height) if cfg.width and cfg.height else None
        # proxy-owned objects
        self.next_id = MY_ID_BASE
        self.mine = {}           # id -> kind
        self.my_globals = {}     # interface -> our bound object id
        self.buffers = {}        # our buffer id -> ("shm"|"pixel")
        # input routing
        self.last_serial = 0
        self.ptr_focus = None    # ("mine", surface) | ("tree", surface) | None
        self.ptr_pos = (0.0, 0.0)
        self.ptr_group_forwarded = False
        self.ptr_group_dropped = False
        self.enter_serial = 0
        self.touches = {}        # touch id -> "mine" | surface id
        self.touch_group = [False, False]   # forwarded, dropped
        self.hover = {}          # our surface kind -> hovered action
        self.pressed = None      # (surface kind, action)
        self.last_title_click = 0.0
        self.cursor_dev = None
        self.pings = {}           # serial -> time the compositor pinged
        self.ping_stats = {"pings": 0, "pongs": 0, "max_latency": 0.0, "last_latency": 0.0}

    # -- helpers ------------------------------------------------------------------
    def new_id(self, kind):
        i = self.next_id
        self.next_id += 1
        self.mine[i] = kind
        return i

    def to_server(self, data, fds=()):
        if TRACE:
            o, w = HEADER.unpack_from(data)
            trace(">>+", o, w & 0xffff, self.objs.get(o) or self.mine.get(o), "injected")
        self.c2s.inject(data, fds)

    def to_client(self, data):
        if TRACE:
            o, w = HEADER.unpack_from(data)
            trace("<<+", o, w & 0xffff, self.objs.get(o) or self.mine.get(o), "injected")
        self.s2c.inject(data)

    def in_tree(self, sid):
        """Is the surface the full-UI toplevel or one of the HWC's subsurfaces of it?"""
        w = self.window
        if not w or sid is None:
            return False
        if sid == w.surface:
            return True
        s = self.surfaces.get(sid)
        return bool(s and s.parent == w.surface)

    # -- geometry model -------------------------------------------------------------
    @property
    def framed(self):
        return bool(self.cfg.frame and self.window and self.window.frame and not self.window.fullscreen)

    def frame_extent(self):
        return (fr.TOOLBAR_W, fr.TITLE_H) if self.framed else (0, 0)

    def content_size(self):
        z = self.zoom or 1.0
        rw, rh = self.res or (0, 0)
        return max(1, int(round(rw * z))), max(1, int(round(rh * z)))

    def area(self):
        """Size of the region the content is centred in (the HWC toplevel's size) when the
        compositor dictates the window size (fullscreen, maximized, tiled); else None."""
        w = self.window
        if not w:
            return None
        if w.fullscreen and w.fs_size:
            return w.fs_size
        if w.fill_size:
            b, t = (fr.TOOLBAR_W, fr.TITLE_H) if self.framed else (0, 0)
            return max(1, w.fill_size[0] - b), max(1, w.fill_size[1] - t)
        return None

    def content_offset(self):
        a = self.area()
        if a and self.res:
            dw, dh = self.content_size()
            return max(0, (a[0] - dw) // 2), max(0, (a[1] - dh) // 2)
        return 0, 0

    def output_logical(self):
        out = None
        if self.surface_output in self.outputs:
            out = self.outputs[self.surface_output]
        elif self.outputs:
            out = max(self.outputs.values(), key=lambda o: (o.get("mode") or (0, 0))[0] * (o.get("mode") or (0, 0))[1])
        if not out or not out.get("mode"):
            return None
        sc = max(1, out.get("scale", 1))
        return out["mode"][0] // sc, out["mode"][1] // sc

    def fit_zoom(self, avail_w, avail_h):
        if not self.res:
            return 1.0
        b, t = (fr.TOOLBAR_W, fr.TITLE_H) if (self.cfg.frame and not (self.window and self.window.fullscreen)) else (0, 0)
        z = min((avail_w - b) / self.res[0], (avail_h - t) / self.res[1])
        return max(ZOOM_MIN, min(ZOOM_MAX, z))

    def initial_zoom(self):
        if self.cfg.zoom not in (None, "", "auto"):
            return max(ZOOM_MIN, min(ZOOM_MAX, float(self.cfg.zoom) / 100.0))
        out = self.output_logical()
        if not out or not self.res:
            return 1.0
        return min(1.0, self.fit_zoom(out[0] - 2 * fr.BORDER, out[1] - PANEL_ALLOWANCE))

    def scaled_dest(self, sid, w, h):
        z = self.zoom or 1.0
        win = self.window
        if win and sid == win.surface:
            return self.area() or self.content_size()
        return max(1, int(math.ceil(w * z))), max(1, int(math.ceil(h * z)))

    def scaled_pos(self, x, y):
        z = self.zoom or 1.0
        cx, cy = self.content_offset()
        return int(round(x * z)) + cx, int(round(y * z)) + cy

    def geometry(self):
        w = self.window
        if w and w.fullscreen and w.fs_size:
            return 0, 0, w.fs_size[0], w.fs_size[1]
        aw, ah = self.area() or self.content_size()
        b, t = self.frame_extent()
        return 0, -t, aw + b, ah + t

    def unscale_point(self, sid, x_fixed, y_fixed):
        """Compositor surface coords -> what the HWC expects (Android px at scale 1)."""
        z = self.zoom or 1.0
        x, y = P.fixed_to_float(x_fixed), P.fixed_to_float(y_fixed)
        if self.window and sid == self.window.surface:
            cx, cy = self.content_offset()
            x, y = x - cx, y - cy
            if self.res:
                dw, dh = self.content_size()
                x, y = min(max(x, 0.0), dw - 0.01), min(max(y, 0.0), dh - 0.01)
        return P.float_to_fixed(x / z), P.float_to_fixed(y / z)

    # -- client -> server -------------------------------------------------------------
    def on_request(self, obj, op, payload):
        RECENT.append(">> {}@{}.{} len={}".format(self.objs.get(obj) or self.mine.get(obj), obj, op, len(payload)))
        try:
            res = self._on_request(obj, op, payload)
        except Exception:  # noqa: BLE001  fail open: forward the request unchanged
            log_error("error handling request {}@{}.{}".format(self.objs.get(obj), obj, op))
            res = None
        if TRACE:
            trace(">>", obj, op, self.objs.get(obj) or self.mine.get(obj),
                  "" if res is None else "rewritten->{}".format(len(res)))
        return res

    def _on_request(self, obj, op, payload):
        iface = self.objs.get(obj)
        r = Reader(payload)
        if obj == 1:
            if op == P.WL_DISPLAY_GET_REGISTRY:
                rid = r.n()
                self.objs[rid] = "wl_registry"
                self.registries.add(rid)
            return None
        if iface is None:
            return None
        if iface == "wl_registry" and op == P.WL_REGISTRY_BIND:
            r.u()
            name_iface = r.s()
            r.u()
            new = r.n()
            self.objs[new] = name_iface
            if name_iface == "wl_seat":
                self.seat = new
            elif name_iface == "wl_output":
                self.outputs.setdefault(new, {})
            return None
        if iface == "wl_compositor" and op == P.WL_COMPOSITOR_CREATE_SURFACE:
            sid = r.n()
            self.objs[sid] = "wl_surface"
            self.surfaces[sid] = Surf(sid)
            return None
        if iface == "wl_subcompositor" and op == P.WL_SUBCOMPOSITOR_GET_SUBSURFACE:
            new, sid, parent = r.n(), r.o(), r.o()
            self.objs[new] = "wl_subsurface"
            self.subsurfaces[new] = sid
            s = self.surfaces.get(sid)
            if s:
                s.parent, s.sub = parent, new
                if parent in self.surfaces:
                    self.surfaces[parent].children.append(sid)
            return None
        if iface == "wp_viewporter" and op == P.WP_VIEWPORTER_GET_VIEWPORT:
            new, sid = r.n(), r.o()
            self.objs[new] = "wp_viewport"
            self.viewports[new] = sid
            if sid in self.surfaces:
                self.surfaces[sid].viewport = new
            return None
        if iface == "xdg_wm_base" and op == 3:          # pong
            t0 = self.pings.pop(r.u(), None)
            if t0 is not None:
                lat = time.monotonic() - t0
                st = self.ping_stats
                st["pongs"] += 1
                st["last_latency"] = lat
                st["max_latency"] = max(st["max_latency"], lat)
            return None
        if iface == "xdg_wm_base" and op == P.XDG_WM_BASE_GET_XDG_SURFACE:
            new, sid = r.n(), r.o()
            self.objs[new] = "xdg_surface"
            self.xdg_surfaces[new] = sid
            return None
        if iface == "xdg_surface":
            return self._xdg_surface_request(obj, op, r)
        if iface == "xdg_toplevel":
            return self._toplevel_request(obj, op, r, payload)
        if iface == "wl_seat":
            if op in (P.WL_SEAT_GET_POINTER, P.WL_SEAT_GET_KEYBOARD, P.WL_SEAT_GET_TOUCH):
                new = r.n()
                kind = {P.WL_SEAT_GET_POINTER: "wl_pointer", P.WL_SEAT_GET_KEYBOARD: "wl_keyboard",
                        P.WL_SEAT_GET_TOUCH: "wl_touch"}[op]
                self.objs[new] = kind
                setattr(self, kind[3:], new)
            return None
        if iface == "wp_fractional_scale_manager_v1" and op == P.WP_FRACTIONAL_SCALE_MANAGER_GET:
            self.objs[r.n()] = "wp_fractional_scale_v1"
            return None
        if iface == "wl_data_device_manager" and op == P.WL_DATA_DEVICE_MANAGER_GET_DATA_DEVICE:
            self.objs[r.n()] = "wl_data_device"
            return None
        if iface == "wl_surface":
            return self._surface_request(obj, op, r)
        if iface == "wp_viewport":
            return self._viewport_request(obj, op, r)
        if iface == "wl_subsurface":
            return self._subsurface_request(obj, op, r)
        return None

    def _surface_request(self, sid, op, r):
        s = self.surfaces.get(sid)
        if s is None:
            return None
        tree = self.in_tree(sid)
        if op == P.WL_SURFACE_DESTROY:
            self._forget_surface(sid)
            return None
        if op == P.WL_SURFACE_COMMIT:
            out = []
            s.dirty = False
            out.append(msg(sid, P.WL_SURFACE_COMMIT))
            w = self.window
            if w and sid == w.surface and not w.committed:
                w.committed = True
                self._window_mapped()
            return out
        if not tree:
            return None
        s.dirty = True
        if op == P.WL_SURFACE_DAMAGE and self.zoom not in (None, 1.0):
            return [msg(sid, P.WL_SURFACE_DAMAGE, "iiii", 0, 0, FULL_DAMAGE, FULL_DAMAGE)]
        if op == P.WL_SURFACE_SET_OPAQUE_REGION:
            return []   # coordinates would need scaling; it is only an optimisation
        return None

    def _viewport_request(self, vid, op, r):
        sid = self.viewports.get(vid)
        if op == P.WP_VIEWPORT_DESTROY:
            self.viewports.pop(vid, None)
            if sid in self.surfaces and self.surfaces[sid].viewport == vid:
                self.surfaces[sid].viewport = None
            return None
        if not self.in_tree(sid):
            return None
        s = self.surfaces[sid]
        s.dirty = True
        if op == P.WP_VIEWPORT_SET_DESTINATION:
            w, h = r.i(), r.i()
            if w <= 0 or h <= 0:
                s.req_dest = None
                return None
            s.req_dest = (w, h)
            if self.res is None and self.window and sid == self.window.surface:
                self.res = (w, h)       # HWC scale is 1, so this is the Android size
            if self.zoom is None:
                self.zoom = self.initial_zoom()
            dw, dh = self.scaled_dest(sid, w, h)
            return [msg(vid, P.WP_VIEWPORT_SET_DESTINATION, "ii", dw, dh)]
        return None

    def _subsurface_request(self, subid, op, r):
        sid = self.subsurfaces.get(subid)
        if op == P.WL_SUBSURFACE_DESTROY:
            self.subsurfaces.pop(subid, None)
            return None
        if op != P.WL_SUBSURFACE_SET_POSITION or not self.window or sid is None:
            return None
        s = self.surfaces.get(sid)
        if not s or s.parent != self.window.surface:
            return None
        x, y = r.i(), r.i()
        s.req_pos = (x, y)
        self.surfaces[self.window.surface].dirty = True
        px, py = self.scaled_pos(x, y)
        return [msg(subid, P.WL_SUBSURFACE_SET_POSITION, "ii", px, py)]

    def _xdg_surface_request(self, xid, op, r):
        if op == P.XDG_SURFACE_GET_TOPLEVEL:
            new = r.n()
            self.objs[new] = "xdg_toplevel"
            self.toplevels[new] = {"xdg": xid, "surface": self.xdg_surfaces.get(xid), "app_id": None}
            return None
        w = self.window
        if not w or xid != w.xdg_surface:
            return None
        if op == P.XDG_SURFACE_SET_WINDOW_GEOMETRY:
            w.req_geometry = (r.i(), r.i(), r.i(), r.i())
            self.surfaces[w.surface].dirty = True
            if self.res is None:
                self.res = (w.req_geometry[2], w.req_geometry[3])
            return [msg(xid, P.XDG_SURFACE_SET_WINDOW_GEOMETRY, "iiii", *self.geometry())]
        if op == P.XDG_SURFACE_ACK_CONFIGURE and w.pending_apply:
            # Apply the new geometry right after the HWC has acked the configure
            w.pending_apply = False
            self.request_apply()
            return None
        if op == P.XDG_SURFACE_DESTROY:
            self._drop_window()
        return None

    def _toplevel_request(self, tid, op, r, payload):
        t = self.toplevels.get(tid)
        if t is None:
            return None
        if op == P.XDG_TOPLEVEL_SET_TITLE:
            title = r.s()
            return [msg(tid, op, "s", self.title_for(title))]
        if op == P.XDG_TOPLEVEL_SET_APP_ID:
            app_id = r.s()
            t["app_id"] = app_id
            if app_id in (None, FULL_UI_APP_ID) and self.window is None and t["surface"] in self.surfaces:
                self.window = Window(tid, t["xdg"], t["surface"])
                if self.zoom is None and self.res:
                    self.zoom = self.initial_zoom()
            return [msg(tid, op, "s", self.app_id_for(app_id))]
        if op in (P.XDG_TOPLEVEL_SET_MAXIMIZED, P.XDG_TOPLEVEL_UNSET_MAXIMIZED) and \
                self.window and self.window.toplevel == tid:
            return []   # calibration maximize would skew the zoom; the window size is ours
        if op == P.XDG_TOPLEVEL_DESTROY:
            if self.window and self.window.toplevel == tid:
                out = [msg(tid, op)]
                self._drop_window()
                del self.toplevels[tid]
                return out
            del self.toplevels[tid]
        return None

    # -- labels -------------------------------------------------------------------------
    def title_for(self, title):
        if not title or title == FULL_UI_APP_ID:
            return "Waydroid · {}".format(self.cfg.name)
        return "{} · {}".format(title, self.cfg.name)

    def app_id_for(self, app_id):
        base = "waydroid-multi.{}".format(self.cfg.id)
        if not app_id or app_id == FULL_UI_APP_ID:
            return base
        if app_id.startswith("waydroid."):
            return base + "." + app_id[len("waydroid."):]
        return base + "." + app_id

    # -- server -> client ---------------------------------------------------------------
    def on_event(self, obj, op, payload):
        if obj == 1 and op == P.WL_DISPLAY_EV_ERROR:
            try:
                r = Reader(payload)
                bad, code, text = r.o(), r.u(), r.s()
                sys.stderr.write("wayland protocol error on object {} ({}): code {}: {}\n".format(
                    bad, self.objs.get(bad) or self.mine.get(bad), code, text))
            except ProtocolError:
                pass
        RECENT.append("<< {}@{}.{} len={}".format(self.objs.get(obj) or self.mine.get(obj), obj, op, len(payload)))
        try:
            res = self._on_event(obj, op, payload)
        except Exception:  # noqa: BLE001  fail open: forward the event unchanged
            log_error("error handling event {}@{}.{}".format(self.objs.get(obj), obj, op))
            res = [] if obj in self.mine else None
        if TRACE:
            trace("<<", obj, op, self.objs.get(obj) or self.mine.get(obj),
                  "" if res is None else "rewritten->{}".format(len(res)))
        return res

    def _on_event(self, obj, op, payload):
        if obj in self.mine:
            self._my_event(obj, op, payload)
            return []
        iface = self.objs.get(obj)
        r = Reader(payload)
        if obj == 1:
            if op == P.WL_DISPLAY_EV_DELETE_ID:
                dead = r.u()
                if dead in self.mine:
                    del self.mine[dead]
                    self.buffers.pop(dead, None)
                    return []
                self._forget(dead)
            return None
        if iface is None:
            return None
        if iface == "wl_registry":
            if op == P.WL_REGISTRY_EV_GLOBAL:
                name = r.u()
                gi = r.s()
                ver = r.u()
                self.globals[name] = (gi, ver)
            elif op == P.WL_REGISTRY_EV_GLOBAL_REMOVE:
                self.globals.pop(r.u(), None)
            return None
        if iface == "wl_output":
            out = self.outputs.setdefault(obj, {})
            if op == P.WL_OUTPUT_EV_SCALE:
                out["scale"] = r.i()
                return [msg(obj, op, "i", 1)]          # the HWC always works at scale 1
            if op == P.WL_OUTPUT_EV_MODE:
                flags, w, h = r.u(), r.i(), r.i()
                if flags & 1:
                    out["mode"] = (w, h)
            return None
        if iface == "wp_fractional_scale_v1" and op == P.WP_FRACTIONAL_SCALE_EV_PREFERRED:
            return [msg(obj, op, "u", 120)]
        if iface == "wl_surface" and op == 0 and self.window and obj == self.window.surface:
            self.surface_output = r.o()             # wl_surface.enter(output)
            return None
        if iface == "xdg_wm_base" and op == 0:          # ping
            self.ping_stats["pings"] += 1
            self.pings[r.u()] = time.monotonic()
            if len(self.pings) > 64:
                self.pings.pop(next(iter(self.pings)))
            return None
        if iface == "xdg_toplevel":
            return self._toplevel_event(obj, op, r)
        if iface == "wl_pointer":
            return self._pointer_event(obj, op, r, payload)
        if iface == "wl_touch":
            return self._touch_event(obj, op, r, payload)
        if iface == "wl_keyboard":
            return self._keyboard_event(obj, op, r)
        if iface == "wl_data_device":
            return self._data_device_event(obj, op, r, payload)
        return None

    KEY_ESC, KEY_F11 = 1, 87

    def _keyboard_event(self, kid, op, r):
        if op == P.WL_KEYBOARD_EV_ENTER:
            self.last_serial, self.kbd_focus = r.u(), r.o()
        elif op == 2:                                     # leave
            self.last_serial = r.u()
            self.kbd_focus = None
        elif op == 4:                                     # modifiers
            self.last_serial = r.u()
        elif op == P.WL_KEYBOARD_EV_KEY:
            serial, _t, key, state = r.u(), r.u(), r.u(), r.u()
            self.last_serial = serial
            w = self.window
            swallowed = getattr(self, "swallowed_keys", set())
            if state == 0 and key in swallowed:
                swallowed.discard(key)
                return []
            if (state == 1 and w and getattr(self, "kbd_focus", None) == w.surface
                    and (key == self.KEY_F11 or (key == self.KEY_ESC and w.fullscreen))):
                swallowed.add(key)
                self.swallowed_keys = swallowed
                self.toggle_fullscreen(leave_only=(key == self.KEY_ESC))
                return []
        return None

    def _toplevel_event(self, tid, op, r):
        w = self.window
        if not w or tid != w.toplevel:
            return None
        if op == P.XDG_TOPLEVEL_EV_CLOSE:
            self._close_requested()
            return []          # the HWC would wipe all Android recent tasks
        if op != P.XDG_TOPLEVEL_EV_CONFIGURE:
            return None
        width, height = r.i(), r.i()
        raw_states = r.a()
        states = set(struct.unpack("={}I".format(len(raw_states) // 4), raw_states))
        self._configure(width, height, states)
        # The HWC would hotplug Android's display on any real size: hide it
        return [msg(tid, op, "iia", 0, 0, raw_states)]

    FILL_STATES = {P.XDG_TOPLEVEL_STATE_MAXIMIZED, 5, 6, 7, 8}     # maximized, tiled_*

    def _configure(self, width, height, states):
        w = self.window
        was_fs, was_fill, was_resizing = w.fullscreen, w.fill_size is not None, w.resizing
        w.fullscreen = P.XDG_TOPLEVEL_STATE_FULLSCREEN in states
        w.maximized = P.XDG_TOPLEVEL_STATE_MAXIMIZED in states
        filling = bool(states & self.FILL_STATES) and not w.fullscreen
        w.resizing = P.XDG_TOPLEVEL_STATE_RESIZING in states
        sized = width > 0 and height > 0
        changed = False
        if w.fullscreen:
            if sized:
                if not was_fs and not was_fill:
                    w.saved_zoom = self.zoom
                w.fs_size, w.fill_size = (width, height), None
                self.zoom = self.fit_zoom(width, height)
                changed = True
        elif filling:
            if sized:
                if not was_fs and not was_fill:
                    w.saved_zoom = self.zoom
                w.fill_size, w.fs_size = (width, height), None
                self.zoom = self.fit_zoom(width, height)
                changed = True
        elif was_fs or was_fill:
            # back to a normal window: restore the zoom it had before
            self.zoom = w.saved_zoom or self.zoom
            w.fs_size = w.fill_size = None
            changed = True
        elif sized and self.res:
            gx, gy, gw, gh = self.geometry()
            if w.resizing or abs(width - gw) > 1 or abs(height - gh) > 1:
                self.zoom = self.fit_zoom(width, height)
                changed = True
        if was_resizing and not w.resizing:
            self._report_zoom()
        if changed or was_fs != w.fullscreen:
            w.pending_apply = True

    def _pointer_event(self, pid, op, r, payload):
        if op == P.WL_POINTER_EV_ENTER:
            serial, sid, x, y = r.u(), r.o(), r.f(), r.f()
            self.last_serial = serial
            if sid in self.mine:
                self.ptr_focus = ("mine", sid)
                self.enter_serial = serial
                self.ptr_pos = (P.fixed_to_float(x), P.fixed_to_float(y))
                self._frame_hover(sid, *self.ptr_pos, entered=True)
                self.ptr_group_dropped = True
                return []
            self.ptr_group_forwarded = True
            if self.in_tree(sid):
                self.ptr_focus = ("tree", sid)
                ux, uy = self.unscale_point(sid, x, y)
                return [msg(pid, op, "uoff", serial, sid, ux, uy)]
            self.ptr_focus = ("other", sid)
            return None
        if op == P.WL_POINTER_EV_LEAVE:
            serial, sid = r.u(), r.o()
            if sid in self.mine:
                self._frame_leave(sid)
                self.ptr_focus = None
                self.ptr_group_dropped = True
                return []
            self.ptr_focus = None
            self.ptr_group_forwarded = True
            return None
        focus = self.ptr_focus
        if op == P.WL_POINTER_EV_FRAME:
            fwd, drop = self.ptr_group_forwarded, self.ptr_group_dropped
            self.ptr_group_forwarded = self.ptr_group_dropped = False
            if drop and not fwd:
                return []
            return None
        if focus and focus[0] == "mine":
            if op == P.WL_POINTER_EV_MOTION:
                r.u()
                self.ptr_pos = (P.fixed_to_float(r.f()), P.fixed_to_float(r.f()))
                self._frame_hover(focus[1], *self.ptr_pos)
            elif op == P.WL_POINTER_EV_BUTTON:
                serial, _t, button, state = r.u(), r.u(), r.u(), r.u()
                self.last_serial = serial
                if button == P.BTN_LEFT:
                    self._frame_button(focus[1], *self.ptr_pos, pressed=state == 1, serial=serial)
            self.ptr_group_dropped = True
            return []
        self.ptr_group_forwarded = True
        if op == P.WL_POINTER_EV_BUTTON:
            self.last_serial = r.u()
            return None
        if op == P.WL_POINTER_EV_MOTION and focus and focus[0] == "tree":
            t, x, y = r.u(), r.f(), r.f()
            ux, uy = self.unscale_point(focus[1], x, y)
            return [msg(pid, op, "uff", t, ux, uy)]
        return None

    def _touch_event(self, tid, op, r, payload):
        if op == P.WL_TOUCH_EV_DOWN:
            serial, t, sid, point, x, y = r.u(), r.u(), r.o(), r.i(), r.f(), r.f()
            self.last_serial = serial
            if sid in self.mine:
                self.touches[point] = ("mine", sid, P.fixed_to_float(x), P.fixed_to_float(y))
                self._frame_button(sid, P.fixed_to_float(x), P.fixed_to_float(y), pressed=True, serial=serial)
                self.touch_group[1] = True
                return []
            self.touches[point] = ("tree" if self.in_tree(sid) else "other", sid)
            self.touch_group[0] = True
            if self.in_tree(sid):
                ux, uy = self.unscale_point(sid, x, y)
                return [msg(tid, op, "uuoiff", serial, t, sid, point, ux, uy)]
            return None
        if op == P.WL_TOUCH_EV_UP:
            serial, t, point = r.u(), r.u(), r.i()
            info = self.touches.pop(point, None)
            if info and info[0] == "mine":
                self._frame_button(info[1], info[2], info[3], pressed=False, serial=serial)
                self.touch_group[1] = True
                return []
            self.touch_group[0] = True
            return None
        if op == P.WL_TOUCH_EV_MOTION:
            t, point, x, y = r.u(), r.i(), r.f(), r.f()
            info = self.touches.get(point)
            if info and info[0] == "mine":
                self.touch_group[1] = True
                return []
            self.touch_group[0] = True
            if info and info[0] == "tree":
                ux, uy = self.unscale_point(info[1], x, y)
                return [msg(tid, op, "uiff", t, point, ux, uy)]
            return None
        if op in (P.WL_TOUCH_EV_FRAME, P.WL_TOUCH_EV_CANCEL):
            fwd, drop = self.touch_group
            self.touch_group = [False, False]
            if op == P.WL_TOUCH_EV_CANCEL:
                self.touches = {k: v for k, v in self.touches.items() if v[0] != "mine"}
            if drop and not fwd and not any(v[0] != "mine" for v in self.touches.values()):
                return []
            return None
        return None

    def _data_device_event(self, did, op, r, payload):
        if op == P.WL_DATA_DEVICE_EV_ENTER:
            serial, sid, x, y, offer = r.u(), r.o(), r.f(), r.f(), r.o()
            if sid in self.mine:
                return []
            if self.in_tree(sid):
                self._dnd_surface = sid
                ux, uy = self.unscale_point(sid, x, y)
                return [msg(did, op, "uoffo", serial, sid, ux, uy, offer)]
            self._dnd_surface = None
            return None
        if op == P.WL_DATA_DEVICE_EV_MOTION and getattr(self, "_dnd_surface", None):
            t, x, y = r.u(), r.f(), r.f()
            ux, uy = self.unscale_point(self._dnd_surface, x, y)
            return [msg(did, op, "uff", t, ux, uy)]
        return None

    def _my_event(self, obj, op, payload):
        kind = self.mine.get(obj)
        if kind in ("buffer_shm", "buffer_pixel") and op == 0:     # wl_buffer.release
            self.to_server(msg(obj, P.WL_BUFFER_DESTROY))

    # -- applying geometry ----------------------------------------------------------------
    def _apply_messages(self, commit=True):
        """Messages that bring every tree surface and the frame up to date."""
        w = self.window
        if not w or not self.res or self.zoom is None:
            return []
        out = []
        S = self.surfaces.get(w.surface)
        if S is None:
            return []
        if S.viewport and S.req_dest:
            out.append(msg(S.viewport, P.WP_VIEWPORT_SET_DESTINATION, "ii", *self.scaled_dest(S.id, *S.req_dest)))
        for cid in S.children:
            c = self.surfaces.get(cid)
            if not c or c.parent != S.id:
                continue
            if c.sub and c.req_pos is not None:
                out.append(msg(c.sub, P.WL_SUBSURFACE_SET_POSITION, "ii", *self.scaled_pos(*c.req_pos)))
            if c.viewport and c.req_dest:
                out.append(msg(c.viewport, P.WP_VIEWPORT_SET_DESTINATION, "ii", *self.scaled_dest(cid, *c.req_dest)))
                if commit and not c.dirty:
                    out.append(msg(cid, P.WL_SURFACE_COMMIT))
        if w.req_geometry is not None or w.committed:
            out.append(msg(w.xdg_surface, P.XDG_SURFACE_SET_WINDOW_GEOMETRY, "iiii", *self.geometry()))
        out.extend(self._frame_messages())
        if commit and not S.dirty:
            out.append(msg(S.id, P.WL_SURFACE_COMMIT))
        return out

    def apply_now(self):
        # Computed now, so the dirty flags reflect everything the HWC has sent
        for m in self._apply_messages():
            self.to_server(m)

    def set_zoom(self, z, report=True):
        if not self.res:
            return
        self.zoom = max(ZOOM_MIN, min(ZOOM_MAX, z))
        self.request_apply()
        if report:
            self._report_zoom()

    def _report_zoom(self):
        if self.zoom:
            self.emit("zoom {}".format(int(round(self.zoom * 100))))

    def _window_mapped(self):
        """First commit of the full-UI toplevel: create the frame."""
        if self.zoom is None:
            self.zoom = self.initial_zoom()
        if self.cfg.frame and self._can_frame():
            self._create_frame()
        self.request_apply()

    def request_apply(self):
        """Bring geometry/frame up to date, as soon as the HWC's stream allows."""
        if self.c2s.feeding:
            self._apply_wanted = True
            return
        self.apply_now()

    def post_feed_c2s(self):
        if getattr(self, "_apply_wanted", False):
            self._apply_wanted = False
            self.apply_now()

    def _drop_window(self):
        w = self.window
        if not w:
            return
        if w.frame:
            for key in ("title_sub", "toolbar_sub", "border_sub", "border_vp"):
                oid = w.frame.get(key)
                if oid:
                    op = P.WL_SUBSURFACE_DESTROY if key.endswith("_sub") else P.WP_VIEWPORT_DESTROY
                    self.to_server(msg(oid, op))
            for key in ("title", "toolbar", "border"):
                oid = w.frame.get(key)
                if oid:
                    self.to_server(msg(oid, P.WL_SURFACE_DESTROY))
        self.window = None

    def _forget_surface(self, sid):
        s = self.surfaces.pop(sid, None)
        if s and s.parent in self.surfaces:
            try:
                self.surfaces[s.parent].children.remove(sid)
            except ValueError:
                pass

    def _forget(self, oid):
        iface = self.objs.pop(oid, None)
        if iface == "wl_surface":
            self._forget_surface(oid)
        self.viewports.pop(oid, None)
        self.subsurfaces.pop(oid, None)
        self.xdg_surfaces.pop(oid, None)
        self.toplevels.pop(oid, None)
        self.outputs.pop(oid, None)
        self.registries.discard(oid)

    # -- frame ------------------------------------------------------------------------------
    def _global(self, interface):
        best = None
        for name, (gi, ver) in self.globals.items():
            if gi == interface:
                best = (name, ver)
        return best

    def _can_frame(self):
        return bool(self.registries) and all(self._global(i) for i in
                                             ("wl_compositor", "wl_subcompositor", "wl_shm", "wp_viewporter"))

    def _bind(self, interface, version):
        if interface in self.my_globals:
            return self.my_globals[interface]
        g = self._global(interface)
        if not g:
            return None
        reg = next(iter(self.registries))
        oid = self.new_id(interface)
        self.to_server(msg(reg, P.WL_REGISTRY_BIND, "usun", g[0], interface, min(version, g[1]), oid))
        self.my_globals[interface] = oid
        return oid

    def _create_frame(self):
        w = self.window
        comp = self._bind("wl_compositor", 4)
        subc = self._bind("wl_subcompositor", 1)
        self._bind("wl_shm", 1)
        vp = self._bind("wp_viewporter", 1)
        self._bind("wp_single_pixel_buffer_manager_v1", 1)
        self._bind("wp_cursor_shape_manager_v1", 1)
        f = {}
        for kind in ("title", "toolbar", "border"):
            sid = self.new_id("surface_" + kind)
            self.to_server(msg(comp, P.WL_COMPOSITOR_CREATE_SURFACE, "n", sid))
            sub = self.new_id("subsurface")
            self.to_server(msg(subc, P.WL_SUBCOMPOSITOR_GET_SUBSURFACE, "noo", sub, sid, w.surface))
            f[kind], f[kind + "_sub"] = sid, sub
        self.to_server(msg(f["title_sub"], P.WL_SUBSURFACE_SET_DESYNC))
        self.to_server(msg(f["toolbar_sub"], P.WL_SUBSURFACE_SET_DESYNC))
        self.to_server(msg(f["border_sub"], P.WL_SUBSURFACE_PLACE_BELOW, "o", w.surface))
        self.to_server(msg(f["border_sub"], P.WL_SUBSURFACE_SET_DESYNC))
        f["border_vp"] = self.new_id("viewport")
        self.to_server(msg(vp, P.WP_VIEWPORTER_GET_VIEWPORT, "no", f["border_vp"], f["border"]))
        f["sizes"] = {}
        w.frame = f
        self.surface_kinds = {f["title"]: "title", f["toolbar"]: "toolbar", f["border"]: "border"}

    def _frame_messages(self):
        """Positions of our subsurfaces (parent state) and fresh buffers when sizes changed."""
        w = self.window
        if not w or not w.frame:
            return []
        f = w.frame
        out = []
        dw, dh = self.content_size()
        hidden = w.fullscreen or not self.cfg.frame
        b, t, m = fr.TOOLBAR_W, fr.TITLE_H, fr.BORDER
        if hidden:
            for kind in ("title", "toolbar", "border"):
                if f["sizes"].get(kind) != "hidden":
                    out.append(msg(f[kind], P.WL_SURFACE_ATTACH, "oii", 0, 0, 0))
                    out.append(msg(f[kind], P.WL_SURFACE_COMMIT))
                    f["sizes"][kind] = "hidden"
            return out
        dw, dh = self.area() or (dw, dh)     # maximized/tiled: frame hugs the window, content centred
        out.append(msg(f["title_sub"], P.WL_SUBSURFACE_SET_POSITION, "ii", 0, -t))
        out.append(msg(f["toolbar_sub"], P.WL_SUBSURFACE_SET_POSITION, "ii", dw, 0))
        out.append(msg(f["border_sub"], P.WL_SUBSURFACE_SET_POSITION, "ii", -m, -t - m))
        if f["sizes"].get("title") != (dw + b, t):
            f["sizes"]["title"] = (dw + b, t)
            self._draw("title")
        if f["sizes"].get("toolbar") != (b, dh):
            f["sizes"]["toolbar"] = (b, dh)
            self._draw("toolbar")
        if f["sizes"].get("border") != (dw + b + 2 * m, dh + t + 2 * m):
            f["sizes"]["border"] = (dw + b + 2 * m, dh + t + 2 * m)
            out.extend(self._border_messages(*f["sizes"]["border"]))
        return out

    def _out_scale(self):
        out = self.outputs.get(self.surface_output) or {}
        return max(1, out.get("scale", 1))

    def _draw(self, kind):
        """Render a frame part into a fresh shm buffer; returns messages (fd queued separately)."""
        w = self.window
        f = w.frame
        size = f["sizes"].get(kind)
        if not size or size == "hidden":
            return []
        scale = self._out_scale()
        hover = self.hover.get(kind)
        pressed = self.pressed[1] if self.pressed and self.pressed[0] == kind else None
        if kind == "title":
            data, pw, ph, stride = fr.render_title(size[0], scale, "{} · #{}".format(self.cfg.name, self.cfg.id),
                                                   self.cfg.theme, hover, pressed)
        else:
            data, pw, ph, stride = fr.render_toolbar(size[1], scale, self.cfg.theme, hover, pressed,
                                                     fullscreen=w.fullscreen)
        shm = self.my_globals.get("wl_shm")
        fd = os.memfd_create("wdm-frame", os.MFD_CLOEXEC)
        os.write(fd, data)
        pool = self.new_id("pool")
        buf = self.new_id("buffer_shm")
        # Everything goes through the injection queue, in order, so the pool's fd
        # always reaches the compositor no later than create_pool itself
        self.to_server(msg(shm, P.WL_SHM_CREATE_POOL, "ni", pool, len(data)), fds=[fd])
        for m in (msg(pool, P.WL_SHM_POOL_CREATE_BUFFER, "niiiiu", buf, 0, pw, ph, stride, P.WL_SHM_FORMAT_ARGB8888),
                  msg(pool, P.WL_SHM_POOL_DESTROY),
                  msg(f[kind], P.WL_SURFACE_SET_BUFFER_SCALE, "i", scale),
                  msg(f[kind], P.WL_SURFACE_ATTACH, "oii", buf, 0, 0),
                  msg(f[kind], P.WL_SURFACE_DAMAGE, "iiii", 0, 0, FULL_DAMAGE, FULL_DAMAGE),
                  msg(f[kind], P.WL_SURFACE_COMMIT)):
            self.to_server(m)
        return []

    def _border_messages(self, bw, bh):
        f = self.window.frame
        out = []
        pixel = self.my_globals.get("wp_single_pixel_buffer_manager_v1")
        if pixel:
            buf = self.new_id("buffer_pixel")
            out.append(msg(pixel, P.WP_SINGLE_PIXEL_CREATE_U32_RGBA, "nuuuu", buf, 0, 0, 0, 0))
            out.append(msg(f["border"], P.WL_SURFACE_ATTACH, "oii", buf, 0, 0))
        out.append(msg(f["border_vp"], P.WP_VIEWPORT_SET_DESTINATION, "ii", bw, bh))
        out.append(msg(f["border"], P.WL_SURFACE_DAMAGE, "iiii", 0, 0, FULL_DAMAGE, FULL_DAMAGE))
        out.append(msg(f["border"], P.WL_SURFACE_COMMIT))
        return out

    def _redraw(self, kind):
        if not self.window or not self.window.frame or self.window.frame["sizes"].get(kind) in (None, "hidden"):
            return
        for m in self._draw(kind):
            self.to_server(m)

    def _set_cursor(self, shape):
        mgr = self.my_globals.get("wp_cursor_shape_manager_v1")
        if not mgr or not self.pointer:
            return
        if self.cursor_dev is None:
            self.cursor_dev = self.new_id("cursor_shape_device")
            self.to_server(msg(mgr, P.WP_CURSOR_SHAPE_MANAGER_GET_POINTER, "no", self.cursor_dev, self.pointer))
        self.to_server(msg(self.cursor_dev, P.WP_CURSOR_SHAPE_DEVICE_SET_SHAPE, "uu",
                           self.enter_serial, P.CURSOR_SHAPE.get(shape, 1)))

    def _hit(self, kind, x, y):
        f = self.window.frame if self.window else None
        if not f:
            return None
        size = f["sizes"].get(kind)
        if not size or size == "hidden":
            return None
        if kind == "title":
            return fr.hit_title(x, y, size[0])
        if kind == "toolbar":
            return fr.hit_toolbar(x, y, size[1])
        return fr.border_edge(x, y, size[0], size[1])

    def _frame_hover(self, sid, x, y, entered=False):
        kind = getattr(self, "surface_kinds", {}).get(sid)
        if not kind:
            return
        action = self._hit(kind, x, y)
        if kind == "border":
            cursor = P.EDGE_CURSOR.get(action, "default")
            if entered or self.hover.get(kind) != action:
                self._set_cursor(cursor)
            self.hover[kind] = action
            return
        if entered:
            self._set_cursor("default")
        if action == "move":
            action = None
        if self.hover.get(kind) != action:
            self.hover[kind] = action
            self._redraw(kind)

    def _frame_leave(self, sid):
        kind = getattr(self, "surface_kinds", {}).get(sid)
        if kind and self.hover.get(kind):
            self.hover[kind] = None
            if kind != "border":
                self._redraw(kind)
        if self.pressed and self.pressed[0] == kind:
            self.pressed = None

    def _frame_button(self, sid, x, y, pressed, serial):
        kind = getattr(self, "surface_kinds", {}).get(sid)
        if not kind or not self.window:
            return
        action = self._hit(kind, x, y)
        w = self.window
        if kind == "border":
            if pressed and action and self.seat:
                self.to_server(msg(w.toplevel, P.XDG_TOPLEVEL_RESIZE, "oou", self.seat, serial,
                                   P.RESIZE_EDGE[action]))
            return
        if kind == "title" and action == "move":
            if pressed:
                now = time.monotonic()
                if now - self.last_title_click < 0.4:
                    self.toggle_maximize()
                elif self.seat:
                    self.to_server(msg(w.toplevel, P.XDG_TOPLEVEL_MOVE, "ou", self.seat, serial))
                self.last_title_click = now
            return
        if kind == "toolbar" and action == "resize":
            if pressed and self.seat:
                self.to_server(msg(w.toplevel, P.XDG_TOPLEVEL_RESIZE, "oou", self.seat, serial,
                                   P.RESIZE_EDGE["bottom_right"]))
            return
        if pressed:
            self.pressed = (kind, action)
            self._redraw(kind)
            return
        was = self.pressed
        self.pressed = None
        self._redraw(kind)
        if was == (kind, action) and action:
            self.do_action(action)

    def toggle_maximize(self):
        w = self.window
        if not w:
            return
        op = P.XDG_TOPLEVEL_UNSET_MAXIMIZED if w.maximized else P.XDG_TOPLEVEL_SET_MAXIMIZED
        self.to_server(msg(w.toplevel, op))

    def toggle_fullscreen(self, leave_only=False):
        w = self.window
        if not w:
            return
        if w.fullscreen:
            self.to_server(msg(w.toplevel, P.XDG_TOPLEVEL_UNSET_FULLSCREEN))
        elif not leave_only:
            self.to_server(msg(w.toplevel, P.XDG_TOPLEVEL_SET_FULLSCREEN, "o", 0))

    def _toggle_fit(self):
        out = self.output_logical()
        if not out or not self.res:
            return
        fit = min(1.0, self.fit_zoom(out[0] - 2 * fr.BORDER, out[1] - PANEL_ALLOWANCE))
        self.set_zoom(1.0 if abs((self.zoom or 1) - fit) < 0.02 else fit)

    # -- actions ----------------------------------------------------------------------------
    KEYS = {"back": [158], "home": [172], "volume_up": [115], "volume_down": [114]}

    def do_action(self, action):
        w = self.window
        if action in self.KEYS:
            self.send_keys(self.KEYS[action])
        elif action == "recents":
            # KEYCODE_APP_SWITCH (580) is dropped by the hwcomposer: the session
            # asks the daemon to write it into Android's keyboard input instead
            self.emit("action key 580")
        elif action == "screenshot":
            self.emit("action screenshot")
        elif action == "fullscreen":
            self.toggle_fullscreen()
        elif action == "minimize" and w:
            self.to_server(msg(w.toplevel, P.XDG_TOPLEVEL_SET_MINIMIZED))
        elif action == "close":
            self._close_requested()

    def _close_requested(self):
        self.emit("close")
        if self.cfg.close_action != "stop" and self.window:
            self.to_server(msg(self.window.toplevel, P.XDG_TOPLEVEL_SET_MINIMIZED))

    def send_keys(self, codes, release_order=None):
        """Synthesise key presses on the HWC's wl_keyboard."""
        if not self.keyboard:
            return
        t = int(time.monotonic() * 1000) & 0xffffffff
        for code in codes:
            self.to_client(msg(self.keyboard, P.WL_KEYBOARD_EV_KEY, "uuuu", self.last_serial, t, code, 1))
        for code in (release_order or list(reversed(codes))):
            self.to_client(msg(self.keyboard, P.WL_KEYBOARD_EV_KEY, "uuuu", self.last_serial, t + 30, code, 0))


# -- I/O ----------------------------------------------------------------------------------

class Connection:
    def __init__(self, proxy, client, upstream):
        self.proxy = proxy
        self.client = client
        self.upstream = upstream
        self.session = Session(proxy.cfg, proxy.emit)
        self.tr = Translator(wlschema.load(), lambda m: sys.stderr.write("wlproxy: " + m + "\n"))
        self.c2s = Stream(self.session.on_request, self.session.post_feed_c2s, translate=self.tr.request)
        self.s2c = Stream(self._event)
        self.session.c2s, self.session.s2c = self.c2s, self.s2c
        self.closed = False

    def _event(self, obj, op, payload):
        """Translate to the HWC's id space first, then let the session decide."""
        try:
            t = self.tr.event(obj, op, payload)
        except Exception:  # noqa: BLE001
            log_error("error translating event {}.{}".format(obj, op))
            return []
        if t is HIDDEN:
            return []
        if t is None:
            RECENT.append("<< dropped untranslatable event on {}.{}".format(obj, op))
            self.drops = getattr(self, "drops", 0) + 1
            if self.drops <= 20:
                sys.stderr.write("wlproxy: dropped untranslatable event on {}.{}\n".format(obj, op))
            return []
        cobj, cpayload = t
        res = self.session.on_event(cobj, op, cpayload)
        if res is None:
            return [build_message(cobj, op, cpayload)]
        return res

    def close(self):
        if self.closed:
            return
        self.closed = True
        for s in (self.client, self.upstream):
            try:
                self.proxy.sel.unregister(s)
            except (KeyError, ValueError):
                pass
            s.close()
        for st in (self.c2s, self.s2c):
            for fd in st.fds:
                os.close(fd)
            st.fds.clear()
            for _data, fds in st.deferred:
                for fd in fds:
                    os.close(fd)
            st.deferred.clear()
        if self in self.proxy.conns:
            self.proxy.conns.remove(self)

    def update_interest(self):
        if self.closed:
            return
        for sock, inq, outq in ((self.client, self.c2s, self.s2c), (self.upstream, self.s2c, self.c2s)):
            ev = 0
            if len(inq.out) < HIGH_WATER:
                ev |= selectors.EVENT_READ
            if outq.pending():
                ev |= selectors.EVENT_WRITE
            self.proxy.sel.modify(sock, ev or selectors.EVENT_READ, self)

    def pump(self):
        try:
            flush(self.upstream, self.c2s)
            flush(self.client, self.s2c)
        except OSError:
            self.close()
            return
        self.update_interest()

    def on_event(self, sock, mask):
        src_is_client = sock is self.client
        if mask & selectors.EVENT_READ:
            stream = self.c2s if src_is_client else self.s2c
            try:
                data, fds = recv_with_fds(sock)
            except (BlockingIOError, InterruptedError):
                data, fds = None, []
            except (OSError, ProtocolError) as e:
                if not isinstance(e, (ConnectionResetError, BrokenPipeError)):
                    log_error("connection read error: {}".format(e))
                self.close()
                return
            if data is not None:
                if not data:
                    other = self.upstream if src_is_client else self.client
                    try:
                        flush(other, stream)
                    except OSError:
                        pass
                    self.close()
                    return
                try:
                    stream.feed(data, fds)
                except ProtocolError as e:
                    log_error("protocol error, closing connection: {}".format(e))
                    self.close()
                    return
        self.pump()


class Config:
    def __init__(self, inst_id, name, width=0, height=0, zoom="auto", frame=True, theme="dark",
                 close_action="stop"):
        self.id = inst_id
        self.name = name
        self.width = int(width or 0)
        self.height = int(height or 0)
        self.zoom = zoom
        self.frame = frame
        self.theme = theme
        self.close_action = close_action


class Proxy:
    def __init__(self, listen, upstream, cfg, out=sys.stdout):
        self.listen_path = listen
        self.upstream_path = upstream
        self.cfg = cfg
        self.out = out
        self.sel = selectors.DefaultSelector()
        self.server = None
        self.conns = []

    def emit(self, ev):
        try:
            self.out.write(ev + "\n")
            self.out.flush()
        except (OSError, ValueError):
            pass

    def bind(self):
        os.makedirs(os.path.dirname(self.listen_path), mode=0o700, exist_ok=True)
        try:
            os.unlink(self.listen_path)
        except FileNotFoundError:
            pass
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(self.listen_path)
        # Android processes run as various uids inside the container
        os.chmod(self.listen_path, 0o777)
        srv.listen(16)
        srv.setblocking(False)
        self.server = srv
        self.sel.register(srv, selectors.EVENT_READ, None)

    def accept(self):
        try:
            client, _ = self.server.accept()
        except (BlockingIOError, InterruptedError):
            return
        up = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            up.connect(self.upstream_path)
        except OSError:
            client.close()
            up.close()
            return
        client.setblocking(False)
        up.setblocking(False)
        conn = Connection(self, client, up)
        self.conns.append(conn)
        self.sel.register(client, selectors.EVENT_READ, conn)
        self.sel.register(up, selectors.EVENT_READ, conn)

    def dump_state(self):
        lines = ["{} state dump".format(time.strftime("%Y-%m-%d %H:%M:%S"))]
        for i, c in enumerate(self.conns):
            se = c.session
            w = se.window
            lines.append("connection {}: zoom={} res={} window={} fullscreen={} fill={} frame={}".format(
                i, se.zoom, se.res, bool(w), w and w.fullscreen, w and w.fill_size, bool(w and w.frame)))
            lines.append("  pings={pings} pongs={pongs} last_latency={last_latency:.3f}s "
                         "max_latency={max_latency:.3f}s outstanding={n}".format(n=len(se.pings), **se.ping_stats))
            lines.append("  c2s out={} deferred={} inbuf={} fds={} | s2c out={} deferred={} inbuf={} fds={}".format(
                len(c.c2s.out), len(c.c2s.deferred), len(c.c2s.inbuf), len(c.c2s.fds),
                len(c.s2c.out), len(c.s2c.deferred), len(c.s2c.inbuf), len(c.s2c.fds)))
            lines.append("  dropped events={} ids mapped={}".format(getattr(c, "drops", 0), len(c.tr.c2s)))
        lines.append("recent messages (oldest first):")
        lines.extend("  " + r for r in RECENT)
        if LOG_PATH:
            with open(LOG_PATH, "a") as f:
                f.write("\n".join(lines) + "\n\n")

    def run(self):
        if self.server is None:
            self.bind()
        self.emit("ready")
        while True:
            for key, mask in self.sel.select():
                if key.data is None:
                    self.accept()
                else:
                    key.data.on_event(key.fileobj, mask)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--listen", required=True)
    p.add_argument("--upstream", required=True)
    p.add_argument("--id", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--width", type=int, default=0)
    p.add_argument("--height", type=int, default=0)
    p.add_argument("--zoom", default="auto")
    p.add_argument("--frame", default="on", choices=("on", "off"))
    p.add_argument("--theme", default="dark", choices=("dark", "light"))
    p.add_argument("--close-action", default="stop", choices=("stop", "freeze", "none"))
    o = p.parse_args(argv)
    cfg = Config(o.id, o.name, o.width, o.height, o.zoom, o.frame == "on", o.theme, o.close_action)
    global LOG_PATH
    cache = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    os.makedirs(os.path.join(cache, "waydroid-multi"), exist_ok=True)
    LOG_PATH = os.path.join(cache, "waydroid-multi", "wlproxy-{}.log".format(o.id))

    def on_term(*_):
        sys.exit(0)
    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGPIPE, signal.SIG_IGN)
    proxy = Proxy(o.listen, o.upstream, cfg)
    signal.signal(signal.SIGUSR1, lambda *_: proxy.dump_state())   # waydroid-multi log <id> --window
    try:
        proxy.run()
    except KeyboardInterrupt:
        pass
    except SystemExit:
        raise
    except BaseException:
        log_error("proxy crashed")
        raise


if __name__ == "__main__":
    main()
