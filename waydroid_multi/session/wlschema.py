# SPDX-License-Identifier: GPL-3.0-or-later
"""Wayland message signatures loaded from the vendored protocol XML files.

The proxy uses them to find every object / new_id argument in a message so
that object ids can be translated between the hwcomposer's id space and the
compositor's (libwayland-server only accepts contiguously allocated client
ids, so proxy-created objects need ids of their own on the compositor side).
"""
import functools
import glob
import os
import xml.etree.ElementTree as ET

PROTO_DIR = os.path.join(os.path.dirname(os.path.realpath(__file__)), "protocols")


class Arg:
    __slots__ = ("type", "interface")

    def __init__(self, type_, interface):
        self.type = type_          # int uint fixed string object new_id array fd
        self.interface = interface  # for object/new_id (None = untyped)


class Interface:
    def __init__(self, name, version):
        self.name = name
        self.version = version
        self.requests = []         # opcode -> [Arg]
        self.events = []
        self.request_names = []
        self.event_names = []


@functools.lru_cache(maxsize=1)
def load(proto_dir=PROTO_DIR):
    ifaces = {}
    for path in sorted(glob.glob(os.path.join(proto_dir, "*.xml"))):
        root = ET.parse(path).getroot()
        for el in root.findall("interface"):
            iface = Interface(el.get("name"), int(el.get("version", "1")))
            for kind, store, names in (("request", iface.requests, iface.request_names),
                                       ("event", iface.events, iface.event_names)):
                for m in el.findall(kind):
                    args = [Arg(a.get("type"), a.get("interface")) for a in m.findall("arg")]
                    store.append(args)
                    names.append(m.get("name"))
            ifaces[iface.name] = iface
    return ifaces


def known(name):
    return name in load()
