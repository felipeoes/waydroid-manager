# SPDX-License-Identifier: GPL-3.0-or-later
import array
import os
import socket
import struct
import tempfile
import threading
import time
import unittest

from waydroid_manager.session import wlproto as P
from waydroid_manager.session import wlproxy as wp
from waydroid_manager.session.wlproto import Reader, msg, parse

# HWC-side object ids used in the scenarios
REG, COMP, SUBC, VPR, WM, SEAT, PTR, KBD, OUT, FRAC_MGR = 2, 3, 4, 5, 6, 7, 8, 9, 20, 21
S, V0, XS, TL, L, LSUB, LV = 10, 11, 12, 13, 14, 15, 16


def fixed(v):
    return P.float_to_fixed(v)


class Harness:
    def __init__(self, zoom="50", frame=False, width=1280, height=720, close_action="stop"):
        self.events = []
        cfg = wp.Config("3", "Game", width, height, zoom, frame, "dark", close_action)
        self.s = wp.Session(cfg, self.events.append)
        self.c2s = wp.Stream(self.s.on_request, self.s.post_feed_c2s)
        self.s2c = wp.Stream(self.s.on_event)
        self.s.c2s, self.s.s2c = self.c2s, self.s2c

    def req(self, *msgs, fds=()):
        self.c2s.feed(b"".join(msgs), list(fds))
        out = parse(bytes(self.c2s.out))
        self.c2s.out.clear()
        return out

    def ev(self, *msgs):
        self.s2c.feed(b"".join(msgs), [])
        out = parse(bytes(self.s2c.out))
        self.s2c.out.clear()
        return out

    def server_out(self):
        out = parse(bytes(self.c2s.out))
        self.c2s.out.clear()
        return out

    def setup_globals(self, frame_globals=False):
        self.req(msg(1, P.WL_DISPLAY_GET_REGISTRY, "n", REG))
        globs = [(1, "wl_compositor", 6), (2, "wl_subcompositor", 1), (3, "wp_viewporter", 1),
                 (4, "xdg_wm_base", 7), (5, "wl_seat", 9), (6, "wl_output", 4), (7, "wl_shm", 1),
                 (8, "wp_fractional_scale_manager_v1", 1)]
        if frame_globals:
            globs += [(9, "wp_single_pixel_buffer_manager_v1", 1), (10, "wp_cursor_shape_manager_v1", 2)]
        self.ev(*[msg(REG, P.WL_REGISTRY_EV_GLOBAL, "usu", n, i, v) for n, i, v in globs])
        binds = [(1, "wl_compositor", 5, COMP), (2, "wl_subcompositor", 1, SUBC), (3, "wp_viewporter", 1, VPR),
                 (4, "xdg_wm_base", 1, WM), (5, "wl_seat", 5, SEAT), (6, "wl_output", 3, OUT),
                 (8, "wp_fractional_scale_manager_v1", 1, FRAC_MGR)]
        self.req(*[msg(REG, P.WL_REGISTRY_BIND, "usun", n, i, v, new) for n, i, v, new in binds])
        self.req(msg(SEAT, P.WL_SEAT_GET_POINTER, "n", PTR), msg(SEAT, P.WL_SEAT_GET_KEYBOARD, "n", KBD))
        # output: 1366x768 at scale 1
        self.ev(msg(OUT, P.WL_OUTPUT_EV_MODE, "uiii", 1, 1366, 768, 60000))

    def create_window(self, app_id="Waydroid"):
        return self.req(
            msg(COMP, P.WL_COMPOSITOR_CREATE_SURFACE, "n", S),
            msg(VPR, P.WP_VIEWPORTER_GET_VIEWPORT, "no", V0, S),
            msg(WM, P.XDG_WM_BASE_GET_XDG_SURFACE, "no", XS, S),
            msg(XS, P.XDG_SURFACE_GET_TOPLEVEL, "n", TL),
            msg(TL, P.XDG_TOPLEVEL_SET_TITLE, "s", "Waydroid"),
            msg(TL, P.XDG_TOPLEVEL_SET_APP_ID, "s", app_id),
            msg(COMP, P.WL_COMPOSITOR_CREATE_SURFACE, "n", L),
            msg(SUBC, P.WL_SUBCOMPOSITOR_GET_SUBSURFACE, "noo", LSUB, L, S),
            msg(VPR, P.WP_VIEWPORTER_GET_VIEWPORT, "no", LV, L),
        )


def args(payload, sig):
    r = Reader(payload)
    return [getattr(r, k)() for k in sig]


class LabelTest(unittest.TestCase):
    def test_app_window_labels(self):
        h = Harness()
        h.setup_globals()
        out = h.create_window(app_id="waydroid.com.example.game")
        titles = {op: args(p, "s")[0] for o, op, p in out if o == TL}
        self.assertEqual(titles[P.XDG_TOPLEVEL_SET_TITLE], "Waydroid · Game")
        self.assertEqual(titles[P.XDG_TOPLEVEL_SET_APP_ID], "waydroid-manager.3.com.example.game")
        self.assertIsNone(h.s.window)       # not the full-UI window

    def test_full_ui_labels(self):
        h = Harness()
        h.setup_globals()
        out = h.create_window()
        app = [args(p, "s")[0] for o, op, p in out if o == TL and op == P.XDG_TOPLEVEL_SET_APP_ID]
        self.assertEqual(app, ["waydroid-manager.3"])
        self.assertIsNotNone(h.s.window)


class AppWindowConfigureTest(unittest.TestCase):
    def test_unchanged_size_hidden(self):
        # The HWC hotplugs Android's display on every sized configure, and the desktop sends one
        # on each focus change (the activated state, 4): only a new size may reach it
        h = Harness()
        h.setup_globals()
        h.create_window(app_id="waydroid.com.example.game")
        focused = struct.pack("=I", 4)
        sizes = []
        for w, ht, states in ((1280, 720, b""), (1280, 720, focused), (0, 0, b""), (1280, 720, b""),
                              (1000, 600, focused)):
            (_, _, p), = h.ev(msg(TL, P.XDG_TOPLEVEL_EV_CONFIGURE, "iia", w, ht, states))
            sizes.append(args(p, "ii"))
        self.assertEqual(sizes, [[1280, 720], [0, 0], [0, 0], [0, 0], [1000, 600]])


class CalibrationMaximizeTest(unittest.TestCase):
    def test_maximize_before_app_id_is_dropped(self):
        # The HWC maximizes its full-UI toplevel before naming it: left alone, GNOME keeps it
        # maximized (the unmaximize comes once it is known, and is dropped)
        h = Harness()
        h.setup_globals()
        h.req(msg(COMP, P.WL_COMPOSITOR_CREATE_SURFACE, "n", S),
              msg(WM, P.XDG_WM_BASE_GET_XDG_SURFACE, "no", XS, S),
              msg(XS, P.XDG_SURFACE_GET_TOPLEVEL, "n", TL))
        self.assertEqual(h.req(msg(TL, P.XDG_TOPLEVEL_SET_MAXIMIZED)), [])
        h.req(msg(TL, P.XDG_TOPLEVEL_SET_APP_ID, "s", "Waydroid"))
        self.assertEqual(h.req(msg(TL, P.XDG_TOPLEVEL_UNSET_MAXIMIZED)), [])

    def test_app_windows_may_maximize(self):
        h = Harness()
        h.setup_globals()
        h.create_window(app_id="waydroid.com.example.game")
        (o, op, _), = h.req(msg(TL, P.XDG_TOPLEVEL_SET_MAXIMIZED))
        self.assertEqual((o, op), (TL, P.XDG_TOPLEVEL_SET_MAXIMIZED))


class ZoomTest(unittest.TestCase):
    def setUp(self):
        self.h = Harness(zoom="50")
        self.h.setup_globals()
        self.h.create_window()

    def test_destinations_positions_geometry_scaled(self):
        h = self.h
        out = h.req(msg(V0, P.WP_VIEWPORT_SET_DESTINATION, "ii", 1280, 720),
                    msg(LV, P.WP_VIEWPORT_SET_DESTINATION, "ii", 1280, 720),
                    msg(LSUB, P.WL_SUBSURFACE_SET_POSITION, "ii", 0, 0),
                    msg(XS, P.XDG_SURFACE_SET_WINDOW_GEOMETRY, "iiii", 0, 0, 1280, 720))
        d = {o: args(p, "iiii"[:len(p) // 4]) for o, op, p in out}
        self.assertEqual(d[V0], [640, 360])
        self.assertEqual(d[LV], [640, 360])
        self.assertEqual(d[LSUB], [0, 0])
        self.assertEqual(d[XS], [0, 0, 640, 360])

    def test_damage_becomes_full_and_opaque_region_dropped(self):
        out = self.h.req(msg(L, P.WL_SURFACE_DAMAGE, "iiii", 10, 10, 20, 20),
                         msg(S, P.WL_SURFACE_SET_OPAQUE_REGION, "o", 99))
        self.assertEqual(len(out), 1)
        self.assertEqual(args(out[0][2], "iiii"), [0, 0, wp.FULL_DAMAGE, wp.FULL_DAMAGE])

    def test_pointer_and_touch_unscaled(self):
        h = self.h
        out = h.ev(msg(PTR, P.WL_POINTER_EV_ENTER, "uoff", 5, L, fixed(320.0), fixed(180.5)),
                   msg(PTR, P.WL_POINTER_EV_MOTION, "uff", 1, fixed(10.0), fixed(20.0)))
        enter = args(out[0][2], "uoff")
        self.assertEqual((P.fixed_to_float(enter[2]), P.fixed_to_float(enter[3])), (640.0, 361.0))
        motion = args(out[1][2], "uff")
        self.assertEqual((P.fixed_to_float(motion[1]), P.fixed_to_float(motion[2])), (20.0, 40.0))

    def test_configure_hidden_and_resize_rezooms(self):
        h = self.h
        h.req(msg(V0, P.WP_VIEWPORT_SET_DESTINATION, "ii", 1280, 720), msg(S, P.WL_SURFACE_COMMIT))
        states = struct.pack("=I", P.XDG_TOPLEVEL_STATE_RESIZING)
        out = h.ev(msg(TL, P.XDG_TOPLEVEL_EV_CONFIGURE, "iia", 960, 540, states),
                   msg(XS, P.XDG_SURFACE_EV_CONFIGURE, "u", 77))
        cfg = args(out[0][2], "iia")
        self.assertEqual(cfg[:2], [0, 0])          # the HWC never sees a real size
        self.assertAlmostEqual(h.s.zoom, 0.75)
        out = h.req(msg(XS, P.XDG_SURFACE_ACK_CONFIGURE, "u", 77))
        dests = {o: args(p, "ii") for o, op, p in out if o == V0}
        self.assertEqual(dests[V0], [960, 540])
        self.assertIn((S, P.WL_SURFACE_COMMIT), [(o, op) for o, op, _ in out])

    def test_toplevel_without_viewport_gets_ours(self):
        """Android 11's HWC draws on the toplevel's surface and gives it no viewport."""
        h = Harness(zoom="50")
        h.setup_globals()
        h.req(msg(COMP, P.WL_COMPOSITOR_CREATE_SURFACE, "n", S),
              msg(WM, P.XDG_WM_BASE_GET_XDG_SURFACE, "no", XS, S),
              msg(XS, P.XDG_SURFACE_GET_TOPLEVEL, "n", TL),
              msg(TL, P.XDG_TOPLEVEL_SET_APP_ID, "s", "Waydroid"),
              msg(XS, P.XDG_SURFACE_SET_WINDOW_GEOMETRY, "iiii", 0, 0, 1280, 720))
        out = h.req(msg(S, P.WL_SURFACE_COMMIT))
        vp = h.s.window.own_vp
        self.assertIn((h.s.my_globals["wp_viewporter"], P.WP_VIEWPORTER_GET_VIEWPORT), [(o, op) for o, op, _ in out])
        self.assertEqual([args(p, "ii") for o, op, p in out if o == vp], [[640, 360]])
        h.ev(msg(TL, P.XDG_TOPLEVEL_EV_CONFIGURE, "iia", 960, 540, struct.pack("=I", P.XDG_TOPLEVEL_STATE_RESIZING)),
             msg(XS, P.XDG_SURFACE_EV_CONFIGURE, "u", 77))
        out = h.req(msg(XS, P.XDG_SURFACE_ACK_CONFIGURE, "u", 77))
        self.assertEqual([args(p, "ii") for o, op, p in out if o == vp], [[960, 540]])
        # the HWC asking for one after all: ours goes first
        out = h.req(msg(VPR, P.WP_VIEWPORTER_GET_VIEWPORT, "no", V0, S))
        self.assertEqual([(o, op) for o, op, _ in out], [(vp, P.WP_VIEWPORT_DESTROY), (VPR, P.WP_VIEWPORTER_GET_VIEWPORT)])
        self.assertIsNone(h.s.window.own_vp)

    def test_output_scale_normalised(self):
        h = self.h
        out = h.ev(msg(OUT, P.WL_OUTPUT_EV_SCALE, "i", 2))
        self.assertEqual(args(out[0][2], "i"), [1])
        self.assertEqual(h.s.outputs[OUT]["scale"], 2)
        h.req(msg(FRAC_MGR, P.WP_FRACTIONAL_SCALE_MANAGER_GET, "no", 30, S))
        out = h.ev(msg(30, P.WP_FRACTIONAL_SCALE_EV_PREFERRED, "u", 180))
        self.assertEqual(args(out[0][2], "u"), [120])

    def test_calibration_maximize_dropped(self):
        self.assertEqual(self.h.req(msg(TL, P.XDG_TOPLEVEL_SET_MAXIMIZED)), [])

    def test_auto_zoom_fits_output(self):
        h = Harness(zoom="auto", width=1080, height=1920)
        h.setup_globals()
        h.create_window()
        h.req(msg(V0, P.WP_VIEWPORT_SET_DESTINATION, "ii", 1080, 1920))
        self.assertAlmostEqual(h.s.zoom, (768 - wp.PANEL_ALLOWANCE) / 1920, places=3)


class FrameTest(unittest.TestCase):
    def setUp(self):
        self.h = Harness(zoom="50", frame=True)
        self.h.setup_globals(frame_globals=True)
        self.h.create_window()
        self.h.req(msg(V0, P.WP_VIEWPORT_SET_DESTINATION, "ii", 1280, 720),
                   msg(XS, P.XDG_SURFACE_SET_WINDOW_GEOMETRY, "iiii", 0, 0, 1280, 720))
        self.out = self.h.req(msg(S, P.WL_SURFACE_COMMIT))   # first commit maps the window

    def test_frame_created_with_fd_before_pool_users(self):
        h = self.h
        out = self.out
        kinds = set(h.s.mine.values())
        self.assertTrue({"surface_title", "surface_toolbar", "surface_border"} <= kinds)
        ops = [(o, op) for o, op, _ in out]
        shm = h.s.my_globals["wl_shm"]
        pool_msgs = [i for i, (o, op) in enumerate(ops) if o == shm and op == P.WL_SHM_CREATE_POOL]
        self.assertEqual(len(pool_msgs), 2)                       # title + toolbar
        self.assertEqual(len(h.c2s.fds), 2)                       # one memfd per pool, queued in order
        for i in pool_msgs:
            pool = args(out[i][2], "ni")[0]
            users = [j for j, (o, op) in enumerate(ops) if o == pool]
            self.assertTrue(all(j > i for j in users))
        geo = [args(p, "iiii") for o, op, p in out if o == XS and op == P.XDG_SURFACE_SET_WINDOW_GEOMETRY]
        self.assertEqual(geo[-1], [0, -32, 640 + 40, 360 + 32])
        for fd in h.c2s.fds:
            os.close(fd)

    def test_title_drag_moves_window(self):
        h = self.h
        title = h.s.window.frame["title"]
        out = h.ev(msg(PTR, P.WL_POINTER_EV_ENTER, "uoff", 9, title, fixed(100.0), fixed(10.0)),
                   msg(PTR, P.WL_POINTER_EV_FRAME),
                   msg(PTR, P.WL_POINTER_EV_BUTTON, "uuuu", 10, 0, P.BTN_LEFT, 1),
                   msg(PTR, P.WL_POINTER_EV_FRAME))
        self.assertEqual(out, [])                                # nothing about our surfaces reaches the HWC
        moves = [args(p, "ou") for o, op, p in h.server_out() if o == TL and op == P.XDG_TOPLEVEL_MOVE]
        self.assertEqual(moves, [[SEAT, 10]])
        for fd in h.c2s.fds:
            os.close(fd)

    def test_toolbar_back_sends_key(self):
        h = self.h
        toolbar = h.s.window.frame["toolbar"]
        height = h.s.window.frame["sizes"]["toolbar"][1]
        y0, y1 = [(a, b) for x, a, b in wp.fr.toolbar_layout(height) if x == "back"][0]
        self.assertGreater(y0, height / 2)                       # with Home and Recents at the bottom
        y = (y0 + y1) / 2
        out = h.ev(msg(PTR, P.WL_POINTER_EV_ENTER, "uoff", 9, toolbar, fixed(20.0), fixed(y)),
                   msg(PTR, P.WL_POINTER_EV_BUTTON, "uuuu", 10, 0, P.BTN_LEFT, 1),
                   msg(PTR, P.WL_POINTER_EV_BUTTON, "uuuu", 11, 0, P.BTN_LEFT, 0))
        # only the synthesized key events reach the HWC (none of the pointer events)
        self.assertTrue(all(o == KBD for o, op, p in out))
        keys = [args(p, "uuuu")[2:] for o, op, p in out if o == KBD]
        self.assertEqual(keys, [[158, 1], [158, 0]])
        for fd in h.c2s.fds:
            os.close(fd)

    def test_toolbar_tooltip(self):
        h = self.h
        f = h.s.window.frame
        # the tooltip never takes input: it gets an (empty) input region at creation
        self.assertIn(P.WL_SURFACE_SET_INPUT_REGION, [op for o, op, _ in self.out if o == f["tip"]])
        y0, y1 = [(a, b) for x, a, b in wp.fr.toolbar_layout(720) if x == "install"][0]
        out = h.ev(msg(PTR, P.WL_POINTER_EV_ENTER, "uoff", 9, f["toolbar"], fixed(20.0), fixed((y0 + y1) / 2)))
        self.assertEqual(out, [])
        h.server_out()
        self.assertFalse(h.s.tick())                             # not before the delay
        h.s.tip_due = (0, h.s.tip_due[1])
        self.assertTrue(h.s.tick())
        out = h.server_out()
        w, th = wp.fr.tooltip_size("Install APK")
        pos = [args(p, "ii") for o, op, p in out if o == f["tip_sub"] and op == P.WL_SUBSURFACE_SET_POSITION]
        self.assertEqual(pos, [[-w - 6, (y0 + y1 - th) // 2]])  # left of the button, centred on it
        self.assertIn((f["toolbar"], P.WL_SURFACE_COMMIT), [(o, op) for o, op, _ in out])
        h.ev(msg(PTR, P.WL_POINTER_EV_LEAVE, "uo", 12, f["toolbar"]))
        attach = [args(p, "oii") for o, op, p in h.server_out() if o == f["tip"] and op == P.WL_SURFACE_ATTACH]
        self.assertEqual(attach, [[0, 0, 0]])                   # hidden
        self.assertIsNone(h.s.tip_due)
        for fd in h.c2s.fds:
            os.close(fd)

    def test_short_toolbar_scrolls(self):
        h = self.h
        h.s.set_zoom(0.25)                                       # a 180 px tall toolbar
        f = h.s.window.frame
        height = f["sizes"]["toolbar"][1]
        y = wp.fr.toolbar_end(height) - 10
        self.assertEqual(wp.fr.hit_toolbar(20, 20, height), "settings")
        self.assertNotEqual(h.s._hit("toolbar", 20, y), "fullscreen")
        h.ev(msg(PTR, P.WL_POINTER_EV_ENTER, "uoff", 9, f["toolbar"], fixed(20.0), fixed(y)),
             *[msg(PTR, P.WL_POINTER_EV_AXIS, "uuf", 10, 0, fixed(15.0))] * 20)
        self.assertEqual(h.s.tb_scroll, wp.fr.toolbar_scroll_max(height))   # down to the last entry
        self.assertEqual(h.s.hover["toolbar"], "fullscreen")
        self.assertEqual(h.s._hit("toolbar", 20, height - 20), "recents")  # the navigation stays put
        h.ev(*[msg(PTR, P.WL_POINTER_EV_AXIS, "uuf", 11, 0, fixed(-15.0))] * 20)
        self.assertEqual(h.s.tb_scroll, 0)
        # a touchpad scrolls by fractions of a pixel: the tooltip still goes up
        h.ev(msg(PTR, P.WL_POINTER_EV_AXIS, "uuf", 12, 0, fixed(3.3)))
        h.s.tip_due = (0, "settings")
        self.assertTrue(h.s.tick())
        for fd in h.c2s.fds:
            os.close(fd)

    def test_toolbar_settings_asks_session(self):
        h = self.h
        layout = wp.fr.toolbar_layout(h.s.window.frame["sizes"]["toolbar"][1])
        self.assertEqual(layout[0][0], "settings")               # first, so a short window keeps it
        y0, y1 = layout[0][1:]
        out = h.ev(msg(PTR, P.WL_POINTER_EV_ENTER, "uoff", 9, h.s.window.frame["toolbar"], fixed(20.0),
                       fixed((y0 + y1) / 2)),
                   msg(PTR, P.WL_POINTER_EV_BUTTON, "uuuu", 10, 0, P.BTN_LEFT, 1),
                   msg(PTR, P.WL_POINTER_EV_BUTTON, "uuuu", 11, 0, P.BTN_LEFT, 0))
        self.assertEqual(out, [])
        self.assertIn("action settings", h.events)
        for fd in h.c2s.fds:
            os.close(fd)

    def test_recents_goes_through_daemon(self):
        h = self.h
        h.s.do_action("recents")
        self.assertIn("action key 580", h.events)
        for fd in h.c2s.fds:
            os.close(fd)

    def test_close_not_forwarded(self):
        h = self.h
        out = h.ev(msg(TL, P.XDG_TOPLEVEL_EV_CLOSE))
        self.assertEqual(out, [])
        self.assertIn("close", h.events)
        for fd in h.c2s.fds:
            os.close(fd)

    def test_my_object_events_swallowed(self):
        h = self.h
        buf = next(i for i, k in h.s.mine.items() if k == "buffer_shm")
        self.assertEqual(h.ev(msg(buf, 0)), [])                  # wl_buffer.release
        self.assertEqual(h.ev(msg(1, P.WL_DISPLAY_EV_DELETE_ID, "u", buf)), [])
        for fd in h.c2s.fds:
            os.close(fd)


class WindowStateTest(unittest.TestCase):
    def setUp(self):
        self.h = Harness(zoom="50", frame=True)
        self.h.setup_globals(frame_globals=True)
        self.h.create_window()
        self.h.req(msg(V0, P.WP_VIEWPORT_SET_DESTINATION, "ii", 1280, 720),
                   msg(LSUB, P.WL_SUBSURFACE_SET_POSITION, "ii", 0, 0))
        self.h.req(msg(S, P.WL_SURFACE_COMMIT))
        self.h.server_out()

    def tearDown(self):
        for fd in self.h.c2s.fds:
            os.close(fd)

    def configure(self, w, h_, *states):
        raw = struct.pack("={}I".format(len(states)), *states)
        self.h.ev(msg(TL, P.XDG_TOPLEVEL_EV_CONFIGURE, "iia", w, h_, raw), msg(XS, P.XDG_SURFACE_EV_CONFIGURE, "u", 5))
        return self.h.req(msg(XS, P.XDG_SURFACE_ACK_CONFIGURE, "u", 5))

    def test_maximized_fills_with_centred_content(self):
        h = self.h
        out = self.configure(1366, 736, P.XDG_TOPLEVEL_STATE_MAXIMIZED)
        geo = [args(p, "iiii") for o, op, p in out if o == XS and op == P.XDG_SURFACE_SET_WINDOW_GEOMETRY]
        self.assertEqual(geo[-1], [0, -32, 1366, 736])                       # exactly the maximized size
        area = (1366 - 40, 736 - 32)
        self.assertAlmostEqual(h.s.zoom, min(area[0] / 1280, area[1] / 720))
        dest = [args(p, "ii") for o, op, p in out if o == V0]
        self.assertEqual(dest[-1], list(area))                              # black letterbox fills the area
        pos = [args(p, "ii") for o, op, p in out if o == LSUB]
        dw = int(round(1280 * h.s.zoom))
        self.assertEqual(pos[-1][0], (area[0] - dw) // 2)                   # content centred
        # un-maximize restores the previous zoom
        self.configure(0, 0)
        self.assertAlmostEqual(h.s.zoom, 0.5)

    def test_double_click_title_toggles_maximize(self):
        h = self.h
        title = h.s.window.frame["title"]
        h.ev(msg(PTR, P.WL_POINTER_EV_ENTER, "uoff", 9, title, fixed(100.0), fixed(10.0)),
             msg(PTR, P.WL_POINTER_EV_BUTTON, "uuuu", 10, 0, P.BTN_LEFT, 1),
             msg(PTR, P.WL_POINTER_EV_BUTTON, "uuuu", 11, 0, P.BTN_LEFT, 0),
             msg(PTR, P.WL_POINTER_EV_BUTTON, "uuuu", 12, 0, P.BTN_LEFT, 1))
        ops = [op for o, op, p in h.server_out() if o == TL]
        self.assertIn(P.XDG_TOPLEVEL_SET_MAXIMIZED, ops)

    def test_f11_and_esc_control_fullscreen(self):
        h = self.h
        h.ev(msg(KBD, P.WL_KEYBOARD_EV_ENTER, "uoa", 3, S, b""))
        out = h.ev(msg(KBD, P.WL_KEYBOARD_EV_KEY, "uuuu", 4, 0, 87, 1),
                   msg(KBD, P.WL_KEYBOARD_EV_KEY, "uuuu", 5, 0, 87, 0))
        self.assertEqual(out, [])                                           # F11 never reaches Android
        self.assertIn(P.XDG_TOPLEVEL_SET_FULLSCREEN, [op for o, op, p in h.server_out() if o == TL])
        self.configure(1366, 768, P.XDG_TOPLEVEL_STATE_FULLSCREEN)
        h.server_out()
        out = h.ev(msg(KBD, P.WL_KEYBOARD_EV_KEY, "uuuu", 6, 0, 1, 1),
                   msg(KBD, P.WL_KEYBOARD_EV_KEY, "uuuu", 7, 0, 1, 0))
        self.assertEqual(out, [])
        self.assertIn(P.XDG_TOPLEVEL_UNSET_FULLSCREEN, [op for o, op, p in h.server_out() if o == TL])
        # outside fullscreen Esc is a normal key (Android Back)
        self.configure(0, 0)
        out = h.ev(msg(KBD, P.WL_KEYBOARD_EV_KEY, "uuuu", 8, 0, 1, 1))
        self.assertEqual(len(out), 1)

    def test_nav_glyphs_render(self):
        from waydroid_manager.session import frame
        data, w, h_, stride = frame.render_toolbar(400, 1, "dark")
        self.assertEqual(len(data), stride * h_)


class StreamTest(unittest.TestCase):
    def test_fd_injection_waits_for_partial_message(self):
        st = wp.Stream(lambda o, op, p: None)
        st.feed(msg(5, 0, "u", 1)[:6], [])          # partial client message buffered
        st.inject(b"X" * 8, fds=[123])
        self.assertEqual(st.fds, [])                # not queued while a partial message is pending
        st.feed(msg(5, 0, "u", 1)[6:], [])
        self.assertEqual(st.fds, [123])
        self.assertTrue(bytes(st.out).endswith(b"X" * 8))

    def test_injection_during_feed_goes_after_current_messages(self):
        st = None

        def handler(o, op, p):
            st.inject(b"INJECTED")
            return None
        st = wp.Stream(handler)
        data = msg(5, 0, "u", 1) + msg(6, 0, "u", 2)
        st.feed(data, [])
        self.assertTrue(bytes(st.out).startswith(data))


    def test_tracked_fds_follow_their_messages(self):
        st = None

        def handler(o, op, p):
            if o == 5:                                  # drop it, keeping its fd
                taken.append(st.take_fd())
                return []
            return None
        taken = []
        st = wp.Stream(handler, count_fds=lambda o, op: 1)
        r1, w1 = os.pipe()
        r2, w2 = os.pipe()
        st.feed(msg(5, 0, "u", 1) + msg(6, 0, "u", 2), [w1, w2])
        self.assertEqual(taken, [w1])
        self.assertEqual(st.fds, [w2])                  # the forwarded message's fd only
        self.assertEqual([o for o, _, _ in parse(bytes(st.out))], [6])
        st.inject(msg(7, 0, "u", 3), fds=[r2])
        self.assertEqual((st.fds, st.out_fds), ([w2, r2], 2))
        for fd in (r1, w1, r2, w2):
            os.close(fd)


DDM, DD, OFFER = 30, 31, 0xff000001


class ClipboardTest(unittest.TestCase):
    """The HWC's clipboard reads are answered by the proxy, never by a slow owner."""

    def setUp(self):
        self.h = h = Harness(frame=False)
        h.c2s = h.s.c2s = wp.Stream(h.s.on_request, h.s.post_feed_c2s, count_fds=lambda o, op: int(
            h.s.objs.get(o) == "wl_data_offer" and op == P.WL_DATA_OFFER_RECEIVE))
        h.setup_globals()
        h.req(msg(REG, P.WL_REGISTRY_BIND, "usun", 9, "wl_data_device_manager", 3, DDM))
        h.req(msg(DDM, P.WL_DATA_DEVICE_MANAGER_GET_DATA_DEVICE, "no", DD, SEAT))

    def offer_selection(self, offer=OFFER):
        h = self.h
        h.ev(msg(DD, P.WL_DATA_DEVICE_EV_DATA_OFFER, "n", offer),
             msg(offer, P.WL_DATA_OFFER_EV_OFFER, "s", "UTF8_STRING"),
             msg(offer, P.WL_DATA_OFFER_EV_OFFER, "s", "text/plain;charset=utf-8"),
             msg(DD, P.WL_DATA_DEVICE_EV_SELECTION, "o", offer))

    def sent_fd(self):
        """The fd our receive sent to the compositor (as flush() would)."""
        self.h.c2s.out_fds -= 1
        return self.h.c2s.fds.pop(0)

    def hwc_reads(self, offer=OFFER):
        """The HWC's read_selection: returns what it reads, and how long it was blocked."""
        r, w = os.pipe()
        self.assertEqual(self.h.req(msg(offer, P.WL_DATA_OFFER_RECEIVE, "s", "text/plain;charset=utf-8"),
                                    fds=[w]), [])           # answered by us, not forwarded
        t0, data = time.monotonic(), b""
        while True:
            b = os.read(r, 4096)
            if not b:
                break
            data += b
        os.close(r)
        return data, time.monotonic() - t0

    def test_text_fetched_by_us_reaches_the_hwc(self):
        self.offer_selection()
        (o, op, p), = self.h.server_out()                  # we asked the compositor ourselves
        self.assertEqual((o, op, args(p, "s")), (OFFER, P.WL_DATA_OFFER_RECEIVE, ["text/plain;charset=utf-8"]))
        owner = self.sent_fd()                              # the owner writes and closes
        os.write(owner, "olá".encode())
        os.close(owner)
        self.assertEqual(self.hwc_reads()[0], "olá".encode())

    def test_slow_owner_does_not_block_the_hwc(self):
        self.offer_selection()
        self.h.server_out()
        owner = self.sent_fd()                              # never answers
        data, blocked = self.hwc_reads()
        self.assertEqual(data, b"")
        self.assertLess(blocked, wp.CLIP_WAIT + 1)
        self.assertEqual(self.h.s.clip_stats["late"], 1)
        os.close(owner)

    def test_own_selection_answered_without_asking(self):
        h = self.h
        h.req(msg(DDM, P.WL_DATA_DEVICE_MANAGER_CREATE_DATA_SOURCE, "n", 40),
              msg(DD, P.WL_DATA_DEVICE_SET_SELECTION, "ou", 40, 7))
        self.offer_selection()
        self.assertEqual(h.server_out(), [])
        self.assertEqual(self.hwc_reads()[0], b"")


class CpuBufferTest(unittest.TestCase):
    """Android's dmabufs reach the compositor as shm: over the same fd ("shared", software
    rendering), or in memory of our own filled through the GPU they come from."""
    SHM, DMABUF, PARAMS, BUF = 30, 31, 32, 33
    AB24 = 0x34324241

    def setUp(self):
        self.h = h = Harness()
        h.s.cfg.cpu_buffers = "shared"
        h.c2s = h.s.c2s = wp.Stream(h.s.on_request, h.s.post_feed_c2s, count_fds=lambda o, op: int(
            h.s.objs.get(o) == "zwp_linux_buffer_params_v1" and op == P.ZWP_LINUX_BUFFER_PARAMS_ADD))
        h.setup_globals()
        h.req(msg(REG, P.WL_REGISTRY_BIND, "usun", 7, "wl_shm", 1, self.SHM),
              msg(REG, P.WL_REGISTRY_BIND, "usun", 11, "zwp_linux_dmabuf_v1", 3, self.DMABUF))
        h.ev(msg(self.SHM, P.WL_SHM_EV_FORMAT, "u", 0), msg(self.SHM, P.WL_SHM_EV_FORMAT, "u", self.AB24))
        self.fd = os.memfd_create("dmabuf")
        os.ftruncate(self.fd, 64 * 4 * 32)

    def create(self, modifier=0):
        h = self.h
        sent = h.req(msg(self.DMABUF, P.ZWP_LINUX_DMABUF_CREATE_PARAMS, "n", self.PARAMS),
                     msg(self.PARAMS, P.ZWP_LINUX_BUFFER_PARAMS_ADD, "uuuuu", 0, 0, 64 * 4, modifier >> 32,
                         modifier & 0xffffffff), fds=[self.fd])
        sent += h.req(msg(self.PARAMS, P.ZWP_LINUX_BUFFER_PARAMS_CREATE_IMMED, "niiuu", self.BUF, 64, 32,
                          self.AB24, 0))
        fds = h.c2s.fds[:h.c2s.out_fds]
        return [(o, op) for o, op, _ in sent], sent[-2:], fds

    def test_linear_buffer_becomes_shm_on_its_own_fd(self):
        ops, (create, destroy), fds = self.create()
        pool = create[0]
        self.assertEqual(ops[:2], [(self.DMABUF, P.ZWP_LINUX_DMABUF_CREATE_PARAMS),
                                   (self.PARAMS, P.ZWP_LINUX_BUFFER_PARAMS_ADD)])
        self.assertEqual(ops[2], (self.SHM, P.WL_SHM_CREATE_POOL))
        self.assertEqual((create[1], args(create[2], "niiiiu")), (P.WL_SHM_POOL_CREATE_BUFFER,
                                                                  [self.BUF, 0, 64, 32, 256, self.AB24]))
        self.assertEqual(destroy[:2], (pool, P.WL_SHM_POOL_DESTROY))
        self.assertEqual(len(fds), 2)          # the dmabuf for add, a copy of it for create_pool
        self.assertEqual(os.fstat(fds[1]).st_ino, os.fstat(self.fd).st_ino)

    def test_tiled_buffer_stays_a_dmabuf(self):
        ops, _, fds = self.create(modifier=1 << 56 | 4)
        self.assertEqual(ops[-1], (self.PARAMS, P.ZWP_LINUX_BUFFER_PARAMS_CREATE_IMMED))
        self.assertEqual(len(fds), 1)

    def test_gpu_buffer_is_copied_into_our_memory_on_attach(self):
        h, calls = self.h, []

        class Gpu:
            def import_buffer(self, *a):
                calls.append(a[1:])
                return "bo"

            def read(self, bo, width, height, mem, stride):
                calls.append(("read", bo, width, height, stride))
                mem[:5] = b"frame"

            def free(self, bo):
                calls.append(("free", bo))
        h.s.cfg.gpu = Gpu()
        tiled = 1 << 56 | 4                 # any layout the GPU reads
        ops, (create, _), fds = self.create(modifier=tiled)
        self.assertEqual(calls, [(64, 32, self.AB24, 0, 256, tiled)])
        self.assertEqual(ops[2], (self.SHM, P.WL_SHM_CREATE_POOL))
        self.assertEqual(args(create[2], "niiiiu"), [self.BUF, 0, 64, 32, 256, self.AB24])
        self.assertNotEqual(os.fstat(fds[1]).st_ino, os.fstat(self.fd).st_ino)
        h.req(msg(COMP, P.WL_COMPOSITOR_CREATE_SURFACE, "n", S), msg(S, P.WL_SURFACE_ATTACH, "oii", self.BUF, 0, 0))
        self.assertEqual(calls[-1], ("read", "bo", 64, 32, 256))
        self.assertEqual(os.pread(fds[1], 5, 0), b"frame")
        h.ev(msg(1, P.WL_DISPLAY_EV_DELETE_ID, "u", self.BUF))
        self.assertEqual(calls[-1], ("free", "bo"))


class FileDropTest(unittest.TestCase):
    """Files dropped on the window are the proxy's: the APKs among them are reported for installing."""

    def setUp(self):
        self.h = h = Harness(frame=False)
        h.c2s = h.s.c2s = wp.Stream(h.s.on_request, h.s.post_feed_c2s, count_fds=lambda o, op: int(
            h.s.objs.get(o) == "wl_data_offer" and op == P.WL_DATA_OFFER_RECEIVE))
        h.setup_globals()
        h.create_window()
        h.req(msg(REG, P.WL_REGISTRY_BIND, "usun", 9, "wl_data_device_manager", 3, DDM))
        h.req(msg(DDM, P.WL_DATA_DEVICE_MANAGER_GET_DATA_DEVICE, "no", DD, SEAT))
        self.watched = []
        h.s.watch = lambda fd, done: self.watched.append((fd, done))

    def drag(self, *mimes):
        h = self.h
        h.ev(msg(DD, P.WL_DATA_DEVICE_EV_DATA_OFFER, "n", OFFER),
             *[msg(OFFER, P.WL_DATA_OFFER_EV_OFFER, "s", m) for m in mimes])
        return h.ev(msg(DD, P.WL_DATA_DEVICE_EV_ENTER, "uoffo", 9, S, fixed(10.0), fixed(10.0), OFFER))

    def test_apks_dropped_on_the_window_are_installed(self):
        h = self.h
        self.assertEqual(self.drag("text/uri-list", "text/plain;charset=utf-8"), [])   # the HWC sees nothing
        sent = [(o, op) for o, op, _ in h.server_out()]
        self.assertEqual(sent, [(OFFER, P.WL_DATA_OFFER_ACCEPT), (OFFER, P.WL_DATA_OFFER_SET_ACTIONS)])
        self.assertEqual(h.ev(msg(OFFER, P.WL_DATA_OFFER_EV_ACTION, "u", P.DND_ACTION_COPY),
                              msg(DD, P.WL_DATA_DEVICE_EV_MOTION, "uff", 1, fixed(20.0), fixed(20.0)),
                              msg(DD, P.WL_DATA_DEVICE_EV_DROP),
                              msg(DD, P.WL_DATA_DEVICE_EV_LEAVE)), [])
        (o, op, p), = h.server_out()
        self.assertEqual((o, op, args(p, "s")), (OFFER, P.WL_DATA_OFFER_RECEIVE, [wp.URI_LIST]))
        h.c2s.out_fds -= 1
        os.close(h.c2s.fds.pop(0))                       # (the source's end of the pipe)
        (fd, done), = self.watched
        os.close(fd)
        done(b"file:///home/u/My%20Game.apk\r\nfile:///home/u/photo.png\r\n")
        self.assertEqual(h.events, ["install /home/u/My Game.apk"])
        self.assertEqual([(o, op) for o, op, _ in h.server_out()], [(OFFER, P.WL_DATA_OFFER_FINISH)])

    def test_other_drags_still_reach_the_hwc(self):
        (o, op, p), = self.drag("text/plain;charset=utf-8")
        self.assertEqual((o, op), (DD, P.WL_DATA_DEVICE_EV_ENTER))
        self.assertEqual(self.h.server_out(), [])

    def test_apk_paths(self):
        self.assertEqual(wp.apk_paths(b"# comment\nfile://localhost/a/B.APK\nhttps://x/c.apk\n"
                                      b"file://otherhost/d.apk\nfile:///e%0Af.apk\nfile:///g.apk.txt\n"
                                      b"file:///h/game.xapk\n"),
                         ["/a/B.APK", "/h/game.xapk"])


class TranslatorTest(unittest.TestCase):
    def setUp(self):
        from waydroid_manager.session import wlschema
        self.tr = wp.Translator(wlschema.load())

    def req(self, data):
        out = self.tr.request(data)
        return parse(out)[0] if out else None

    def test_contiguous_ids_with_proxy_objects_in_between(self):
        tr = self.tr
        self.req(msg(1, P.WL_DISPLAY_GET_REGISTRY, "n", 2))
        self.req(msg(2, P.WL_REGISTRY_BIND, "usun", 1, "wl_compositor", 4, 3))
        # the proxy creates a surface of its own (high id) ...
        o, op, p = self.req(msg(3, P.WL_COMPOSITOR_CREATE_SURFACE, "n", wp.MY_ID_BASE))
        self.assertEqual(args(p, "n"), [4])
        # ... and the HWC's next object gets the next compositor id, not a clash
        o, op, p = self.req(msg(3, P.WL_COMPOSITOR_CREATE_SURFACE, "n", 4))
        self.assertEqual(args(p, "n"), [5])
        self.assertEqual(tr.c2s[4], 5)
        self.assertEqual(tr.c2s[wp.MY_ID_BASE], 4)

    def test_events_translated_back_and_delete_id_frees(self):
        tr = self.tr
        self.req(msg(1, P.WL_DISPLAY_GET_REGISTRY, "n", 2))
        self.req(msg(2, P.WL_REGISTRY_BIND, "usun", 1, "wl_compositor", 4, 3))
        self.req(msg(3, P.WL_COMPOSITOR_CREATE_SURFACE, "n", wp.MY_ID_BASE))    # server id 4
        self.req(msg(3, P.WL_COMPOSITOR_CREATE_SURFACE, "n", 4))                # server id 5
        cobj, payload = tr.event(5, 0, struct.pack("=I", 0))                     # wl_surface.enter on HWC's surface
        self.assertEqual(cobj, 4)
        cobj, payload = tr.event(1, P.WL_DISPLAY_EV_DELETE_ID, struct.pack("=I", 4))
        self.assertEqual(struct.unpack("=I", payload)[0], wp.MY_ID_BASE)       # ours: maps back to our id
        self.assertEqual(tr.free, [4])
        o, op, p = self.req(msg(3, P.WL_COMPOSITOR_CREATE_SURFACE, "n", 6))
        self.assertEqual(args(p, "n"), [4])                                      # freed id reused

    def test_unknown_globals_hidden(self):
        tr = self.tr
        self.req(msg(1, P.WL_DISPLAY_GET_REGISTRY, "n", 2))
        hidden = tr.event(2, P.WL_REGISTRY_EV_GLOBAL, P._enc("u", 40) + P._enc("s", "gtk_shell1") + P._enc("u", 5))
        self.assertIs(hidden, wp.HIDDEN)
        shown = tr.event(2, P.WL_REGISTRY_EV_GLOBAL, P._enc("u", 41) + P._enc("s", "wl_shm") + P._enc("u", 1))
        self.assertIsNotNone(shown)
        self.assertIs(tr.event(2, P.WL_REGISTRY_EV_GLOBAL_REMOVE, P._enc("u", 40)), wp.HIDDEN)

    def test_server_created_objects_keep_ids(self):
        tr = self.tr
        self.req(msg(1, P.WL_DISPLAY_GET_REGISTRY, "n", 2))
        self.req(msg(2, P.WL_REGISTRY_BIND, "usun", 1, "wl_data_device_manager", 3, 3))
        self.req(msg(2, P.WL_REGISTRY_BIND, "usun", 2, "wl_seat", 5, 4))
        self.req(msg(3, 1, "no", 5, 4))                                         # get_data_device(5, seat 4)
        cobj, payload = tr.event(tr.c2s[5], 0, struct.pack("=I", 0xff000001))   # data_offer(new_id)
        self.assertEqual(struct.unpack("=I", payload)[0], 0xff000001)
        self.assertEqual(tr.iface[0xff000001], "wl_data_offer")


class ProxyIntegrationTest(unittest.TestCase):
    """The real proxy between a fake client and a fake compositor."""

    def test_end_to_end_rewrite_and_fd_passing(self):
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
        proxy = wp.Proxy(listen_path, upstream_path, wp.Config("9", "Nine", frame=False), out)
        proxy.bind()
        threading.Thread(target=proxy.run, daemon=True).start()
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(listen_path)
        server, _ = comp.accept()
        r, w = os.pipe()
        data = (msg(1, 1, "n", 2) +
                msg(2, 0, "usun", 1, "wl_compositor", 4, 3) +
                msg(2, 0, "usun", 2, "xdg_wm_base", 1, 4) +
                msg(2, 0, "usun", 3, "wl_shm", 1, 5) +
                msg(3, 0, "n", 6) +                       # create_surface
                msg(4, 2, "no", 7, 6) +                   # get_xdg_surface
                msg(7, 1, "n", 8) +                       # get_toplevel
                msg(8, 2, "s", "Waydroid") +              # set_title
                msg(5, 0, "ni", 9, 4096))                 # wl_shm.create_pool(fd)
        client.sendmsg([data], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [r]))])
        got, fds = b"", []
        server.settimeout(5)
        while len(parse(got)) < 9:
            chunk, anc, _, _ = server.recvmsg(65536, socket.CMSG_SPACE(64))
            got += chunk
            for _level, _type, payload in anc:
                fds.extend(array.array("i", payload[:len(payload) - len(payload) % 4]))
        msgs = parse(got)
        self.assertEqual(args(msgs[7][2], "s"), ["Waydroid · Nine"])
        self.assertEqual(len(fds), 1)
        os.write(w, b"ok")
        self.assertEqual(os.read(fds[0], 2), b"ok")
        for fd in fds + [r, w]:
            os.close(fd)
        client.close()
        server.close()
        comp.close()


if __name__ == "__main__":
    unittest.main()
