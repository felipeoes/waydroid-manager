# SPDX-License-Identifier: GPL-3.0-or-later
"""Per-instance binder nodes in the shared /dev/binderfs mount.

Binder contexts belong to each device node, so ``wdm<N>-binder`` etc. give
every instance its own servicemanager without a second binderfs mount.
Nodes are allocated idempotently at each start and never deleted.
"""
import gc
import os

from .. import stock
from ..instance import binder_nodes
from .util import log

BINDERFS = "/dev/binderfs"


def ensure_binderfs(args):
    """Load binder and mount binderfs the way stock does (creating the stock
    anbox-* nodes too), so stock Waydroid never stacks a second binderfs on
    top of ours later."""
    if not all(os.path.exists("/dev/" + n) for n in ("anbox-binder", "anbox-vndbinder", "anbox-hwbinder")) \
            or not os.path.exists(os.path.join(BINDERFS, "binder-control")):
        stock.tools().helpers.drivers.probeBinderDriver(args)
        gc.collect()  # stock's command runner leaks fds until collected
    if not os.path.exists(os.path.join(BINDERFS, "binder-control")):
        raise RuntimeError("binderfs is not available (is the binder_linux module loaded?)")


def ensure_nodes(args, index):
    nodes = list(binder_nodes(index).values())
    missing = [n for n in nodes if not os.path.exists(os.path.join(BINDERFS, n))]
    if missing:
        log.info("allocating binder nodes %s", ", ".join(missing))
        stock.tools().helpers.drivers.allocBinderNodes(args, missing)
    for n in nodes:
        path = os.path.join(BINDERFS, n)
        if not os.path.exists(path):
            raise RuntimeError("binder node {} could not be created".format(path))
        os.chmod(path, 0o666)
    return nodes
