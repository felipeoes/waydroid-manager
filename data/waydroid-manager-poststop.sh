#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# LXC lxc.hook.post-stop for waydroid-manager instances: only an explicit stop
# succeeds, so LXC never restarts a container by itself after an in-container
# reboot (the daemon handles reboots so mounts/props stay consistent).
[ "$LXC_TARGET" = "stop" ] && exit 0
exit 1
