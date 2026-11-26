#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Uninstall waydroid-manager.  Usage: sudo uninstall.sh [--purge] [PREFIX]
#   --purge      also delete every instance (all their Android data) and the image store
#   --stop-only  only stop everything and remove per-user launchers (package pre-remove)
# install.sh copies this script to PREFIX/lib/waydroid-manager/uninstall.sh (the GUI runs
# it from there through pkexec); PREFIX then defaults to the one it was installed to.
# A .deb or .rpm install is removed through the package manager, whose pre-remove calls
# back into --stop-only (on rpm-ostree, this script stops everything before rpm-ostree uninstall).
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
    */lib/waydroid-manager) DEFAULT_PREFIX="${SELF_DIR%/lib/waydroid-manager}" ;;
    *)
        if [ ! -d /usr/lib/waydroid-manager ] && [ -d /usr/local/lib/waydroid-manager ]; then
            DEFAULT_PREFIX=/usr/local
        else
            DEFAULT_PREFIX=/usr
        fi
        ;;
esac
PREFIX="${1:-$DEFAULT_PREFIX}"
LIBDIR="$PREFIX/lib/waydroid-manager"
STATE=/var/lib/waydroid-manager
RUNDIR=/run/waydroid-manager

[ "$(id -u)" = 0 ] || { echo "run as root: sudo $0" >&2; exit 1; }
if [ -d /etc/runit/sv ]; then
    SV_DIR=/etc/runit/sv; SV_LINKS=/run/runit/service       # Artix
else
    SV_DIR=/etc/sv; SV_LINKS=/var/service                   # Void
fi
DAEMON_RE='-m waydroid_manager\.daemon\.main'

# mounts at or below a directory, deepest first
mounts_under() {
    awk -v d="$1" '$2 == d || index($2, d "/") == 1 {print $2}' /proc/self/mounts | sort -r
}

stop_all() {
    echo "Stopping instances"
    for c in $(lxc-ls -P "$STATE/lxc" --active 2>/dev/null); do
        # through the daemon first: it unmounts and cleans up after the container
        if pgrep -f -- "$DAEMON_RE" >/dev/null && [ -x "$PREFIX/bin/waydroid-manager" ]; then
            timeout 60 "$PREFIX/bin/waydroid-manager" stop "${c#wdm-}" >/dev/null 2>&1
        fi
        lxc-unfreeze -P "$STATE/lxc" -n "$c" 2>/dev/null
        lxc-stop -P "$STATE/lxc" -n "$c" -k 2>/dev/null
    done
    systemctl disable --now waydroid-manager.service 2>/dev/null
    if [ -f /etc/init.d/waydroid-manager ]; then
        rc-service waydroid-manager stop >/dev/null 2>&1
        rc-update del waydroid-manager >/dev/null 2>&1
    fi
    if [ -e "$SV_LINKS/waydroid-manager" ]; then
        sv stop "$SV_LINKS/waydroid-manager" >/dev/null 2>&1
        rm -f "$SV_LINKS/waydroid-manager"
    fi
    pkill -f -- "$DAEMON_RE" 2>/dev/null    # D-Bus started it (no systemd, OpenRC or runit)
    systemctl stop waydroid-manager-dnsmasq.service 2>/dev/null
    pid="$(cat "$RUNDIR/dnsmasq.pid" 2>/dev/null)" && [ "$(cat "/proc/$pid/comm" 2>/dev/null)" = dnsmasq ] &&
        kill "$pid"     # without systemd it runs on its own
    # the daemon's hardware helpers (KillMode=process leaves them to us) and the per-user
    # instance sessions, which normally exit on their own once their instance stops
    pkill -f -- '-m waydroid_manager\.daemon\.hwhelper ' 2>/dev/null
    for uid in $(loginctl list-users --no-legend 2>/dev/null | awk '{print $1}'); do
        user="$(id -nu "$uid" 2>/dev/null)" || continue
        systemctl --user -M "$user@" stop 'waydroid-manager-session-*.service' 2>/dev/null
    done
    pkill -f -- '-m waydroid_manager session ' 2>/dev/null     # without systemd --user

    echo "Removing the network"
    if [ -x "$LIBDIR/data/waydroid-manager-net.sh" ]; then
        set -a
        [ -f "$RUNDIR/net.env" ] && . "$RUNDIR/net.env"
        set +a
        sh "$LIBDIR/data/waydroid-manager-net.sh" teardown force
    fi

    # Unmount anything left behind
    for mp in $(mounts_under "$STATE") $(mounts_under "$RUNDIR"); do
        umount "$mp" 2>/dev/null || umount -l "$mp"
    done

    # the hidden device software rendering used
    if [ -d /sys/kernel/config/vkms/waydroid-manager ]; then
        echo 0 > /sys/kernel/config/vkms/waydroid-manager/enabled
        rm -f /sys/kernel/config/vkms/waydroid-manager/*/*/possible_*/*
        rmdir /sys/kernel/config/vkms/waydroid-manager/*/* /sys/kernel/config/vkms/waydroid-manager
    fi

    # Instance #0 runs on stock Waydroid's data with stock's container service masked
    # (--runtime) meanwhile: give it back
    if [ "$(systemctl is-enabled waydroid-container.service 2>/dev/null)" = masked-runtime ]; then
        echo "Re-enabling stock Waydroid"
        systemctl unmask --runtime waydroid-container.service
        [ ! -e "$RUNDIR/stock-was-active" ] || systemctl start waydroid-container.service
        rm -f "$RUNDIR/stock-was-active"
    elif [ ! -d /run/systemd/system ] && [ -e "$RUNDIR/stock-was-active" ]; then
        # without systemd the daemon only held its bus name, which went with it
        echo "Restarting stock Waydroid"
        dbus-send --system --type=method_call --dest=org.freedesktop.DBus / \
            org.freedesktop.DBus.StartServiceByName string:id.waydro.Container uint32:0
        rm -f "$RUNDIR/stock-was-active"
    fi
}

# Per-user files, removed as each user (never follow a user's symlinks as root):
# instance launchers would point at a program that is gone; with $1 = 1 also the cache.
remove_user_files() {
    getent passwd | while IFS=: read -r user _ uid _ _ home _; do
        [ "$uid" -ge 1000 ] 2>/dev/null && [ "$uid" -lt 60000 ] && [ -d "$home" ] || continue
        if [ "$1" = 1 ]; then extra="$home/.cache/waydroid-manager"; else extra=""; fi
        runuser -u "$user" -- sh -c '[ ! -d "$1" ] || rm -f "$1"/waydroid-manager.*.desktop
                                      ! grep -qx X-WaydroidManager=true "$1"/Waydroid.desktop || rm -f "$1"/Waydroid.desktop
                                      [ -z "$2" ] || rm -rf "$2"' \
            sh "$home/.local/share/applications" "$extra" 2>/dev/null
    done
}

remove_files() {
    echo "Removing program files"
    UNITDIR="$PREFIX/lib/systemd/system"
    DBUS_CONF_DIR=/usr/share/dbus-1/system.d
    DBUS_SVC_DIR=/usr/share/dbus-1/system-services
    UDEV_DIR=/usr/lib/udev/rules.d
    DESKTOP_DIR=/usr/share/applications
    if [ -f "$LIBDIR/install-paths" ]; then
        while IFS='=' read -r key val || [ -n "$key" ]; do
            case "$val" in
                /*) ;;
                *) continue ;;
            esac
            case "$key" in
                UNITDIR) UNITDIR="$val" ;;
                DBUS_CONF_DIR) DBUS_CONF_DIR="$val" ;;
                DBUS_SVC_DIR) DBUS_SVC_DIR="$val" ;;
                UDEV_DIR) UDEV_DIR="$val" ;;
                DESKTOP_DIR) DESKTOP_DIR="$val" ;;
            esac
        done < "$LIBDIR/install-paths"
    fi
    rm -rf "$SV_DIR/waydroid-manager"
    rm -f "$UNITDIR/waydroid-manager.service" /etc/init.d/waydroid-manager \
          "$DBUS_CONF_DIR/io.github.waydroidmanager.Manager.conf" \
          "$DBUS_SVC_DIR/io.github.waydroidmanager.Manager.service" \
          "$DESKTOP_DIR/io.github.waydroidmanager.desktop" \
          "$UDEV_DIR/70-waydroid-manager.rules" \
          "$PREFIX/bin/waydroid-manager" "$PREFIX/bin/waydroid-manager-gui" "$PREFIX/bin/waydroid-manager-daemon"
    rm -rf "$LIBDIR" "$RUNDIR"
    # the instance names adb uses (only our lines, even if END was deleted)
    sed -i --follow-symlinks '/^# BEGIN waydroid-manager /,/^# END waydroid-manager$/{/^# BEGIN waydroid-manager \|^# END waydroid-manager$\|^[0-9a-fA-F.:]\+ waydroid-[a-z0-9-]\+$/d}' /etc/hosts 2>/dev/null || true
    if [ -d /run/systemd/system ]; then
        systemctl daemon-reload
        systemctl reload dbus 2>/dev/null || true
    else
        dbus-send --system --type=method_call --dest=org.freedesktop.DBus / org.freedesktop.DBus.ReloadConfig
    fi
    if command -v udevadm >/dev/null 2>&1; then
        udevadm control --reload 2>/dev/null || true
    fi
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
    rm -rf --one-file-system "$STATE" /etc/waydroid-manager
    rm -f "/var/lib/misc/dnsmasq.${WDM_BRIDGE:-wdm0}.leases"
    echo "Removed all instances and the image store."
}

if [ "$STOP_ONLY" = 1 ]; then
    stop_all
    remove_user_files 0
    exit 0
fi

METHOD="$(cat "$LIBDIR/install-method" 2>/dev/null || true)"
if [ "$METHOD" = deb ]; then
    # Installed as a package: the package manager removes it (prerm stops everything,
    # postrm purges the data)
    if [ "$PURGE" = 1 ]; then action=purge; else action=remove; fi
    echo "Removing the waydroid-manager package ($action)"
    if command -v apt-get >/dev/null; then
        DEBIAN_FRONTEND=noninteractive apt-get "$action" -y waydroid-manager
    elif [ "$PURGE" = 1 ]; then
        dpkg -P waydroid-manager
    else
        dpkg -r waydroid-manager
    fi
    exit $?
fi

if [ "$METHOD" = rpm ]; then
    if [ -e /run/ostree-booted ] && command -v rpm-ostree >/dev/null 2>&1; then
        # rpm-ostree uninstall runs scriptlets offline in the new deployment; only delete
        # live state and user data after the transaction succeeds so a failed uninstall
        # never wipes instances or launchers.
        if [ "$PURGE" = 1 ]; then
            stop_all
            check_unmounted
        fi
        echo "Removing the waydroid-manager package (rpm-ostree)"
        rpm-ostree uninstall waydroid-manager
        rc=$?
        if [ "$rc" -eq 0 ]; then
            [ "$PURGE" = 1 ] || stop_all
            rm -rf "$RUNDIR"
            sed -i --follow-symlinks '/^# BEGIN waydroid-manager /,/^# END waydroid-manager$/{/^# BEGIN waydroid-manager \|^# END waydroid-manager$\|^[0-9a-fA-F.:]\+ waydroid-[a-z0-9-]\+$/d}' /etc/hosts 2>/dev/null || true
            remove_user_files "$PURGE"
            if [ "$PURGE" = 1 ]; then
                purge_state
            else
                echo "Instances and images kept in $STATE (use --purge to delete them)."
            fi
            echo "Reboot to complete removing waydroid-manager."
            echo "Stock Waydroid was not touched."
        fi
        exit "$rc"
    fi
    echo "Removing the waydroid-manager package"
    if command -v dnf >/dev/null 2>&1; then
        dnf remove -y waydroid-manager
    else
        rpm -e waydroid-manager
    fi
    rc=$?
    if [ "$rc" = 0 ]; then
        if [ "$PURGE" = 1 ]; then
            check_unmounted
            remove_user_files 1
            purge_state
        else
            echo "Instances and images kept in $STATE (use --purge to delete them)."
        fi
        echo "Stock Waydroid was not touched."
    fi
    exit "$rc"
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
