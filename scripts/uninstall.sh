#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Uninstall waydroid-multi.  Usage: sudo uninstall.sh [--purge] [PREFIX]
#   --purge      also delete every instance (all their Android data) and the image store
#   --stop-only  only stop everything and remove per-user launchers (the .deb's prerm)
# install.sh copies this script to PREFIX/lib/waydroid-multi/uninstall.sh (the GUI runs
# it from there through pkexec); PREFIX then defaults to the one it was installed to.
# A .deb install is removed through apt, whose prerm calls back into --stop-only.
set -u

PURGE=0
STOP_ONLY=0
while [ $# -gt 0 ]; do
    case "$1" in
        --purge) PURGE=1; shift ;;
        --stop-only) STOP_ONLY=1; shift ;;
        *) break ;;
    esac
done
SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
case "$SELF_DIR" in
    */lib/waydroid-multi) DEFAULT_PREFIX="${SELF_DIR%/lib/waydroid-multi}" ;;
    *) DEFAULT_PREFIX=/usr ;;
esac
PREFIX="${1:-$DEFAULT_PREFIX}"
LIBDIR="$PREFIX/lib/waydroid-multi"
STATE=/var/lib/waydroid-multi
RUNDIR=/run/waydroid-multi

[ "$(id -u)" = 0 ] || { echo "run as root: sudo $0" >&2; exit 1; }

# mounts at or below a directory, deepest first
mounts_under() {
    awk -v d="$1" '$2 == d || index($2, d "/") == 1 {print $2}' /proc/self/mounts | sort -r
}

stop_all() {
    echo "Stopping instances"
    for c in $(lxc-ls -P "$STATE/lxc" --active 2>/dev/null); do
        # through the daemon first: it unmounts and cleans up after the container
        if systemctl -q is-active waydroid-multi.service && [ -x "$PREFIX/bin/waydroid-multi" ]; then
            timeout 60 "$PREFIX/bin/waydroid-multi" stop "${c#wdm-}" >/dev/null 2>&1
        fi
        lxc-unfreeze -P "$STATE/lxc" -n "$c" 2>/dev/null
        lxc-stop -P "$STATE/lxc" -n "$c" -k 2>/dev/null
    done
    systemctl disable --now waydroid-multi.service 2>/dev/null
    systemctl stop waydroid-multi-dnsmasq.service 2>/dev/null
    # the daemon's hardware helpers (KillMode=process leaves them to us) and the per-user
    # instance sessions, which normally exit on their own once their instance stops
    pkill -f -- '-m waydroid_multi\.daemon\.hwhelper ' 2>/dev/null
    for uid in $(loginctl list-users --no-legend 2>/dev/null | awk '{print $1}'); do
        user="$(id -nu "$uid" 2>/dev/null)" || continue
        systemctl --user -M "$user@" stop 'waydroid-multi-session-*.service' 2>/dev/null
    done

    echo "Removing the network"
    if [ -x "$LIBDIR/data/waydroid-multi-net.sh" ]; then
        set -a
        [ -f "$RUNDIR/net.env" ] && . "$RUNDIR/net.env"
        set +a
        sh "$LIBDIR/data/waydroid-multi-net.sh" teardown force
    fi

    # Unmount anything left behind
    for mp in $(mounts_under "$STATE") $(mounts_under "$RUNDIR"); do
        umount "$mp" 2>/dev/null || umount -l "$mp"
    done

    # Instance #0 runs on stock Waydroid's data with stock's container service masked
    # (--runtime) meanwhile: give it back
    if [ "$(systemctl is-enabled waydroid-container.service 2>/dev/null)" = masked-runtime ]; then
        echo "Re-enabling stock Waydroid"
        systemctl unmask --runtime waydroid-container.service
        [ ! -e "$RUNDIR/stock-was-active" ] || systemctl start waydroid-container.service
        rm -f "$RUNDIR/stock-was-active"
    fi
}

# Per-user files, removed as each user (never follow a user's symlinks as root):
# instance launchers would point at a program that is gone; with $1 = 1 also the cache.
remove_user_files() {
    getent passwd | while IFS=: read -r user _ uid _ _ home _; do
        [ "$uid" -ge 1000 ] 2>/dev/null && [ "$uid" -lt 60000 ] && [ -d "$home" ] || continue
        if [ "$1" = 1 ]; then extra="$home/.cache/waydroid-multi"; else extra=""; fi
        runuser -u "$user" -- sh -c '[ ! -d "$1" ] || rm -f "$1"/waydroid-multi.*.desktop
                                      [ -z "$2" ] || rm -rf "$2"' \
            sh "$home/.local/share/applications" "$extra" 2>/dev/null
    done
}

remove_files() {
    echo "Removing program files"
    rm -f /etc/systemd/system/waydroid-multi.service \
          "$PREFIX/lib/systemd/system/waydroid-multi.service" \
          /usr/share/dbus-1/system.d/io.github.waydroidmulti.Manager.conf \
          /etc/dbus-1/system.d/io.github.waydroidmulti.Manager.conf \
          /usr/share/dbus-1/system-services/io.github.waydroidmulti.Manager.service \
          /usr/share/polkit-1/actions/io.github.waydroidmulti.policy \
          /usr/share/applications/io.github.waydroidmulti.desktop \
          "$PREFIX/bin/waydroid-multi" "$PREFIX/bin/waydroid-multi-gui" "$PREFIX/bin/waydroid-multi-daemon"
    rm -rf "$LIBDIR" "$RUNDIR"
    systemctl daemon-reload
    systemctl reload dbus 2>/dev/null || true
}

# rm --one-file-system does not stop at bind mounts of the same filesystem (stock
# Waydroid's data is usually on it): never purge while anything is still mounted
check_unmounted() {
    if [ -n "$(mounts_under "$STATE")" ]; then
        echo "Not uninstalling: still mounted under $STATE:" >&2
        mounts_under "$STATE" >&2
        echo "Reboot, then run this again." >&2
        exit 1
    fi
}

purge_state() {
    rm -rf --one-file-system "$STATE" /etc/waydroid-multi
    rm -f "/var/lib/misc/dnsmasq.${WDM_BRIDGE:-wdmulti0}.leases"
    echo "Removed all instances and the image store."
}

if [ "$STOP_ONLY" = 1 ]; then
    stop_all
    remove_user_files 0
    exit 0
fi

if [ "$(cat "$LIBDIR/install-method" 2>/dev/null)" = deb ]; then
    # Installed as a package: the package manager removes it (prerm stops everything,
    # postrm purges the data)
    if [ "$PURGE" = 1 ]; then action=purge; else action=remove; fi
    echo "Removing the waydroid-multi package ($action)"
    if command -v apt-get >/dev/null; then
        DEBIAN_FRONTEND=noninteractive apt-get "$action" -y waydroid-multi
    elif [ "$PURGE" = 1 ]; then
        dpkg -P waydroid-multi
    else
        dpkg -r waydroid-multi
    fi
    exit $?
fi

stop_all
[ "$PURGE" = 1 ] && check_unmounted
remove_files
remove_user_files "$PURGE"
if [ "$PURGE" = 1 ]; then
    purge_state
else
    echo "Instances and images kept in $STATE (use --purge to delete them)."
fi
echo "Stock Waydroid was not touched."
