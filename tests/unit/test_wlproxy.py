# SPDX-License-Identifier: GPL-3.0-or-later
import os
import socket
import struct
import threading
import unittest

from waydroid_multi.session import wlproxy as wp


def msg(obj, opcode, payload=b""):
    return struct.pack("=II", obj, ((8 + len(payload)) << 16) | opcode) + payload


def u32(*vals):
    return b"".join(struct.pack("=I", v) for v in vals)


def string(s):
    return wp.encode_string(s)


def parse(stream_bytes):
    out = []
    pos = 0
    while pos < len(stream_bytes):
        obj, word = struct.unpack_from("=II", stream_bytes, pos)
        size = word >> 16
        out.append((obj, word & 0xffff, stream_bytes[pos + 8:pos + size]))
        pos += size
    return out


def setup_toplevel(lab):
    """Client creates registry(2) -> binds xdg_wm_base(3) -> xdg_surface(5) -> toplevel(6)."""
    s = wp.Stream(lab.client_message)
    s.feed(msg(1, 1, u32(2)), [])                                      # get_registry
    s.feed(msg(2, 0, u32(7) + string("xdg_wm_base") + u32(5, 3)), [])   # bind name=7 v5 id=3
    s.feed(msg(3, 2, u32(5, 4)), [])                                    # get_xdg_surface(5, wl_surface 4)
    s.feed(msg(5, 1, u32(6)), [])                                       # get_toplevel(6)
    return s


class LabelerTest(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.lab = wp.Labeler("t1", "Game One", self.events.append)

    def test_rewrites_full_ui_title_and_app_id(self):
        s = setup_toplevel(self.lab)
        s.out.clear()
        s.feed(msg(6, 2, string("Waydroid")) + msg(6, 3, string("Waydroid")), [])
        (o1, op1, p1), (o2, op2, p2) = parse(bytes(s.out))
        self.assertEqual((o1, op1), (6, 2))
        self.assertEqual(wp.read_string(p1, 0)[0], "Waydroid · Game One")
        self.assertEqual(wp.read_string(p2, 0)[0], "waydroid-multi.t1")

    def test_rewrites_app_window(self):
        s = setup_toplevel(self.lab)
        s.out.clear()
        s.feed(msg(6, 3, string("waydroid.com.example.game")), [])
        (_, _, p), = parse(bytes(s.out))
        self.assertEqual(wp.read_string(p, 0)[0], "waydroid-multi.t1.com.example.game")

    def test_rewritten_message_is_padded(self):
        s = setup_toplevel(self.lab)
        s.out.clear()
        s.feed(msg(6, 2, string("x")), [])
        self.assertEqual(len(s.out) % 4, 0)
        obj, word = struct.unpack_from("=II", bytes(s.out))
        self.assertEqual(word >> 16, len(s.out))

    def test_untracked_objects_pass_through(self):
        s = setup_toplevel(self.lab)
        s.out.clear()
        raw = msg(9, 2, string("Waydroid"))  # object 9 is not a toplevel
        s.feed(raw, [])
        self.assertEqual(bytes(s.out), raw)

    def test_split_messages_across_reads(self):
        s = setup_toplevel(self.lab)
        s.out.clear()
        raw = msg(6, 2, string("Waydroid"))
        for i in range(len(raw)):
            s.feed(raw[i:i + 1], [])
        (_, _, p), = parse(bytes(s.out))
        self.assertEqual(wp.read_string(p, 0)[0], "Waydroid · Game One")

    def test_destroy_forgets_toplevel(self):
        s = setup_toplevel(self.lab)
        s.feed(msg(6, 0), [])                 # xdg_toplevel.destroy
        s.out.clear()
        raw = msg(6, 2, string("Waydroid"))   # id 6 reused by something else
        s.feed(raw, [])
        self.assertEqual(bytes(s.out), raw)

    def test_close_event_reported_for_full_ui_only(self):
        s = setup_toplevel(self.lab)
        s.feed(msg(6, 3, string("Waydroid")), [])
        self.lab.server_message(6, 1, memoryview(b""))
        self.assertEqual(self.events, ["close"])
        s.feed(msg(5, 1, u32(8)) + msg(8, 3, string("waydroid.com.app")), [])
        self.lab.server_message(8, 1, memoryview(b""))
        self.assertEqual(self.events, ["close"])

    def test_bad_size_raises(self):
        s = wp.Stream(self.lab.client_message)
        with self.assertRaises(wp.ProtocolError):
            s.feed(struct.pack("=II", 1, (6 << 16) | 0) + b"\0\0", [])

    def test_fds_are_queued_in_order(self):
        s = wp.Stream(self.lab.client_message)
        s.feed(msg(10, 0, u32(1))[:5], [11, 12])
        s.feed(msg(10, 0, u32(1))[5:], [13])
        self.assertEqual(s.fds, [11, 12, 13])
        self.assertEqual(len(parse(bytes(s.out))), 1)


class ProxyIntegrationTest(unittest.TestCase):
    """Run the real proxy between a fake client and a fake compositor."""

    def test_end_to_end_rewrite_and_fd_passing(self):
        import tempfile
        tmp = tempfile.mkdtemp()
        upstream_path = os.path.join(tmp, "compositor")
        listen_path = os.path.join(tmp, "sub", "wayland-0")
        comp = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        comp.bind(upstream_path)
        comp.listen(1)

        class Out:
            def __init__(self):
                self.lines = []

            def write(self, s):
                self.lines.append(s)

            def flush(self):
                pass

        out = Out()
        proxy = wp.Proxy(listen_path, upstream_path, "t9", "Nine", out)
        proxy.bind()
        threading.Thread(target=proxy.run, daemon=True).start()

        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(listen_path)
        server, _ = comp.accept()
        r, w = os.pipe()
        data = (msg(1, 1, u32(2)) + msg(2, 0, u32(7) + string("xdg_wm_base") + u32(5, 3)) +
                msg(3, 2, u32(5, 4)) + msg(5, 1, u32(6)) + msg(6, 2, string("Waydroid")))
        import array
        client.sendmsg([data], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [r]))])
        got = b""
        fds = []
        server.settimeout(5)
        while len(got) < len(data):
            chunk, anc, _, _ = server.recvmsg(65536, socket.CMSG_SPACE(64))
            got += chunk
            for level, ctype, payload in anc:
                fds.extend(array.array("i", payload[:len(payload) - len(payload) % 4]))
        last = parse(got)[-1]
        self.assertEqual(wp.read_string(last[2], 0)[0], "Waydroid · Nine")
        self.assertEqual(len(fds), 1)
        os.write(w, b"ok")
        self.assertEqual(os.read(fds[0], 2), b"ok")
        # compositor asks the full-UI window to close -> proxy reports it
        server.sendall(msg(6, 1))
        client.settimeout(5)
        self.assertEqual(client.recv(64), msg(6, 1))
        for _ in range(50):
            if "close\n" in out.lines:
                break
            threading.Event().wait(0.05)
        self.assertIn("close\n", out.lines)
        for fd in fds + [r, w]:
            os.close(fd)
        client.close()
        server.close()
        comp.close()


if __name__ == "__main__":
    unittest.main()
