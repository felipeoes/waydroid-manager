# SPDX-License-Identifier: GPL-3.0-or-later
"""Wayland proxy that labels an instance's windows.

The Waydroid hwcomposer hardcodes the full-UI window's xdg app_id and title to
"Waydroid", so every instance would look identical and group under the stock
Waydroid launcher. This proxy sits between the container and the real
compositor and rewrites only ``xdg_toplevel.set_title`` / ``set_app_id``:

    "Waydroid"          -> title "Waydroid · <name>", app_id "waydroid-multi.<id>"
    "waydroid.<pkg>"    -> app_id "waydroid-multi.<id>.<pkg>"

It also reports ``xdg_toplevel.close`` on the full-UI window (a user clicking
the window's close button) so the session can apply the instance's
close_action.

Everything else is forwarded untouched. Messages are framed by their 8-byte
header; file descriptors travel in a FIFO next to the byte stream and are
always delivered no later than the message that uses them.

Run as a separate process (no gbinder here):
  python3 -m waydroid_multi.session.wlproxy --listen SOCK --upstream SOCK --id ID --name NAME
Events are written to stdout, one per line ("ready", "close").
"""
import argparse
import array
import errno
import os
import selectors
import signal
import socket
import struct
import sys

HEADER = struct.Struct("=II")
MAX_FDS_PER_MSG = 28        # libwayland's MAX_FDS_OUT; receivers truncate beyond this
MAX_MSG = 4096              # libwayland's maximum message size
RECV_SIZE = 65536
HIGH_WATER = 4 * 1024 * 1024
FULL_UI_APP_ID = "Waydroid"

# Opcodes (requests are client->server, events server->client)
WL_DISPLAY_GET_REGISTRY = 1
WL_DISPLAY_EV_DELETE_ID = 1
WL_REGISTRY_BIND = 0
XDG_WM_BASE_DESTROY = 0
XDG_WM_BASE_GET_XDG_SURFACE = 2
XDG_SURFACE_DESTROY = 0
XDG_SURFACE_GET_TOPLEVEL = 1
XDG_TOPLEVEL_DESTROY = 0
XDG_TOPLEVEL_SET_TITLE = 2
XDG_TOPLEVEL_SET_APP_ID = 3
XDG_TOPLEVEL_EV_CLOSE = 1


class ProtocolError(Exception):
    pass


def read_string(payload, off):
    """Return (str or None, new offset) for a wire string at payload[off:]."""
    if off + 4 > len(payload):
        raise ProtocolError("truncated string length")
    (n,) = struct.unpack_from("=I", payload, off)
    off += 4
    if n == 0:
        return None, off
    end = off + ((n + 3) & ~3)
    if end > len(payload) or n > len(payload):
        raise ProtocolError("truncated string")
    raw = bytes(payload[off:off + n])
    if raw[-1:] != b"\0":
        raise ProtocolError("string not NUL-terminated")
    return raw[:-1].decode("utf-8", "replace"), end


def encode_string(s):
    raw = s.encode("utf-8") + b"\0"
    pad = (-len(raw)) % 4
    return struct.pack("=I", len(raw)) + raw + b"\0" * pad


def build_message(obj, opcode, payload):
    size = HEADER.size + len(payload)
    if size > MAX_MSG:
        raise ProtocolError("message too large")
    return HEADER.pack(obj, (size << 16) | opcode) + payload


class Labeler:
    """Per-connection protocol state: which object ids are xdg objects."""

    def __init__(self, inst_id, name, on_event=None):
        self.inst_id = inst_id
        self.name = name
        self.on_event = on_event or (lambda ev: None)
        self.registries = set()
        self.wm_bases = set()
        self.xdg_surfaces = set()
        self.toplevels = {}   # id -> original app_id (or None)

    # -- rewriting rules ----------------------------------------------------
    def title_for(self, title):
        if not title or title == FULL_UI_APP_ID:
            return "Waydroid · {}".format(self.name)
        return "{} · {}".format(title, self.name)

    def app_id_for(self, app_id):
        base = "waydroid-multi.{}".format(self.inst_id)
        if not app_id or app_id == FULL_UI_APP_ID:
            return base
        if app_id.startswith("waydroid."):
            return base + "." + app_id[len("waydroid."):]
        return base + "." + app_id

    # -- client -> server ---------------------------------------------------
    def client_message(self, obj, opcode, payload):
        """Return replacement bytes for the whole message, or None to keep it."""
        if obj == 1 and opcode == WL_DISPLAY_GET_REGISTRY:
            self.registries.add(self._u32(payload, 0))
        elif obj in self.registries and opcode == WL_REGISTRY_BIND:
            off = 4
            iface, off = read_string(payload, off)
            new_id = self._u32(payload, off + 4)
            if iface == "xdg_wm_base":
                self.wm_bases.add(new_id)
        elif obj in self.wm_bases:
            if opcode == XDG_WM_BASE_GET_XDG_SURFACE:
                self.xdg_surfaces.add(self._u32(payload, 0))
            elif opcode == XDG_WM_BASE_DESTROY:
                self.wm_bases.discard(obj)
        elif obj in self.xdg_surfaces:
            if opcode == XDG_SURFACE_GET_TOPLEVEL:
                self.toplevels[self._u32(payload, 0)] = None
            elif opcode == XDG_SURFACE_DESTROY:
                self.xdg_surfaces.discard(obj)
        elif obj in self.toplevels:
            if opcode == XDG_TOPLEVEL_SET_TITLE:
                title, _ = read_string(payload, 0)
                return build_message(obj, opcode, encode_string(self.title_for(title)))
            if opcode == XDG_TOPLEVEL_SET_APP_ID:
                app_id, _ = read_string(payload, 0)
                self.toplevels[obj] = app_id
                return build_message(obj, opcode, encode_string(self.app_id_for(app_id)))
            if opcode == XDG_TOPLEVEL_DESTROY:
                del self.toplevels[obj]
        return None

    # -- server -> client ---------------------------------------------------
    def server_message(self, obj, opcode, payload):
        if obj in self.toplevels and opcode == XDG_TOPLEVEL_EV_CLOSE:
            if self.toplevels[obj] in (None, FULL_UI_APP_ID):
                self.on_event("close")
        elif obj == 1 and opcode == WL_DISPLAY_EV_DELETE_ID and len(payload) >= 4:
            dead = self._u32(payload, 0)
            self.registries.discard(dead)
            self.wm_bases.discard(dead)
            self.xdg_surfaces.discard(dead)
            self.toplevels.pop(dead, None)

    @staticmethod
    def _u32(payload, off):
        if off + 4 > len(payload):
            raise ProtocolError("truncated argument")
        return struct.unpack_from("=I", payload, off)[0]


class Stream:
    """One direction of a proxied connection: frames, filters and queues output."""

    def __init__(self, handler):
        self.handler = handler   # (obj, opcode, payload) -> bytes | None
        self.inbuf = bytearray()
        self.out = bytearray()
        self.fds = []            # fds waiting to be forwarded (FIFO)

    def feed(self, data, fds):
        self.fds.extend(fds)
        self.inbuf += data
        buf = self.inbuf
        pos = 0
        while len(buf) - pos >= HEADER.size:
            obj, word = HEADER.unpack_from(buf, pos)
            size = word >> 16
            if size < HEADER.size or size % 4:
                raise ProtocolError("bad message size {}".format(size))
            if len(buf) - pos < size:
                break
            payload = memoryview(buf)[pos + HEADER.size:pos + size]
            try:
                repl = self.handler(obj, word & 0xffff, payload)
            finally:
                payload.release()
            if repl is None:
                self.out += buf[pos:pos + size]
            else:
                self.out += repl
            pos += size
        if pos:
            del self.inbuf[:pos]

    def pending(self):
        # fds alone cannot be sent: they ride on the bytes that follow them
        return bool(self.out)


def recv_with_fds(sock):
    fds = array.array("i")
    msg, ancdata, flags, _ = sock.recvmsg(RECV_SIZE, socket.CMSG_SPACE(253 * fds.itemsize))
    for level, ctype, data in ancdata:
        if level == socket.SOL_SOCKET and ctype == socket.SCM_RIGHTS:
            usable = len(data) - (len(data) % fds.itemsize)
            fds.frombytes(data[:usable])
    if flags & socket.MSG_CTRUNC:
        for fd in fds:
            os.close(fd)
        raise ProtocolError("file descriptors truncated")
    return msg, list(fds)


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


class Connection:
    def __init__(self, proxy, client, upstream):
        self.proxy = proxy
        self.client = client
        self.upstream = upstream
        self.labeler = Labeler(proxy.inst_id, proxy.name, proxy.emit)
        self.c2s = Stream(self.labeler.client_message)
        self.s2c = Stream(self._server_message)
        self.closed = False

    def _server_message(self, obj, opcode, payload):
        self.labeler.server_message(obj, opcode, payload)
        return None

    def sockets(self):
        return (self.client, self.upstream)

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

    def update_interest(self):
        if self.closed:
            return
        # Read from a side only while the opposite queue is below the high-water mark
        for sock, inq, outq in ((self.client, self.c2s, self.s2c), (self.upstream, self.s2c, self.c2s)):
            ev = 0
            if len(inq.out) < HIGH_WATER:
                ev |= selectors.EVENT_READ
            if outq.pending():
                ev |= selectors.EVENT_WRITE
            self.proxy.sel.modify(sock, ev or selectors.EVENT_READ, self)

    def on_event(self, sock, mask):
        src_is_client = sock is self.client
        if mask & selectors.EVENT_READ:
            stream = self.c2s if src_is_client else self.s2c
            try:
                data, fds = recv_with_fds(sock)
            except (BlockingIOError, InterruptedError):
                data, fds = None, []
            except (OSError, ProtocolError):
                self.close()
                return
            if data is not None:
                if not data:
                    # Peer closed: best-effort delivery of what is queued (e.g. a
                    # wl_display.error), then close both sides
                    other = self.upstream if src_is_client else self.client
                    try:
                        flush(other, stream)
                    except OSError:
                        pass
                    self.close()
                    return
                try:
                    stream.feed(data, fds)
                except ProtocolError:
                    self.close()
                    return
        try:
            flush(self.upstream, self.c2s)
            flush(self.client, self.s2c)
        except OSError:
            self.close()
            return
        self.update_interest()


class Proxy:
    def __init__(self, listen, upstream, inst_id, name, out=sys.stdout):
        self.listen_path = listen
        self.upstream_path = upstream
        self.inst_id = inst_id
        self.name = name
        self.out = out
        self.sel = selectors.DefaultSelector()
        self.server = None

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
        self.sel.register(client, selectors.EVENT_READ, conn)
        self.sel.register(up, selectors.EVENT_READ, conn)

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
    o = p.parse_args(argv)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    signal.signal(signal.SIGPIPE, signal.SIG_IGN)
    try:
        Proxy(o.listen, o.upstream, o.id, o.name).run()
    except KeyboardInterrupt:
        pass
    except OSError as e:
        if e.errno != errno.EINTR:
            raise


if __name__ == "__main__":
    main()
