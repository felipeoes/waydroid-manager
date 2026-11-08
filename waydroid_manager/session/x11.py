# SPDX-License-Identifier: GPL-3.0-or-later
"""The X11 window of a device's Weston (an X11 desktop, session.start_nested), through libX11:
finding it (by its client's pid, through libXRes), naming it after the device, and fullscreen,
which kiosk Weston can't do itself."""
import ctypes
import ctypes.util

_P, _UL, _L = ctypes.c_void_p, ctypes.c_ulong, ctypes.c_long
WESTON_CLASS = b"Weston Compositor"      # WM_CLASS class of each Weston output window
CLIENT_MESSAGE, PROP_MODE_REPLACE = 33, 0
SUBSTRUCTURE_MASK = (1 << 19) | (1 << 20)     # SubstructureNotify | SubstructureRedirect
NET_WM_STATE_TOGGLE = 2
XRES_CLIENT_ID_PID_MASK = 1 << 1


class _ClientMessage(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int), ("serial", _UL), ("send_event", ctypes.c_int), ("display", _P),
                ("window", _UL), ("message_type", _UL), ("format", ctypes.c_int), ("data", _L * 5)]


class _ClientIdSpec(ctypes.Structure):
    _fields_ = [("client", _UL), ("mask", ctypes.c_uint)]


class _ClientIdValue(ctypes.Structure):
    _fields_ = [("spec", _ClientIdSpec), ("length", _L), ("value", _P)]


class _Event(ctypes.Union):
    _fields_ = [("xclient", _ClientMessage), ("pad", _L * 24)]


class Display:
    def __init__(self):
        x = self.x = ctypes.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
        x.XOpenDisplay.argtypes, x.XOpenDisplay.restype = [ctypes.c_char_p], _P
        x.XDefaultRootWindow.argtypes, x.XDefaultRootWindow.restype = [_P], _UL
        x.XInternAtom.argtypes, x.XInternAtom.restype = [_P, ctypes.c_char_p, ctypes.c_int], _UL
        x.XGetWindowProperty.argtypes = [_P, _UL, _UL, _L, _L, ctypes.c_int, _UL, ctypes.POINTER(_UL),
                                         ctypes.POINTER(ctypes.c_int), ctypes.POINTER(_UL),
                                         ctypes.POINTER(_UL), ctypes.POINTER(_P)]
        x.XChangeProperty.argtypes = [_P, _UL, _UL, _UL, ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
        x.XSendEvent.argtypes = [_P, _UL, ctypes.c_int, _L, ctypes.POINTER(_Event)]
        x.XFree.argtypes, x.XFlush.argtypes, x.XCloseDisplay.argtypes = [_P], [_P], [_P]
        self.dpy = x.XOpenDisplay(None)
        if not self.dpy:
            raise OSError("can't open the X display")
        self.root = x.XDefaultRootWindow(self.dpy)
        try:
            r = self.xres = ctypes.CDLL(ctypes.util.find_library("XRes") or "libXRes.so.1")
            r.XResQueryClientIds.argtypes = [_P, _L, ctypes.POINTER(_ClientIdSpec), ctypes.POINTER(_L),
                                             ctypes.POINTER(ctypes.POINTER(_ClientIdValue))]
            r.XResGetClientPid.argtypes, r.XResGetClientPid.restype = [ctypes.POINTER(_ClientIdValue)], ctypes.c_int
            r.XResClientIdsDestroy.argtypes = [_L, ctypes.POINTER(_ClientIdValue)]
        except OSError:
            self.xres = None

    def close(self):
        self.x.XCloseDisplay(self.dpy)

    def atom(self, name):
        return self.x.XInternAtom(self.dpy, name.encode(), 0)

    def _prop(self, win, name, length=4096):
        """(format, raw bytes) of a window property, or (0, b"")."""
        typ, fmt, n, after, data = _UL(), ctypes.c_int(), _UL(), _UL(), _P()
        if self.x.XGetWindowProperty(self.dpy, win, self.atom(name), 0, length, 0, 0, ctypes.byref(typ),
                                     ctypes.byref(fmt), ctypes.byref(n), ctypes.byref(after),
                                     ctypes.byref(data)) != 0 or not data:
            return 0, b""
        try:
            # 32-bit items come as C longs
            size = n.value * (ctypes.sizeof(_L) if fmt.value == 32 else fmt.value // 8)
            return fmt.value, ctypes.string_at(data, size)
        finally:
            self.x.XFree(data)

    def weston_windows(self):
        """The managed windows of every Weston on this display."""
        fmt, raw = self._prop(self.root, "_NET_CLIENT_LIST")
        wins = list((_UL * (len(raw) // ctypes.sizeof(_UL))).from_buffer_copy(raw)) if fmt == 32 else []
        return {w for w in wins if WESTON_CLASS in self._prop(w, "WM_CLASS")[1].split(b"\0")}

    def window_pid(self, win):
        """The pid of the client that made win, or None (no X-Resource, or a remote client)."""
        if self.xres is None:
            return None
        spec, n, ids = _ClientIdSpec(win, XRES_CLIENT_ID_PID_MASK), _L(), ctypes.POINTER(_ClientIdValue)()
        if self.xres.XResQueryClientIds(self.dpy, 1, ctypes.byref(spec), ctypes.byref(n), ctypes.byref(ids)) != 0:
            return None
        try:
            pids = [self.xres.XResGetClientPid(ids[i]) for i in range(n.value)]
        finally:
            self.xres.XResClientIdsDestroy(n, ids)
        return next((p for p in pids if p > 0), None)

    def set_title(self, win, title):
        data = title.encode()
        for name in ("_NET_WM_NAME", "_NET_WM_ICON_NAME"):
            self.x.XChangeProperty(self.dpy, win, self.atom(name), self.atom("UTF8_STRING"), 8,
                                   PROP_MODE_REPLACE, data, len(data))
        self.x.XFlush(self.dpy)

    def is_fullscreen(self, win):
        fmt, raw = self._prop(win, "_NET_WM_STATE")
        states = (_UL * (len(raw) // ctypes.sizeof(_UL))).from_buffer_copy(raw) if fmt == 32 else ()
        return self.atom("_NET_WM_STATE_FULLSCREEN") in list(states)

    def toggle_fullscreen(self, win):
        ev = _Event()
        ev.xclient = _ClientMessage(CLIENT_MESSAGE, 0, 1, self.dpy, win, self.atom("_NET_WM_STATE"), 32,
                                    (_L * 5)(NET_WM_STATE_TOGGLE, self.atom("_NET_WM_STATE_FULLSCREEN"), 0, 1, 0))
        self.x.XSendEvent(self.dpy, self.root, 0, SUBSTRUCTURE_MASK, ctypes.byref(ev))
        self.x.XFlush(self.dpy)
