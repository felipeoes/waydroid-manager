#!/bin/sh
# LXC lxc.net.0.script.up hook. Isolate the instance's bridge port so
# instances cannot reach each other.
{ echo "args: $*"; env | grep '^LXC_' ; } >> /run/waydroid-multi/netup-debug.log 2>&1
dev="$6"
[ -n "$dev" ] && bridge link set dev "$dev" isolated on
exit 0
