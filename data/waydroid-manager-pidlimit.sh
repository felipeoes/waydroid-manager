#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# LXC start-host hook, enabled only where pid_max is namespaced (Linux 6.14+).
set -eu
# Keep the host's writable proc mount; Android's /proc/sys is already read-only.
exec nsenter --target "${LXC_PID:?}" --pid -- /bin/sh -c 'echo 65535 > /proc/sys/kernel/pid_max'
