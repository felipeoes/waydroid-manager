# SPDX-License-Identifier: GPL-3.0-or-later
"""The root daemon keeps its main loop free and its state watcher alive."""
import collections
import os
import subprocess
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

from waydroid_manager.daemon import container
from waydroid_manager.daemon import main


def manager():
    m = main.Manager.__new__(main.Manager)
    m.locks = collections.defaultdict(threading.Lock)
    m.transient, m.known, m.sessions, m.helpers, m.key_fds = {}, {}, {}, {}, {}
    m.disk, m.last_close, m.picked, m.stopping_by_us = {}, {}, set(), set()
    m.key_lock = threading.Lock()
    m.config_turn = {}
    m.sync_failed = None
    m.StateChanged, m.ConfigChanged = mock.Mock(), mock.Mock()
    return m


def now(f, *a):
    f(*a)


class SetConfigTest(unittest.TestCase):
    def setUp(self):
        self.m = manager()
        self.m.load = mock.Mock(return_value=types.SimpleNamespace(id="1", owner_uid=1000))
        self.m.caller = mock.Mock(return_value=1000)
        self.m.check_android = mock.Mock()
        self.cur = mock.Mock(cfg={"properties": {}})
        for p in (mock.patch.object(main.Instance, "load", return_value=self.cur),
                  mock.patch.object(main.container, "android_of", return_value="13"),
                  mock.patch.object(main.GLib, "idle_add", side_effect=now)):
            p.start()
            self.addCleanup(p.stop)

    def test_waits_for_the_lock_in_a_worker(self):
        done = threading.Event()
        reply = mock.Mock(side_effect=lambda: done.set())
        with self.m.locks["1"]:             # an APK install holding the instance
            self.m.SetConfig("1", {"zoom": "80"}, ":1.2", reply, mock.Mock())
            self.assertFalse(done.wait(0.2))
            self.m.ConfigChanged.assert_not_called()
        self.assertTrue(done.wait(5))
        self.cur.set.assert_called_once_with("zoom", "80")
        self.cur.save.assert_called_once()
        self.m.ConfigChanged.assert_called_once_with("1")

    def test_saves_in_the_order_they_came(self):
        class LastComesFirst:
            """An instance lock whose later waiters win it."""
            lock, n = threading.Lock(), 0

            def __enter__(self):
                self.n += 1
                time.sleep(0.1 / self.n)
                self.lock.acquire()

            def __exit__(self, *a):
                self.lock.release()
        self.m.locks["1"] = LastComesFirst()
        replies = threading.Semaphore(0)
        for z in range(8):
            self.m.SetConfig("1", {"zoom": str(z)}, ":1.2", replies.release, mock.Mock())
        for _ in range(8):
            self.assertTrue(replies.acquire(timeout=5))
        self.assertEqual([c.args[1] for c in self.cur.set.call_args_list], [str(z) for z in range(8)])

    def test_invalid_value_reaches_the_caller(self):
        failed = threading.Event()
        errors = []
        self.cur.set.side_effect = ValueError("bad zoom")
        self.m.SetConfig("1", {"zoom": "x"}, ":1.2", mock.Mock(),
                         lambda e: (errors.append(e), failed.set()))
        self.assertTrue(failed.wait(5))
        self.assertTrue(errors[0].get_dbus_name().endswith(".InvalidArgs"))
        self.cur.save.assert_not_called()
        self.m.ConfigChanged.assert_not_called()


class WatcherTest(unittest.TestCase):
    def setUp(self):
        self.m = manager()
        self.idle = mock.Mock()
        self.up = set()
        for p in (mock.patch.object(main, "list_ids", return_value=["1"]),
                  mock.patch.object(main, "active_ids", lambda: self.up),
                  mock.patch.object(main.GLib, "idle_add", self.idle)):
            p.start()
            self.addCleanup(p.stop)

    def test_survives_a_failing_lxc_info(self):
        with mock.patch.object(main, "lxc_state", side_effect=OSError("fork failed")), \
                mock.patch.object(main, "log") as log:
            self.m._watch_tick(2)
        log.exception.assert_called_once()

    def test_reboot_seen_through_stopping_still_reaches_on_observed(self):
        self.m.known["1"] = "RUNNING"
        self.m.run_async = mock.Mock()
        with mock.patch.object(main, "lxc_state", return_value="STOPPING"):
            self.m._watch_tick(2)
        self.idle.assert_not_called()       # on the way: nothing recorded
        with mock.patch.object(main, "lxc_state", return_value="STOPPED"):
            self.m._watch_tick(3)
        self.idle.assert_called_once_with(self.m._on_observed, "1", "STOPPED")
        self.m._on_observed("1", "STOPPED")
        self.m.run_async.assert_called_once()    # cleanup (no session: not a reboot)

    def test_stopped_instance_without_a_container_is_not_polled(self):
        self.m.known["1"] = "STOPPED"
        with mock.patch.object(main, "lxc_state") as state:
            self.m._watch_tick(2)
        state.assert_not_called()

    def test_running_instance_recorded_as_stopped_is_corrected(self):
        self.m.known["1"] = "STOPPED"           # a failed lxc-info right after its start
        self.up.add("1")
        with mock.patch.object(main, "lxc_state", return_value="RUNNING"):
            self.m._watch_tick(2)
        self.idle.assert_called_once_with(self.m._on_observed, "1", "RUNNING")

    def test_failed_stock_sync_is_retried_only_for_new_images(self):
        with mock.patch.object(main, "lxc_state", return_value="STOPPED"), mock.patch.object(main, "log"), \
                mock.patch.multiple(main.images, stock_image_id=mock.DEFAULT, stock_is_newer=mock.DEFAULT,
                                    stock_busy=mock.DEFAULT, ensure_synced=mock.DEFAULT) as im:
            im["stock_image_id"].return_value = "1-1"
            im["stock_is_newer"].return_value = True
            im["stock_busy"].return_value = False
            im["ensure_synced"].side_effect = RuntimeError("no space left")
            self.m._watch_tick(1)
            self.m._watch_tick(11)
            self.assertEqual(im["ensure_synced"].call_count, 1)
            im["stock_image_id"].return_value = "2-2"
            im["ensure_synced"].side_effect = None
            self.m._watch_tick(21)
            self.assertEqual(im["ensure_synced"].call_count, 2)
            self.assertIsNone(self.m.sync_failed)

    def test_deleted_instance_is_ignored_by_on_observed(self):
        with mock.patch.object(main, "list_ids", return_value=[]):
            self.m._on_observed("1", "STOPPED")
        self.assertNotIn("1", self.m.known)
        self.m.StateChanged.assert_not_called()


class SessionTest(unittest.TestCase):
    def test_dead_session_cancels_its_watch(self):
        m = manager()
        m.bus = mock.Mock()
        m.run_async = mock.Mock()
        m._attach_session("1", 1000, {}, ":1.5")
        watch = m.bus.watch_name_owner.return_value
        m.bus.watch_name_owner.call_args.args[1]("")    # the session's bus name went away
        watch.cancel.assert_called_once()
        self.assertNotIn("1", m.sessions)
        m.run_async.assert_called_once()

    def test_reboot_closes_the_old_keyboard_fifo(self):
        m = manager()
        r, w = os.pipe()
        self.addCleanup(os.close, r)
        m.key_fds["1"] = w
        m.sessions["1"] = {"session": {}, "uid": 1000}
        for name in ("net", "stop_helper", "start_helper", "set_transient", "hosts", "images_in_use"):
            setattr(m, name, mock.Mock())
        with mock.patch.object(main.Instance, "load"), mock.patch.object(main, "container"):
            m._restart("1")
        self.assertNotIn("1", m.key_fds)
        self.assertRaises(OSError, os.fstat, w)

    def test_helper_of_a_stopped_container_is_not_restarted(self):
        m = manager()
        p = mock.Mock(returncode=0)
        m.helpers["1"], m.known["1"] = p, "RUNNING"     # the watcher hasn't seen the stop yet
        m.start_helper = mock.Mock()
        with mock.patch.object(main, "lxc_state", return_value="STOPPED"), mock.patch.object(main, "log"):
            m._helper_exited("1", p, 60)
        m.start_helper.assert_not_called()
        self.assertNotIn("1", m.helpers)

    def test_deleted_instance_gets_no_disk_figure_back(self):
        m = manager()
        inst = types.SimpleNamespace(id="3", index=3, dir="/nonexistent")
        du = threading.Event()

        def run(*a, **kw):
            du.wait(5)
            return types.SimpleNamespace(stdout="123\t/x\n123\ttotal\n")
        with mock.patch.object(main.subprocess, "run", run):
            m.disk_used(inst)
            m.disk.pop("3")                         # _delete, while du runs
            du.set()
            next(t for t in threading.enumerate() if t.name == "du-3").join(5)
        self.assertNotIn("3", m.disk)


class CreateTest(unittest.TestCase):
    def test_leftover_directory_keeps_its_number(self):
        m = manager()
        with tempfile.TemporaryDirectory() as d, mock.patch.object(main.paths, "INSTANCES_DIR", d), \
                mock.patch.object(main, "allocate_index", side_effect=StopIteration) as alloc:
            for n in ("0", "1"):                # 1: a delete that failed half-way, no instance.cfg
                os.mkdir(os.path.join(d, n))
            with self.assertRaises(StopIteration):
                m._create_from(1000, {}, "", None, True)
        self.assertEqual(sorted(alloc.call_args.args[0]), [0, 1])


class RotationTest(unittest.TestCase):
    def test_gone_reader_ends_the_watch_without_more_output(self):
        real = subprocess.Popen
        procs = []

        def fake(cmd, **kw):        # Android's loop: one rotation, then nothing changes
            procs.append(real(["sh", "-c", "echo 1; exec sleep 30"], **kw))
            return procs[-1]
        r, w = os.pipe()
        inst = types.SimpleNamespace(id="1", container="wdm-1")
        with mock.patch.object(container, "lxc_state", return_value="RUNNING"), \
                mock.patch.object(container.subprocess, "Popen", fake):
            t = threading.Thread(target=container.watch_rotation, args=(inst, w), daemon=True)
            t.start()
            self.assertEqual(os.read(r, 16), b"1\n")
            os.close(r)
            t.join(5)
        self.assertFalse(t.is_alive())
        self.assertIsNotNone(procs[0].poll())


if __name__ == "__main__":
    unittest.main()
