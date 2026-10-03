#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Uninstall waydroid-multi.  Usage: sudo scripts/uninstall.sh [--purge] [PREFIX]
#   --purge  also delete every instance (all their Android data) and the image store
set -u

PURGE=0
if [ "${1:-}" = "--purge" ]; then PURGE=1; shift; fi
PREFIX="${1:-/usr}"
LIBDIR="$PREFIX/lib/waydroid-multi"
STATE=/var/lib/waydroid-multi

[ "$(id -u)" = 0 ] || { echo "run as root: sudo $0" >&2; exit 1; }

# Stop every instance, the daemon and the network
for c in $(lxc-ls -P "$STATE/lxc" 2>/dev/null); do
    lxc-stop -P "$STATE/lxc" -n "$c" -k 2>/dev/null
done
systemctl disable --now waydroid-multi.service 2>/dev/null
systemctl stop waydroid-multi-dnsmasq.service 2>/dev/null
if [ -x "$LIBDIR/data/waydroid-multi-net.sh" ]; then
    [ -f /run/waydroid-multi/net.env ] && . /run/waydroid-multi/net.env
    sh "$LIBDIR/data/waydroid-multi-net.sh" teardown force
fi
# Unmount anything left behind
for mp in $(awk '{print $2}' /proc/mounts | grep -E "^($STATE|/run/waydroid-multi)" | sort -r); do
    umount "$mp" 2>/dev/null || umount -l "$mp"
done

rm -f /etc/systemd/system/waydroid-multi.service \
      /usr/share/dbus-1/system.d/io.github.waydroidmulti.Manager.conf \
      /etc/dbus-1/system.d/io.github.waydroidmulti.Manager.conf \
      /usr/share/dbus-1/system-services/io.github.waydroidmulti.Manager.service \
      /usr/share/polkit-1/actions/io.github.waydroidmulti.policy \
      /usr/share/applications/io.github.waydroidmulti.desktop \
      "$PREFIX/bin/waydroid-multi" "$PREFIX/bin/waydroid-multi-gui" "$PREFIX/bin/waydroid-multi-daemon"
rm -rf "$LIBDIR" /run/waydroid-multi
systemctl daemon-reload
systemctl reload dbus 2>/dev/null || true

if [ "$PURGE" = 1 ]; then
    rm -rf --one-file-system "$STATE" /etc/waydroid-multi
    echo "Removed all instances and the image store."
    echo "Per-user launchers remain in ~/.local/share/applications/waydroid-multi.*.desktop"
else
    echo "Instances and images kept in $STATE (use --purge to delete them)."
fi
echo "Stock Waydroid was not touched."
