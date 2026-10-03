#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Spike: register IHardware for several instances in ONE process using only
async gbinder calls, and log every transaction Android sends.

  sudo spike_hw.py spike1 spike2
"""
import configparser
import datetime
import sys

import gbinder
from gi.repository import GLib

INTERFACE = "lineageos.waydroid.IHardware"
SERVICE_NAME = "waydroidhardware"
CODES = {1: "enableNFC", 2: "enableBluetooth", 3: "suspend", 4: "reboot", 5: "upgrade", 6: "upgrade2"}


def log(*a):
    print(datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3], *a, flush=True)


class HwService:
    def __init__(self, name):
        cfg = configparser.ConfigParser()
        cfg.read("/var/lib/waydroid-multi/instances/{}/instance.cfg".format(name))
        w = cfg["waydroid"]
        self.name = name
        self.sm = gbinder.ServiceManager("/dev/" + w["binder"], w["service_manager_protocol"], w["binder_protocol"])
        self.obj = self.sm.new_local_object(INTERFACE, self.handle)
        self.hid = self.sm.add_presence_handler(self.presence)
        self.presence()

    def presence(self):
        log(self.name, "servicemanager present:", self.sm.is_present())
        if self.sm.is_present():
            log(self.name, "add_service_sync status", self.sm.add_service_sync(SERVICE_NAME, self.obj))

    def handle(self, req, code, flags):
        log(self.name, "transaction", code, CODES.get(code, "?"), "flags", flags)
        reply = self.obj.new_reply()
        reply.append_int32(0)
        if code in (1, 2):
            reply.append_int32(0)
        return reply, 0


services = [HwService(n) for n in sys.argv[1:]]
GLib.timeout_add_seconds(5, lambda: (log("main loop alive"), True)[1])
GLib.MainLoop().run()
