# SPDX-License-Identifier: GPL-3.0-or-later
"""Wayland proxy between an instance's hwcomposer (HWC) and the real compositor.

It gives every instance window an identity and a frame:

* **Labels**: ``xdg_toplevel.set_title/set_app_id`` become "Waydroid · <name>"
  and "waydroid-manager.<id>[.<pkg>]", so each instance is its own app in the dock.
* **Zoom**: Android keeps rendering at its resolution while the window is shown
  smaller or larger. The HWC's own viewport/subsurface/geometry requests are
  scaled and input coordinates are scaled back. The HWC scale is normalised
  to 1 (``wl_output.scale`` and fractional scale are rewritten), and configure
  sizes are hidden from it (any real size triggers an Android display hotplug).
* **Frame** (full-UI window only): a title bar (move, minimize, close), an
  attached side toolbar (Settings, Restart, Back, Home, Recents, volume, screenshot,
  Install APK, fullscreen) with tooltips, and an invisible resize border. These are proxy-owned
  subsurfaces; events for them are never forwarded to the HWC, which aborts on
  unknown object ids.
* **Clipboard**: the HWC reads a new selection on its only Wayland thread, blocking
  until the owner has sent it, so a slow owner (or the HWC itself, after a copy in
  Android) holds back its pongs and GNOME calls the window not responding. The
  proxy fetches the text itself and answers the HWC's reads (see Clip).
* **APK drops**: files dragged onto the window are taken over from the HWC (it has no
  use for them); the APKs and XAPKs among them are reported for installing.

Toolbar keys are delivered as synthetic ``wl_keyboard.key`` events (the HWC
forwards evdev codes and needs no focus; codes >= 239 are dropped, hence
Alt+Tab for Recents).

Run as a separate process (no gbinder here):
  python3 -m waydroid_manager.session.wlproxy --listen SOCK --upstream SOCK --id ID --name NAME \\
      [--width W --height H --zoom auto|PCT --theme dark|light --close-action stop|freeze|none]
Events are written to stdout, one per line: "ready", "close", "zoom <pct>", "action <name>",
"install <path>". Android's display rotation (0-3) is read from stdin, one per line: the picture
and the window turn with it (wl_surface.set_buffer_transform), input is turned back.
"""
import argparse
import array
import collections
import math
import mmap
import os
import selectors
import signal
import socket
import struct
import sys
import threading
import time
import urllib.parse

from . import frame as fr
from . import gbm
from . import wlschema
from .wlproto import (HEADER, MAX_MSG, ProtocolError, Reader, msg)
from . import wlproto as P

MAX_FDS_PER_MSG = 28        # libwayland's MAX_FDS_OUT; receivers truncate beyond this
RECV_SIZE = 65536
HIGH_WATER = 4 * 1024 * 1024
FULL_UI_APP_ID = "Waydroid"
MY_ID_BASE = 0xfe000000      # proxy-created objects in client space (mapped to real ids by Translator)
FULL_DAMAGE = 0x7fffffff
GRIP_KINDS = ("grip_left", "grip_bottom")   # our resize strips inside the picture's edges
ZOOM_MIN, ZOOM_MAX = 0.25, 2.0
PANEL_ALLOWANCE = 64          # room for a desktop panel when fitting to the screen
# Clipboard text the HWC reads, in its order of preference
CLIP_TYPES = ("text/plain;charset=utf-8", "UTF8_STRING", "text/plain", "TEXT", "STRING")
CLIP_WAIT = 1.0               # longest the HWC waits on a slow clipboard owner (GNOME pings time out at 5 s)
CLIP_MAX = 1 << 20            # more text than this is not passed to Android
URI_LIST = "text/uri-list"    # what file managers offer when files are dragged


def apk_paths(uri_list):
    """Local .apk and .xapk paths in a dropped text/uri-list."""
    paths = []
    for line in uri_list.decode("utf-8", "replace").splitlines():
        u = urllib.parse.urlsplit(line.strip())
        path = urllib.parse.unquote(u.path)
        if (u.scheme == "file" and u.netloc in ("", "localhost") and path.lower().endswith((".apk", ".xapk"))
                and "\n" not in path and "\r" not in path):
            paths.append(path)
    return paths


def rotate(x, y, w, h, t):
    """Point (x, y) of a w x h frame, in that frame turned t * 90 degrees clockwise: what the
    compositor does to a buffer with wl_output transform t (0-3)."""
    return ((x, y), (h - y, x), (w - x, h - y), (y, w - x))[t & 3]


def rotate_rect(x, y, rw, rh, w, h, t):
    (ax, ay), (bx, by) = rotate(x, y, w, h, t), rotate(x + rw, y + rh, w, h, t)
    return min(ax, bx), min(ay, by), abs(bx - ax), abs(by - ay)


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

    The handler returns None (forward unchanged) or a list of replacement messages
    (byte strings, or (bytes, fds) for ones carrying new fds; [] drops the message). Proxy-originated messages are
    added with inject(); those carrying fds wait until no partial message is
    buffered, so every fd stays ahead of (or with) the message that uses it.

    With count_fds ((obj, opcode) -> fds the message carries), fds are tracked per
    message: only those of messages already queued are sent, and a handler can
    take_fd() the fds of the message it drops.
    """

    def __init__(self, handler, post_feed=None, translate=None, count_fds=None):
        self.handler = handler
        self.post_feed = post_feed
        self.translate = translate   # applied when a message is appended for sending
        self.count_fds = count_fds
        self.inbuf = bytearray()
        self.out = bytearray()
        self.fds = []
        self.out_fds = 0             # leading fds that belong to messages in out (count_fds only)
        self.cur = None              # [index, count] of the handled message's fds still queued
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
            n = self.count_fds(obj, word & 0xffff) if self.count_fds else 0
            # None when its fds didn't all arrive: it is then forwarded as is
            self.cur = [self.out_fds, n] if self.out_fds + n <= len(self.fds) else None
            repl = self.handler(obj, word & 0xffff, payload)
            left = self.cur[1] if self.cur else 0
            self.cur = None
            if repl is None:
                self._append(bytes(buf[pos:pos + size]), owned=left)
            elif repl:
                for i, m in enumerate(repl):
                    data, fds = m if isinstance(m, tuple) else (m, ())
                    self._append(data, fds, owned=left if i == 0 else 0)
            else:
                self._close_owned(left)
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

    def take_fd(self):
        """Next fd of the message being handled, now the caller's to close; None when
        fds aren't tracked (the message must then be forwarded unchanged)."""
        c = self.cur
        if not c or not c[1]:
            return None
        c[1] -= 1
        return self.fds.pop(c[0])

    def peek_fd(self):
        """A copy of the next fd of the message being handled, which keeps its own; None when
        fds aren't tracked."""
        c = self.cur
        return os.dup(self.fds[c[0]]) if c and c[1] else None

    def _close_owned(self, n):
        for _ in range(n):
            os.close(self.fds.pop(self.out_fds))

    def _append(self, data, fds=(), owned=0):
        """owned: fds of this message already queued (received with it)."""
        if self.translate is not None:
            data = self.translate(data)
            if data is None:
                for fd in fds:
                    os.close(fd)
                self._close_owned(owned)
                return
        if self.count_fds:
            self.out_fds += owned
            self.fds[self.out_fds:self.out_fds] = fds   # ahead of fds received for later messages
            self.out_fds += len(fds)
        else:
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
        ready = stream.fds[:stream.out_fds] if stream.count_fds else stream.fds
        fds = ready[:MAX_FDS_PER_MSG]
        # With more fds than one message may carry, send one byte per batch
        chunk = bytes(stream.out[:1] if len(ready) > MAX_FDS_PER_MSG else stream.out[:RECV_SIZE])
        anc = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", fds))] if fds else []
        try:
            n = sock.sendmsg([chunk], anc)
        except (BlockingIOError, InterruptedError):
            return False
        for fd in fds:
            os.close(fd)
        del stream.fds[:len(fds)]
        if stream.count_fds:
            stream.out_fds -= len(fds)
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

    def request_fds(self, obj, op):
        """How many fds a client request carries (they travel out of band)."""
        iface = self.schema.get(self.iface.get(obj))
        if iface is None or op >= len(iface.requests):
            return 0
        return sum(1 for a in iface.requests[op] if a.type == "fd")

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

class Clip:
    """Clipboard text of one selection offer, fetched by the proxy for the HWC.

    The HWC reads the clipboard on its only Wayland thread, blocking until the owner
    has sent it all, so a slow owner holds back its pongs and GNOME calls the window
    not responding. The proxy fetches the text itself (a thread reads the pipe) and
    answers the HWC's receive, waiting at most CLIP_WAIT."""

    def __init__(self, fd, stats):
        self.data = b""
        self.done = threading.Event()
        self.stats = stats
        if fd is None:
            self.done.set()
        else:
            threading.Thread(target=self._read, args=(fd,), daemon=True, name="clip-read").start()

    def _read(self, fd):
        chunks, size = [], 0
        try:
            while size <= CLIP_MAX:
                b = os.read(fd, 65536)
                if not b:
                    self.data = b"".join(chunks)
                    break
                chunks.append(b)
                size += len(b)
        except OSError:
            pass
        finally:
            os.close(fd)
            self.done.set()

    def answer(self, fd):
        """Write the text to the HWC's pipe once fetched (nothing if it takes too long)."""
        def run():
            try:
                if self.done.wait(CLIP_WAIT):
                    self.stats["answered"] += 1
                    view = memoryview(self.data)
                    while view:
                        view = view[os.write(fd, view):]
                else:
                    self.stats["late"] += 1
            except OSError:
                pass
            finally:
                os.close(fd)
        threading.Thread(target=run, daemon=True, name="clip-answer").start()


class Surf:
    __slots__ = ("id", "viewport", "parent", "sub", "dirty", "req_dest", "req_pos", "req_transform", "req_src",
                 "children")

    def __init__(self, sid):
        self.id = sid
        self.viewport = None   # HWC's wp_viewport on this surface
        self.parent = None     # parent surface id (for subsurfaces)
        self.sub = None        # wl_subsurface id
        self.dirty = False     # HWC has pending (uncommitted) state on it
        self.req_dest = None   # destination the HWC asked for (unscaled)
        self.req_pos = None    # subsurface position the HWC asked for (unscaled)
        self.req_transform = 0  # buffer transform the HWC asked for
        self.req_src = None    # viewport source the HWC asked for (wl_fixed x, y, w, h)
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
        self.resize_axis = None
        self.pending_apply = False
        self.frame = None        # dict of our objects once created
        self.saved_zoom = None
        self.own_vp = None       # our wp_viewport on the surface, when the HWC has none (Android 11)


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
        self.initial_sync = None
        self.pending_outputs = []  # announce outputs last during the initial registry roundtrip
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
        self.comp_version = 0    # of the HWC's wl_compositor (buffer transforms need 2)
        self.surface_output = None
        self.shown_turn = 0      # the turn the tree's buffer transforms were last set for
        self.window = None
        self.zoom = None         # current zoom factor (float), None until known
        self.res = (cfg.width, cfg.height) if cfg.width and cfg.height else None
        # proxy-owned objects
        self.next_id = MY_ID_BASE
        self.mine = {}           # id -> kind
        self.my_globals = {}     # interface -> our bound object id
        self.buffers = {}        # our buffer id -> ("shm"|"pixel")
        self.shm = None          # the HWC's wl_shm, and the formats the compositor takes through it
        self.shm_formats = set()
        self.dmabuf_planes = {}  # cpu_buffers: params id -> [(fd copy, offset, stride, modifier)]
        self.copies = {}         # cpu_buffers through a GPU: buffer id -> (gbm buffer, width, height, our memory, stride)
        self.dmabuf_kept = set()  # (planes, modifier, format) passed on as dmabufs, noted once each
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
        self.tip_due = None      # (monotonic time, toolbar action) to show a tooltip at
        self.tip_shown = None    # toolbar action whose tooltip is up
        self.tb_scroll = 0       # how far the toolbar's entries are scrolled up (short windows)
        self.cursor_dev = None
        self.cursor_shown = None  # the shape we last set over our frame
        self.cursor_surface = None  # the HWC's cursor (Android's pointer, drawn turned with its display)
        self.pings = {}           # serial -> time the compositor pinged
        self.ping_stats = {"pings": 0, "pongs": 0, "max_latency": 0.0, "last_latency": 0.0}
        self.offers = {}          # wl_data_offer id -> mime types offered
        self.clips = {}           # selection offer id -> Clip
        self.clip_stats = {"answered": 0, "late": 0}
        self.own_source = None    # the HWC's wl_data_source while it owns the selection
        self.dnd_version = 1      # of the HWC's wl_data_device_manager (actions and finish need 3)
        self.file_drag = None     # offer of the file drag over the window, kept from the HWC
        self.drag_action = 0      # what the compositor chose for it
        self.watch = None         # (fd, done(bytes)) -> read fd to EOF from the main loop

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

    @property
    def turn(self):
        """wl_output transform that shows Android's picture upright. Android draws in its
        display's natural orientation; its ROTATION_90 is the picture turned 90 degrees clockwise
        there, so the compositor turns it back (transform 3: 270 degrees clockwise)."""
        return (4 - self.cfg.rotation) % 4 if self.comp_version >= 2 else 0

    def view(self):
        """Android's resolution as the picture is shown: res, turned with Android's display."""
        if self.res and self.turn & 1:
            return self.res[1], self.res[0]
        return self.res

    def turned_pos(self, s):
        """Where a subsurface the HWC placed in Android's frame goes in the turned picture."""
        x, y = s.req_pos
        if not self.turn or not self.res:
            return x, y
        # ponytail: a subsurface without a viewport counts as a point; the HWC's have viewports
        w, h = s.req_dest or (0, 0)
        return rotate_rect(x, y, w, h, *self.res, self.turn)[:2]

    def turned_src(self, src):
        """A viewport source (wl_fixed) in the turned buffer's coordinates."""
        if self.turn & 1 and src != (-256,) * 4:
            # ponytail: exact for crops at the origin (the framebuffer target); a compositing
            # HWC's other crops land on the mirrored region, still inside the buffer
            return src[1], src[0], src[3], src[2]
        return src

    def turned_transform(self, t):
        return (t & 4) | ((t + self.turn) & 3)

    def content_size(self):
        z = self.zoom or 1.0
        rw, rh = self.view() or (0, 0)
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

    def centring(self):
        """How far the picture is from the area's top-left when it is centred in the area."""
        a = self.area()
        if not a or not self.res:
            return 0, 0
        dw, dh = self.content_size()
        return max(0, (a[0] - dw) // 2), max(0, (a[1] - dh) // 2)

    def content_offset(self):
        """Where the HWC's subsurfaces go in the toplevel surface (see area_origin for Android 11)."""
        return (0, 0) if self.window and self.window.own_vp else self.centring()

    def area_origin(self):
        """The area's top-left in the toplevel surface. Android 11 draws its picture on the toplevel
        itself, which can't move: the window (geometry and frame) moves around it instead. Not in
        fullscreen: GNOME centres a fullscreen window's surfaces on black itself, and would centre
        our offset backdrop instead of the picture."""
        if not self.window or not self.window.own_vp or self.window.fullscreen:
            return 0, 0
        cx, cy = self.centring()
        return -cx, -cy

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
        vw, vh = self.view()
        z = min((avail_w - b) / vw, (avail_h - t) / vh)
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
        if self.turn & 1:
            w, h = h, w
        return max(1, int(math.ceil(w * z))), max(1, int(math.ceil(h * z)))

    def scaled_pos(self, x, y):
        z = self.zoom or 1.0
        cx, cy = self.content_offset()
        return int(round(x * z)) + cx, int(round(y * z)) + cy

    def geometry(self):
        w = self.window
        ox, oy = self.area_origin()
        if w and w.fullscreen and w.fs_size:
            return ox, oy, w.fs_size[0], w.fs_size[1]
        aw, ah = self.area() or self.content_size()
        b, t = self.frame_extent()
        return ox, oy - t, aw + b, ah + t

    def unscale_point(self, sid, x_fixed, y_fixed, pointer=False):
        """Compositor surface coords -> what the HWC expects (Android px at scale 1)."""
        z = self.zoom or 1.0
        x, y = P.fixed_to_float(x_fixed), P.fixed_to_float(y_fixed)
        if self.window and sid == self.window.surface:
            cx, cy = self.content_offset()
            x, y = x - cx, y - cy
            if self.res:
                dw, dh = self.content_size()
                x, y = min(max(x, 0.0), dw - 0.01), min(max(y, 0.0), dh - 0.01)
        x, y = x / z, y / z
        if self.turn and self.res and not (pointer and self.cfg.logical_pointer):
            # back to Android's natural orientation: its input reader turns touches itself
            size = self.surfaces[sid].req_dest if sid != self.window.surface else None
            rw, rh = size or self.res     # HWC adds the layer's offset after receiving local input
            if self.turn & 1:
                rw, rh = rh, rw
            x, y = rotate(x, y, rw, rh, (4 - self.turn) % 4)
        return P.float_to_fixed(x), P.float_to_fixed(y)

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
            elif op == P.WL_DISPLAY_SYNC and self.initial_sync is None and self.pending_outputs is not None:
                self.initial_sync = r.n()
            return None
        if iface is None:
            return None
        if iface == "wl_registry" and op == P.WL_REGISTRY_BIND:
            r.u()
            name_iface = r.s()
            version = r.u()
            new = r.n()
            self.objs[new] = name_iface
            if name_iface == "wl_data_device_manager":
                self.dnd_version = version
            elif name_iface == "wl_seat":
                self.seat = new
            elif name_iface == "wl_output":
                self.outputs.setdefault(new, {})
            elif name_iface == "wl_shm":
                self.shm = new
            elif name_iface == "wl_compositor":
                self.comp_version = version
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
            w = self.window
            if w and w.frame and parent == w.surface:
                # New HWC layers would cover the toolbar's tooltip and our inside-edge grips.
                return [msg(obj, op, "noo", new, sid, parent)] + [
                    msg(w.frame[kind + "_sub"], P.WL_SUBSURFACE_PLACE_ABOVE, "o", sid)
                    for kind in ("toolbar",) + GRIP_KINDS]
            return None
        if iface == "wp_viewporter" and op == P.WP_VIEWPORTER_GET_VIEWPORT:
            new, sid = r.n(), r.o()
            self.objs[new] = "wp_viewport"
            self.viewports[new] = sid
            if sid in self.surfaces:
                self.surfaces[sid].viewport = new
            w = self.window
            if w and w.own_vp and sid == w.surface:     # a surface has one viewport: ours goes
                own, w.own_vp = w.own_vp, None
                return [msg(own, P.WP_VIEWPORT_DESTROY), msg(obj, op, "no", new, sid)]
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
        if iface == "wl_pointer" and op == P.WL_POINTER_SET_CURSOR:
            r.u()
            self.cursor_surface = r.o() or None
            return None
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
        if iface == "wl_data_device_manager":
            if op == P.WL_DATA_DEVICE_MANAGER_GET_DATA_DEVICE:
                self.objs[r.n()] = "wl_data_device"
            elif op == P.WL_DATA_DEVICE_MANAGER_CREATE_DATA_SOURCE:
                self.objs[r.n()] = "wl_data_source"
            return None
        if iface == "wl_data_device" and op == P.WL_DATA_DEVICE_SET_SELECTION:
            self.own_source = r.o() or None
            return None
        if iface == "wl_data_offer":
            return self._offer_request(obj, op)
        if iface == "wl_surface":
            return self._surface_request(obj, op, r)
        if iface == "wp_viewport":
            return self._viewport_request(obj, op, r)
        if iface == "wl_subsurface":
            return self._subsurface_request(obj, op, r)
        if self.cfg.cpu_buffers and iface in ("zwp_linux_dmabuf_v1", "zwp_linux_buffer_params_v1"):
            return self._dmabuf_request(obj, iface, op, r)
        return None

    def _dmabuf_request(self, obj, iface, op, r):
        """cpu_buffers: Android's dmabufs reach the compositor as shared memory. A compositor may
        not import dmabufs from another device (GNOME on NVIDIA doesn't, from vkms or an iGPU).
        "shared": vkms's linear ones go over the same fd, mapped where they are. A render node:
        the buffer gets memory of our own, which each attach fills through that GPU (cfg.gpu)."""
        if iface == "zwp_linux_dmabuf_v1":
            if op == P.ZWP_LINUX_DMABUF_CREATE_PARAMS:
                self.objs[r.n()] = "zwp_linux_buffer_params_v1"
            return None
        if op == P.ZWP_LINUX_BUFFER_PARAMS_ADD:
            fd = self.c2s.peek_fd()
            if fd is not None:
                r.u()                       # plane index
                offset, stride, hi, lo = r.u(), r.u(), r.u(), r.u()
                self.dmabuf_planes.setdefault(obj, []).append((fd, offset, stride, hi << 32 | lo))
            return None
        if op not in (P.ZWP_LINUX_BUFFER_PARAMS_CREATE_IMMED, P.ZWP_LINUX_BUFFER_PARAMS_DESTROY):
            return None
        planes = self.dmabuf_planes.pop(obj, [])
        try:
            if op == P.ZWP_LINUX_BUFFER_PARAMS_DESTROY or self.shm is None:
                return None
            buf, width, height, fmt = r.n(), r.i(), r.i(), r.u()
            kind = (len(planes), planes[0][3] if planes else None, fmt)
            if len(planes) != 1 and kind not in self.dmabuf_kept:
                self.dmabuf_kept.add(kind)
                sys.stderr.write("wlproxy: dmabuf with {} planes left as is\n".format(len(planes)))
            if len(planes) != 1:
                return None
            fd, offset, stride, modifier = planes[0]
            code = {P.DRM_FORMAT_ARGB8888: 0, P.DRM_FORMAT_XRGB8888: 1}.get(fmt, fmt)
            size = offset + stride * height
            pool_fd = None
            if code not in self.shm_formats:
                pass
            elif self.cfg.gpu:
                bo = self.cfg.gpu.import_buffer(fd, width, height, fmt, offset, stride, modifier)
                if bo:
                    pool_fd, offset, size = os.memfd_create("waydroid-frame", os.MFD_CLOEXEC), 0, stride * height
                    os.ftruncate(pool_fd, size)
                    self.copies[buf] = (bo, width, height, mmap.mmap(pool_fd, size), stride)
            elif modifier in (P.DRM_FORMAT_MOD_LINEAR, P.DRM_FORMAT_MOD_INVALID) and \
                    os.lseek(fd, 0, os.SEEK_END) >= size:
                pool_fd = fd
                planes.clear()              # the fd now travels with create_pool
            if pool_fd is None:
                if kind not in self.dmabuf_kept:
                    self.dmabuf_kept.add(kind)
                    sys.stderr.write("wlproxy: dmabuf modifier {:#x} format {:#x} left as is\n".format(modifier, fmt))
                return None
            pool = self.new_id("pool")
            self.objs[buf] = "wl_buffer"
            return [(msg(self.shm, P.WL_SHM_CREATE_POOL, "ni", pool, size), [pool_fd]),
                    msg(pool, P.WL_SHM_POOL_CREATE_BUFFER, "niiiiu", buf, offset, width, height, stride, code),
                    msg(pool, P.WL_SHM_POOL_DESTROY)]
        finally:
            for plane in planes:
                os.close(plane[0])

    def _surface_request(self, sid, op, r):
        s = self.surfaces.get(sid)
        if s is None:
            return None
        if op == P.WL_SURFACE_ATTACH:
            copy = self.copies.get(r.o())
            if copy:
                self.cfg.gpu.read(*copy)    # the frame Android drew, into the memory the compositor reads
        tree = self.in_tree(sid)
        if op == P.WL_SURFACE_SET_BUFFER_TRANSFORM:
            s.req_transform = r.i()
            if tree or sid == self.cursor_surface:
                s.dirty = True
                return [msg(sid, op, "i", self.turned_transform(s.req_transform))]
            return None
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
        if op == P.WL_SURFACE_DAMAGE and (self.zoom not in (None, 1.0) or self.turn):
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
                self.surfaces[sid].req_src = None
            return None
        if op == P.WP_VIEWPORT_SET_SOURCE and sid in self.surfaces:
            # kept for any surface: one joining the tree later must not turn under an old crop
            self.surfaces[sid].req_src = (r.i(), r.i(), r.i(), r.i())
        if sid in self.surfaces and sid == self.cursor_surface:
            return self._cursor_viewport_request(vid, op, r)
        if not self.in_tree(sid):
            return None
        s = self.surfaces[sid]
        s.dirty = True
        if op == P.WP_VIEWPORT_SET_SOURCE:
            return [msg(vid, op, "iiii", *self.turned_src(s.req_src))]
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

    def _cursor_viewport_request(self, vid, op, r):
        """Android's pointer is drawn turned with its display, like the picture: turn it back."""
        if op == P.WP_VIEWPORT_SET_SOURCE:
            return [msg(vid, op, "iiii", *self.turned_src(self.surfaces[self.cursor_surface].req_src))]
        if op == P.WP_VIEWPORT_SET_DESTINATION and self.turn & 1:
            w, h = r.i(), r.i()
            return [msg(vid, op, "ii", h, w)]
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
        px, py = self.scaled_pos(*self.turned_pos(s))
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
                self.toplevels.get(tid, {}).get("app_id") in (None, FULL_UI_APP_ID):
            # The HWC's calibration maximize would skew the zoom; the window size is ours. It
            # comes before set_app_id, so a toplevel not yet known as an app's counts as full-UI.
            return []
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
        base = "waydroid-manager.{}".format(self.cfg.id)
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
        if obj == self.initial_sync and op == P.WL_CALLBACK_EV_DONE:
            pending, self.pending_outputs = self.pending_outputs, None
            self.initial_sync = None
            return pending + [build_message(obj, op, payload)] if pending else None
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
                if gi == "wl_output" and self.pending_outputs is not None:
                    # Let the HWC's output roundtrip drain dmabuf modifiers before starting its dispatch thread.
                    self.pending_outputs.append(build_message(obj, op, payload))
                    return []
            elif op == P.WL_REGISTRY_EV_GLOBAL_REMOVE:
                self.globals.pop(r.u(), None)
                if self.pending_outputs is not None:
                    self.pending_outputs.append(build_message(obj, op, payload))
                    return []
            return None
        if iface == "wl_shm" and op == P.WL_SHM_EV_FORMAT:
            self.shm_formats.add(r.u())
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
        if iface == "wl_data_offer" and op == P.WL_DATA_OFFER_EV_OFFER and obj in self.offers:
            self.offers[obj].append(r.s())
            return None
        if iface == "wl_data_offer" and op == P.WL_DATA_OFFER_EV_ACTION and obj == self.file_drag:
            self.drag_action = r.u()
            return []
        if iface == "wl_data_source" and op == P.WL_DATA_SOURCE_EV_CANCELLED and obj == self.own_source:
            self.own_source = None
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
            if op == P.XDG_TOPLEVEL_EV_CONFIGURE and tid in self.toplevels:
                return self._app_configure(self.toplevels[tid], tid, op, r)
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

    @staticmethod
    def _app_configure(t, tid, op, r):
        """An app window: the HWC hotplugs Android's display (new buffers, touch reset, a
        visible stall) on every sized configure, and the desktop sends one on each focus
        change. Only a new size gets through, so resizing still works."""
        width, height, raw_states = r.i(), r.i(), r.a()
        if (width, height) == t.get("size"):
            width = height = 0
        elif width > 1 and height > 1:
            t["size"] = (width, height)
        return [msg(tid, op, "iia", width, height, raw_states)]

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
                # Keep the first changed axis through the final configure: compositors can leave
                # the undragged side fixed while our aspect-preserving geometry changes both.
                axis = w.resize_axis
                if axis is None:
                    axis = 0 if abs(width - gw) >= abs(height - gh) else 1
                    if w.resizing and (width, height) != (gw, gh):
                        w.resize_axis = axis
                far = 1 << 30
                self.zoom = self.fit_zoom(width, far) if axis == 0 else \
                    self.fit_zoom(far, height)
                changed = True
        if not w.resizing:
            w.resize_axis = None
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
                ux, uy = self.unscale_point(sid, x, y, pointer=True)
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
            elif op == P.WL_POINTER_EV_AXIS:
                r.u()
                if r.u() == 0:   # vertical
                    self._scroll_toolbar(focus[1], P.fixed_to_float(r.f()))
            self.ptr_group_dropped = True
            return []
        self.ptr_group_forwarded = True
        if op == P.WL_POINTER_EV_BUTTON:
            self.last_serial = r.u()
            return None
        if op == P.WL_POINTER_EV_MOTION and focus and focus[0] == "tree":
            t, x, y = r.u(), r.f(), r.f()
            ux, uy = self.unscale_point(focus[1], x, y, pointer=True)
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
        if op == P.WL_DATA_DEVICE_EV_DATA_OFFER:
            offer = r.n()
            self.objs[offer] = "wl_data_offer"
            self.offers[offer] = []
            return None
        if op == P.WL_DATA_DEVICE_EV_SELECTION:
            self._clip_selection(r.o())
            return None
        if op == P.WL_DATA_DEVICE_EV_ENTER:
            serial, sid, x, y, offer = r.u(), r.o(), r.f(), r.f(), r.o()
            self.file_drag = None
            if URI_LIST in self.offers.get(offer, ()) and (sid in self.mine or self.in_tree(sid)):
                return self._file_drag(offer, serial)
            if sid in self.mine:
                return []
            if self.in_tree(sid):
                self._dnd_surface = sid
                ux, uy = self.unscale_point(sid, x, y)
                return [msg(did, op, "uoffo", serial, sid, ux, uy, offer)]
            self._dnd_surface = None
            return None
        if self.file_drag and op in (P.WL_DATA_DEVICE_EV_MOTION, P.WL_DATA_DEVICE_EV_LEAVE,
                                     P.WL_DATA_DEVICE_EV_DROP):
            if op == P.WL_DATA_DEVICE_EV_DROP:
                self._drop_files(self.file_drag)
            elif op == P.WL_DATA_DEVICE_EV_LEAVE:          # also after a drop: never the HWC's
                self.file_drag = None
            return []
        if op == P.WL_DATA_DEVICE_EV_MOTION and getattr(self, "_dnd_surface", None):
            t, x, y = r.u(), r.f(), r.f()
            ux, uy = self.unscale_point(self._dnd_surface, x, y)
            return [msg(did, op, "uff", t, ux, uy)]
        return None

    def _file_drag(self, offer, serial):
        """Files dragged onto the window: accept them as copies, unseen by the HWC."""
        self._dnd_surface = None
        self.file_drag, self.drag_action = offer, 0
        self.to_server(msg(offer, P.WL_DATA_OFFER_ACCEPT, "us", serial, URI_LIST))
        if self.dnd_version >= 3:
            copy = P.DND_ACTION_COPY
            self.to_server(msg(offer, P.WL_DATA_OFFER_SET_ACTIONS, "uu", copy, copy))
        return []

    def _drop_files(self, offer):
        """Read the dropped file list; report its APKs, then end the drag for the source."""
        if not self.watch:
            return
        finish = self.dnd_version >= 3 and self.drag_action != 0   # finish without an action is an error
        rfd, wfd = os.pipe()
        self.to_server(msg(offer, P.WL_DATA_OFFER_RECEIVE, "s", URI_LIST), fds=[wfd])

        def done(data):
            for path in apk_paths(data):
                self.emit("install " + path)
            if finish and offer in self.offers:            # the HWC may have destroyed it meanwhile
                self.to_server(msg(offer, P.WL_DATA_OFFER_FINISH))
        self.watch(rfd, done)

    def _clip_selection(self, offer):
        """A new selection: start fetching its text for the HWC (see Clip)."""
        mime = next((m for m in CLIP_TYPES if m in self.offers.get(offer, ())), None)
        if not offer or mime is None:
            return
        if self.own_source:
            # The HWC's own text: fetching it would wait on the HWC, which is busy reading
            self.clips[offer] = Clip(None, self.clip_stats)
            return
        rfd, wfd = os.pipe()
        self.to_server(msg(offer, P.WL_DATA_OFFER_RECEIVE, "s", mime), fds=[wfd])
        self.clips[offer] = Clip(rfd, self.clip_stats)

    def _offer_request(self, offer, op):
        if op == P.WL_DATA_OFFER_RECEIVE and offer in self.clips:
            fd = self.c2s.take_fd()
            if fd is not None:
                self.clips[offer].answer(fd)
                return []
        elif op == P.WL_DATA_OFFER_DESTROY:
            self.offers.pop(offer, None)
            self.clips.pop(offer, None)
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
        turning = bool(self.turn or self.shown_turn)
        self.shown_turn = self.turn
        if turning:
            out.extend(self._turn_messages(S))
        if S.viewport and S.req_dest:
            out.append(msg(S.viewport, P.WP_VIEWPORT_SET_DESTINATION, "ii", *self.scaled_dest(S.id, *S.req_dest)))
        elif not S.viewport and (w.own_vp or self._own_viewport()):
            out.append(msg(w.own_vp, P.WP_VIEWPORT_SET_DESTINATION, "ii", *self.content_size()))
        for cid in S.children:
            c = self.surfaces.get(cid)
            if not c or c.parent != S.id:
                continue
            if turning:
                out.extend(self._turn_messages(c))
            if c.sub and c.req_pos is not None:
                out.append(msg(c.sub, P.WL_SUBSURFACE_SET_POSITION, "ii", *self.scaled_pos(*self.turned_pos(c))))
            if c.viewport and c.req_dest:
                out.append(msg(c.viewport, P.WP_VIEWPORT_SET_DESTINATION, "ii", *self.scaled_dest(cid, *c.req_dest)))
            if commit and not c.dirty and (turning or (c.viewport and c.req_dest)):
                out.append(msg(cid, P.WL_SURFACE_COMMIT))
        if w.req_geometry is not None or w.committed:
            out.append(msg(w.xdg_surface, P.XDG_SURFACE_SET_WINDOW_GEOMETRY, "iiii", *self.geometry()))
        out.extend(self._frame_messages())
        if commit and not S.dirty:
            out.append(msg(S.id, P.WL_SURFACE_COMMIT))
        return out

    def _turn_messages(self, s):
        """The buffer transform (and viewport source) a tree surface needs for the current turn."""
        out = [msg(s.id, P.WL_SURFACE_SET_BUFFER_TRANSFORM, "i", self.turned_transform(s.req_transform))]
        if s.viewport and s.req_src:
            out.append(msg(s.viewport, P.WP_VIEWPORT_SET_SOURCE, "iiii", *self.turned_src(s.req_src)))
        return out

    def rotated(self):
        """Android turned its display (cfg.rotation): turn the picture and the window with it. A
        window the compositor sizes refits; a floating one keeps its zoom."""
        w = self.window
        if w and (w.fs_size or w.fill_size):
            self.zoom = self.fit_zoom(*(w.fs_size or w.fill_size))
        self.request_apply()

    def _own_viewport(self):
        """Android 11's HWC draws on its toplevel's surface and scales nothing: the zoom needs a
        viewport of ours there."""
        vpr = self._bind("wp_viewporter", 1)
        if vpr:
            self.window.own_vp = self.new_id("viewport")
            self.to_server(msg(vpr, P.WP_VIEWPORTER_GET_VIEWPORT, "no", self.window.own_vp, self.window.surface))
        return self.window.own_vp

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
        if w.own_vp:
            self.to_server(msg(w.own_vp, P.WP_VIEWPORT_DESTROY))
        if w.frame:
            for key in ("tip_sub", "title_sub", "toolbar_sub", "border_sub", "border_vp", "backdrop_sub",
                        "backdrop_vp", "grip_left_sub", "grip_left_vp", "grip_bottom_sub", "grip_bottom_vp"):
                oid = w.frame.get(key)
                if oid:
                    op = P.WL_SUBSURFACE_DESTROY if key.endswith("_sub") else P.WP_VIEWPORT_DESTROY
                    self.to_server(msg(oid, op))
            for key in ("tip", "title", "toolbar", "border", "backdrop") + GRIP_KINDS:
                oid = w.frame.get(key)
                if oid:
                    self.to_server(msg(oid, P.WL_SURFACE_DESTROY))
        self.window = None
        self.tip_due = self.tip_shown = None

    def _forget_surface(self, sid):
        s = self.surfaces.pop(sid, None)
        if s and s.parent in self.surfaces:
            try:
                self.surfaces[s.parent].children.remove(sid)
            except ValueError:
                pass

    def _forget(self, oid):
        iface = self.objs.pop(oid, None)
        copy = self.copies.pop(oid, None)
        if copy:
            self.cfg.gpu.free(copy[0])
            copy[3].close()
        if iface == "wl_surface":
            self._forget_surface(oid)
        self.viewports.pop(oid, None)
        self.subsurfaces.pop(oid, None)
        self.xdg_surfaces.pop(oid, None)
        self.toplevels.pop(oid, None)
        self.outputs.pop(oid, None)
        self.registries.discard(oid)

    def close(self):
        """The connection is gone: let go of the GPU's buffers."""
        for oid in list(self.copies):
            self._forget(oid)

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
        # Android 11 draws on the toplevel itself, which has nothing behind its picture: black there
        # when the window is bigger than the picture (maximized, fullscreen; a turned picture)
        f["backdrop"], f["backdrop_sub"] = self.new_id("surface_backdrop"), self.new_id("subsurface")
        self.to_server(msg(comp, P.WL_COMPOSITOR_CREATE_SURFACE, "n", f["backdrop"]))
        self.to_server(msg(subc, P.WL_SUBCOMPOSITOR_GET_SUBSURFACE, "noo", f["backdrop_sub"], f["backdrop"],
                           w.surface))
        self.to_server(msg(f["backdrop_sub"], P.WL_SUBSURFACE_PLACE_BELOW, "o", w.surface))
        self.to_server(msg(f["backdrop_sub"], P.WL_SUBSURFACE_SET_DESYNC))
        f["backdrop_vp"] = self.new_id("viewport")
        self.to_server(msg(vp, P.WP_VIEWPORTER_GET_VIEWPORT, "no", f["backdrop_vp"], f["backdrop"]))
        # Resize grips just inside the picture's left and bottom edges (the title bar and toolbar
        # grip the others), above it: invisible strips, like the border
        for kind in GRIP_KINDS:
            f[kind], f[kind + "_sub"] = self.new_id("surface_" + kind), self.new_id("subsurface")
            self.to_server(msg(comp, P.WL_COMPOSITOR_CREATE_SURFACE, "n", f[kind]))
            self.to_server(msg(subc, P.WL_SUBCOMPOSITOR_GET_SUBSURFACE, "noo", f[kind + "_sub"], f[kind], w.surface))
            self.to_server(msg(f[kind + "_sub"], P.WL_SUBSURFACE_SET_DESYNC))
            f[kind + "_vp"] = self.new_id("viewport")
            self.to_server(msg(vp, P.WP_VIEWPORTER_GET_VIEWPORT, "no", f[kind + "_vp"], f[kind]))
        # Tooltip: a child of the toolbar (whose commits we control: subsurface positions are
        # parent state), left of it over the picture, with an empty input region so clicks
        # and hover go through to whatever is below
        f["tip"], f["tip_sub"] = self.new_id("surface_tip"), self.new_id("subsurface")
        self.to_server(msg(comp, P.WL_COMPOSITOR_CREATE_SURFACE, "n", f["tip"]))
        self.to_server(msg(subc, P.WL_SUBCOMPOSITOR_GET_SUBSURFACE, "noo", f["tip_sub"], f["tip"], f["toolbar"]))
        self.to_server(msg(f["tip_sub"], P.WL_SUBSURFACE_SET_DESYNC))
        region = self.new_id("region")
        self.to_server(msg(comp, P.WL_COMPOSITOR_CREATE_REGION, "n", region))
        self.to_server(msg(f["tip"], P.WL_SURFACE_SET_INPUT_REGION, "o", region))
        self.to_server(msg(region, P.WL_REGION_DESTROY))
        f["sizes"] = {}
        w.frame = f
        self.surface_kinds = {f[k]: k for k in ("title", "toolbar", "border") + GRIP_KINDS}

    def _frame_messages(self):
        """Positions of our subsurfaces (parent state) and fresh buffers when sizes changed."""
        w = self.window
        if not w or not w.frame:
            return []
        f = w.frame
        out = []
        dw, dh = self.content_size()
        ox, oy = self.area_origin()
        backdrop = self.area() if w.own_vp and not w.fullscreen else None
        if f["sizes"].get("backdrop") != backdrop:
            f["sizes"]["backdrop"] = backdrop
            out.extend(self._backdrop_messages(backdrop))
        out.append(msg(f["backdrop_sub"], P.WL_SUBSURFACE_SET_POSITION, "ii", ox, oy))
        hidden = w.fullscreen or not self.cfg.frame
        b, t, m = fr.TOOLBAR_W, fr.TITLE_H, fr.BORDER
        if hidden:
            self._tip(None)
            for kind in ("title", "toolbar", "border") + GRIP_KINDS:
                if f["sizes"].get(kind) != "hidden":
                    out.append(msg(f[kind], P.WL_SURFACE_ATTACH, "oii", 0, 0, 0))
                    out.append(msg(f[kind], P.WL_SURFACE_COMMIT))
                    f["sizes"][kind] = "hidden"
            return out
        dw, dh = self.area() or (dw, dh)     # maximized/tiled: frame hugs the window, content centred
        out.append(msg(f["title_sub"], P.WL_SUBSURFACE_SET_POSITION, "ii", ox, oy - t))
        out.append(msg(f["toolbar_sub"], P.WL_SUBSURFACE_SET_POSITION, "ii", ox + dw, oy))
        out.append(msg(f["border_sub"], P.WL_SUBSURFACE_SET_POSITION, "ii", ox - m, oy - t - m))
        if f["sizes"].get("title") != (dw + b, t):
            f["sizes"]["title"] = (dw + b, t)
            self._draw("title")
        if f["sizes"].get("toolbar") != (b, dh):
            f["sizes"]["toolbar"] = (b, dh)
            self._draw("toolbar")
        if f["sizes"].get("border") != (dw + b + 2 * m, dh + t + 2 * m):
            f["sizes"]["border"] = (dw + b + 2 * m, dh + t + 2 * m)
            out.extend(self._clear_messages("border", *f["sizes"]["border"]))
        g = fr.GRIP
        for kind, pos, size in (("grip_left", (ox, oy), (g, dh)), ("grip_bottom", (ox, oy + dh - g), (dw, g))):
            if w.fill_size:                  # maximized/tiled: nothing to grip
                size = "hidden"
            else:
                out.append(msg(f[kind + "_sub"], P.WL_SUBSURFACE_SET_POSITION, "ii", *pos))
            if f["sizes"].get(kind) != size:
                f["sizes"][kind] = size
                out.extend(self._clear_messages(kind, *size) if size != "hidden" else
                           [msg(f[kind], P.WL_SURFACE_ATTACH, "oii", 0, 0, 0), msg(f[kind], P.WL_SURFACE_COMMIT)])
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
            self.tb_scroll = min(self.tb_scroll, fr.toolbar_scroll_max(size[1]))
            data, pw, ph, stride = fr.render_toolbar(size[1], scale, self.cfg.theme, hover, pressed,
                                                     fullscreen=w.fullscreen, scroll=self.tb_scroll)
        self._attach(f[kind], data, pw, ph, stride, scale)
        return []

    def _attach(self, sid, data, pw, ph, stride, scale):
        """Show pixels on one of our surfaces, through a fresh shm buffer."""
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
                  msg(sid, P.WL_SURFACE_SET_BUFFER_SCALE, "i", scale),
                  msg(sid, P.WL_SURFACE_ATTACH, "oii", buf, 0, 0),
                  msg(sid, P.WL_SURFACE_DAMAGE, "iiii", 0, 0, FULL_DAMAGE, FULL_DAMAGE),
                  msg(sid, P.WL_SURFACE_COMMIT)):
            self.to_server(m)

    # -- toolbar tooltips ---------------------------------------------------------------
    TIP_DELAY = 0.5

    def _tip(self, action):
        """Hovered toolbar button changed: tooltip after a short delay, or at once when one is
        already up (moving along the toolbar), like GTK."""
        if action == self.tip_shown and self.tip_due is None:
            return
        showing = self.tip_shown
        if showing:
            self._hide_tip()
        self.tip_due = None
        if action in fr.TOOLTIPS:
            if showing:
                self._show_tip(action)
            else:
                self.tip_due = (time.monotonic() + self.TIP_DELAY, action)

    def tick(self):
        """From the main loop: show a tooltip whose delay has passed. True if messages were queued."""
        if not self.tip_due or time.monotonic() < self.tip_due[0]:
            return False
        action, self.tip_due = self.tip_due[1], None
        return self._show_tip(action)

    def _show_tip(self, action):
        f = self.window.frame if self.window else None
        size = f and f["sizes"].get("toolbar")
        span = [(y0, y1) for a, y0, y1 in fr.toolbar_layout(size[1], self.tb_scroll)
                if a == action] if size and size != "hidden" else []
        if not span:
            return False
        text = fr.TOOLTIPS[action]
        w, h = fr.tooltip_size(text)
        scale = self._out_scale()
        y = int((sum(span[0]) - h) // 2)          # wheel scrolling leaves the toolbar at fractions of a pixel
        self.to_server(msg(f["tip_sub"], P.WL_SUBSURFACE_SET_POSITION, "ii", -w - 6, y))
        self.to_server(msg(f["toolbar"], P.WL_SURFACE_COMMIT))     # applies the position
        self._attach(f["tip"], *fr.render_tooltip(text, scale), scale)
        self.tip_shown = action
        return True

    def _hide_tip(self):
        f = self.window.frame if self.window else None
        if f:
            self.to_server(msg(f["tip"], P.WL_SURFACE_ATTACH, "oii", 0, 0, 0))
            self.to_server(msg(f["tip"], P.WL_SURFACE_COMMIT))
        self.tip_shown = None

    def _clear_messages(self, kind, bw, bh):
        """An invisible bw x bh surface of ours that takes input (the border, the grips)."""
        f = self.window.frame
        out = []
        pixel = self.my_globals.get("wp_single_pixel_buffer_manager_v1")
        if pixel:
            buf = self.new_id("buffer_pixel")
            out.append(msg(pixel, P.WP_SINGLE_PIXEL_CREATE_U32_RGBA, "nuuuu", buf, 0, 0, 0, 0))
            out.append(msg(f[kind], P.WL_SURFACE_ATTACH, "oii", buf, 0, 0))
        out.append(msg(f[kind + "_vp"], P.WP_VIEWPORT_SET_DESTINATION, "ii", bw, bh))
        out.append(msg(f[kind], P.WL_SURFACE_DAMAGE, "iiii", 0, 0, FULL_DAMAGE, FULL_DAMAGE))
        out.append(msg(f[kind], P.WL_SURFACE_COMMIT))
        return out

    def _backdrop_messages(self, size):
        f = self.window.frame
        pixel = self.my_globals.get("wp_single_pixel_buffer_manager_v1")
        if not size or not pixel:
            return [msg(f["backdrop"], P.WL_SURFACE_ATTACH, "oii", 0, 0, 0), msg(f["backdrop"], P.WL_SURFACE_COMMIT)]
        buf = self.new_id("buffer_pixel")
        return [msg(pixel, P.WP_SINGLE_PIXEL_CREATE_U32_RGBA, "nuuuu", buf, 0, 0, 0, 0xffffffff),
                msg(f["backdrop"], P.WL_SURFACE_ATTACH, "oii", buf, 0, 0),
                msg(f["backdrop_vp"], P.WP_VIEWPORT_SET_DESTINATION, "ii", *size),
                msg(f["backdrop"], P.WL_SURFACE_DAMAGE, "iiii", 0, 0, FULL_DAMAGE, FULL_DAMAGE),
                msg(f["backdrop"], P.WL_SURFACE_COMMIT)]

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
        floating = not (self.window.fill_size or self.window.fullscreen)
        if kind == "title":
            return (floating and fr.grip_edge(x, y, *size, ("top", "left", "right"))) or fr.hit_title(x, y, size[0])
        if kind == "toolbar":
            return (floating and fr.grip_edge(x, y, *size, ("right", "bottom"))) or \
                fr.hit_toolbar(x, y, size[1], self.tb_scroll)
        if kind in GRIP_KINDS:
            return fr.grip_edge(x, y, *size, ("left", "bottom"))
        return fr.border_edge(x, y, size[0], size[1])

    def _frame_hover(self, sid, x, y, entered=False):
        kind = getattr(self, "surface_kinds", {}).get(sid)
        if not kind:
            return
        action = self._hit(kind, x, y)
        cursor = P.EDGE_CURSOR.get(action, "default")
        if entered or cursor != self.cursor_shown:
            self.cursor_shown = cursor
            self._set_cursor(cursor)
        if kind not in ("title", "toolbar"):
            self.hover[kind] = action
            return
        if action == "move" or action in P.RESIZE_EDGE:
            action = None
        if self.hover.get(kind) != action:
            self.hover[kind] = action
            self._redraw(kind)
            if kind == "toolbar":
                self._tip(action)

    def _scroll_toolbar(self, sid, dy):
        f = self.window.frame if self.window else None
        size = f and f["sizes"].get("toolbar")
        if getattr(self, "surface_kinds", {}).get(sid) != "toolbar" or not size or size == "hidden":
            return
        scroll = max(0, min(fr.toolbar_scroll_max(size[1]), self.tb_scroll + dy))
        if scroll != self.tb_scroll:
            self.tb_scroll = scroll
            self._tip(None)
            self.hover["toolbar"] = self._hit("toolbar", *self.ptr_pos)
            self._redraw("toolbar")
            self._tip(self.hover["toolbar"])

    def _frame_leave(self, sid):
        kind = getattr(self, "surface_kinds", {}).get(sid)
        if kind == "toolbar":
            self._tip(None)
        if kind and self.hover.get(kind):
            self.hover[kind] = None
            if kind in ("title", "toolbar"):
                self._redraw(kind)
        if self.pressed and self.pressed[0] == kind:
            self.pressed = None

    def _frame_button(self, sid, x, y, pressed, serial):
        kind = getattr(self, "surface_kinds", {}).get(sid)
        if not kind or not self.window:
            return
        action = self._hit(kind, x, y)
        w = self.window
        if kind not in ("title", "toolbar") or action in P.RESIZE_EDGE:
            if pressed and action and self.seat:
                # The first configure may only round the undragged side. Our grip knows the axis.
                w.resize_axis = {"left": 0, "right": 0, "top": 1, "bottom": 1}.get(action)
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
        if pressed:
            self._tip(None)
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
        elif action in ("screenshot", "install", "settings", "restart"):
            self.emit("action " + action)
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

class PipeReader:
    """Reads a pipe to EOF from the main loop, then calls done(data) and flushes the connection."""

    def __init__(self, conn, fd, done):
        self.conn, self.fd, self.done, self.data = conn, fd, done, b""
        os.set_blocking(fd, False)
        conn.proxy.sel.register(fd, selectors.EVENT_READ, self)

    def on_event(self, _fileobj, _mask):
        try:
            b = os.read(self.fd, 65536)
        except BlockingIOError:
            return
        except OSError:
            b = b""
        self.data += b
        if b and len(self.data) <= CLIP_MAX:
            return
        self.conn.proxy.sel.unregister(self.fd)
        os.close(self.fd)
        if not self.conn.closed:
            self.done(self.data)
            self.conn.pump()


class Connection:
    def __init__(self, proxy, client, upstream):
        self.proxy = proxy
        self.client = client
        self.upstream = upstream
        self.session = Session(proxy.cfg, proxy.emit)
        self.tr = Translator(wlschema.load(), lambda m: sys.stderr.write("wlproxy: " + m + "\n"))
        self.c2s = Stream(self.session.on_request, self.session.post_feed_c2s, translate=self.tr.request,
                          count_fds=self.tr.request_fds)
        self.s2c = Stream(self._event)
        self.session.c2s, self.session.s2c = self.c2s, self.s2c
        self.session.watch = lambda fd, done: PipeReader(self, fd, done)
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
        self.session.close()
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
                 close_action="stop", cpu_buffers=None):
        self.id = inst_id
        self.name = name
        self.width = int(width or 0)
        self.height = int(height or 0)
        self.zoom = zoom
        self.frame = frame
        self.theme = theme
        self.close_action = close_action
        self.rotation = 0          # Android's display rotation (Surface.ROTATION_*), from stdin
        # Android 11's mouse isn't orientation-aware: it takes positions in the turned frame
        self.logical_pointer = False
        # the compositor can't import Android's dmabufs: show them as shm ("shared", or the render
        # node of the GPU to copy them through; Session._dmabuf_request)
        self.cpu_buffers = cpu_buffers
        self.gpu = gbm.Device(cpu_buffers) if cpu_buffers not in (None, "shared") else None


class Proxy:
    def __init__(self, listen, upstream, cfg, out=sys.stdout):
        self.listen_path = listen
        self.upstream_path = upstream
        self.cfg = cfg
        self.out = out
        self.sel = selectors.DefaultSelector()
        self.server = None
        self.conns = []

    def watch_rotation(self, fd):
        """Android's display rotation arrives on fd, a digit per line (session/main.py)."""
        os.set_blocking(fd, False)
        self.rot_fd, self.rot_buf = fd, b""
        self.sel.register(fd, selectors.EVENT_READ, self)

    def on_event(self, _fileobj, _mask):
        try:
            data = os.read(self.rot_fd, 4096)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if not data:
            self.sel.unregister(self.rot_fd)
            return
        *lines, self.rot_buf = (self.rot_buf + data).split(b"\n")
        last = lines[-1].strip() if lines else b""
        if last not in (b"0", b"1", b"2", b"3") or int(last) == self.cfg.rotation:
            return
        self.cfg.rotation = int(last)
        for c in self.conns:
            try:
                c.session.rotated()
            except Exception:  # noqa: BLE001
                log_error("error turning the window")
            c.pump()

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
            lines.append("connection {}: zoom={} res={} rotation={} window={} fullscreen={} fill={} frame={} "
                         "geometry={}".format(i, se.zoom, se.res, self.cfg.rotation, bool(w), w and w.fullscreen,
                                              w and w.fill_size, bool(w and w.frame), w and se.geometry()))
            lines.append("  pings={pings} pongs={pongs} last_latency={last_latency:.3f}s "
                         "max_latency={max_latency:.3f}s outstanding={n}".format(n=len(se.pings), **se.ping_stats))
            lines.append("  clipboard answered={answered} late={late}".format(**se.clip_stats))
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
            due = [c.session.tip_due[0] for c in self.conns if c.session.tip_due]
            for key, mask in self.sel.select(max(0.0, min(due) - time.monotonic()) if due else None):
                if key.data is None:
                    self.accept()
                else:
                    key.data.on_event(key.fileobj, mask)
            for c in list(self.conns):
                if c.session.tick():
                    c.pump()


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--listen", required=True)
    p.add_argument("--upstream", required=True)
    p.add_argument("--id", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--width", type=int, default=0)
    p.add_argument("--height", type=int, default=0)
    p.add_argument("--zoom", default="auto")
    p.add_argument("--theme", default="dark", choices=("dark", "light"))
    p.add_argument("--close-action", default="stop", choices=("stop", "freeze", "none"))
    p.add_argument("--cpu-buffers", help='"shared" or a render node (Config)')
    p.add_argument("--logical-pointer", action="store_true", help="Android 11: pointer positions stay turned")
    o = p.parse_args(argv)
    cfg = Config(o.id, o.name, o.width, o.height, o.zoom, True, o.theme, o.close_action, o.cpu_buffers)
    cfg.logical_pointer = o.logical_pointer
    global LOG_PATH
    cache = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    os.makedirs(os.path.join(cache, "waydroid-manager"), exist_ok=True)
    LOG_PATH = os.path.join(cache, "waydroid-manager", "wlproxy-{}.log".format(o.id))

    def on_term(*_):
        sys.exit(0)
    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGPIPE, signal.SIG_IGN)
    proxy = Proxy(o.listen, o.upstream, cfg)
    proxy.watch_rotation(sys.stdin.fileno())
    signal.signal(signal.SIGUSR1, lambda *_: proxy.dump_state())   # waydroid-manager log <id> --window
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
