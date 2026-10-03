# SPDX-License-Identifier: GPL-3.0-or-later
"""Instance index allocation.

Each instance gets a small integer index that determines its binder node
names, MAC, veth name and fixed IP. Indices are handed out round-robin from a
persistent counter, so a deleted instance's index is only reused after the
counter wraps around (and then only if no live instance holds it).
"""
import configparser
import os

from . import paths
from .instance import MAX_INDEX


def _read(path):
    cfg = configparser.ConfigParser()
    cfg.read(path)
    if "registry" not in cfg:
        cfg["registry"] = {}
    return cfg


def allocate_index(used, path=None):
    """Return a free index in 1..MAX_INDEX and advance the persistent counter."""
    path = path or paths.REGISTRY_FILE
    cfg = _read(path)
    nxt = int(cfg["registry"].get("next_index", "1"))
    used = set(used)
    for step in range(MAX_INDEX):
        cand = (nxt - 1 + step) % MAX_INDEX + 1
        if cand not in used:
            cfg["registry"]["next_index"] = str(cand % MAX_INDEX + 1)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                cfg.write(f)
            os.replace(tmp, path)
            return cand
    raise RuntimeError("no free instance index (limit is {} instances)".format(MAX_INDEX))
