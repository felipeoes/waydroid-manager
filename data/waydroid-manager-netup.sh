#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# LXC lxc.net.0.script.up hook for waydroid-manager instances.
# Arguments for veth: $1=container $2=net $3=up $4=veth $5=bridge $6=host-side veth
# Isolate the instance's bridge port so instances cannot reach each other
# (they can still reach the host/gateway and the internet).
[ "$4" = "veth" ] && [ -n "$6" ] && bridge link set dev "$6" isolated on
exit 0
