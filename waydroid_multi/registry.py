# SPDX-License-Identifier: GPL-3.0-or-later
"""Instance number allocation.

An instance's number is its id (#1, #2, ...; #0 is stock Waydroid). It also
determines its binder node names, MAC, veth name and fixed IP. New instances
get the lowest free number, so numbers of deleted instances are reused.
"""
from .instance import MAX_INDEX


def allocate_index(used):
    """Return the lowest free number in 1..MAX_INDEX."""
    used = set(used)
    for cand in range(1, MAX_INDEX + 1):
        if cand not in used:
            return cand
    raise RuntimeError("no free instance number (limit is {} instances)".format(MAX_INDEX))
