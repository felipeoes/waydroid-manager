#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Install waydroid-manager system-wide.
#   sudo scripts/install.sh [--prefix /usr] [--immutable]   install and (re)start the daemon
#   scripts/install.sh --destdir DIR --no-activate          stage the files only (packaging)
# This script is the single source of the installed file layout: the .deb and .rpm are built
# from its staged output (packaging/deb/build.sh, packaging/rpm/build.sh).
set -eu

PREFIX=/usr
PREFIX_SET=0
DESTDIR=""
ACTIVATE=1
METHOD=script
IMMUTABLE=0
while [ $# -gt 0 ]; do
    case "$1" in
        --prefix) PREFIX="$2"; PREFIX_SET=1; shift 2 ;;
        --destdir) DESTDIR="$2"; shift 2 ;;
        --no-activate) ACTIVATE=0; shift ;;
        --method) METHOD="$2"; shift 2 ;;
        --immutable) IMMUTABLE=1; shift ;;
        -h|--help) sed -n '3,7p' "$0"; exit 0 ;;
        -*) echo "unknown option: $1" >&2; exit 2 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done
while [ "$PREFIX" != "/" ] && [ "${PREFIX%/}" != "$PREFIX" ]; do
    PREFIX="${PREFIX%/}"
done
PREFIX="${PREFIX:-/}"
SRC="$(cd "$(dirname "$0")/.." && pwd)"
D="$DESTDIR"

if [ -z "$DESTDIR" ]; then
    [ "$(id -u)" = 0 ] || { echo "run as root: sudo $0" >&2; exit 1; }
    if [ "$PREFIX_SET" = 0 ] && [ ! -w /usr ]; then
        PREFIX=/usr/local
        IMMUTABLE=1
        echo "Read-only /usr detected; installing under /usr/local and /etc"
    fi
fi
if [ "$IMMUTABLE" = 1 ] && [ "$PREFIX_SET" = 0 ]; then
    PREFIX=/usr/local
fi
if [ -z "$DESTDIR" ]; then
    check_dir="$PREFIX"
    while [ ! -e "$check_dir" ] && [ "$check_dir" != "/" ]; do
        check_dir="$(dirname "$check_dir")"
    done
    if [ ! -w "$check_dir" ]; then
        echo "install prefix $PREFIX is not writable (pass --prefix DIR with a writable prefix)" >&2
        exit 1
    fi
fi

LIBDIR="$PREFIX/lib/waydroid-manager"
BINDIR="$PREFIX/bin"
if [ "$IMMUTABLE" = 1 ] || { [ -z "$DESTDIR" ] && { [ "$PREFIX" != /usr ] || [ ! -w /usr ]; }; }; then
    UNITDIR=/etc/systemd/system
    DBUS_CONF_DIR=/etc/dbus-1/system.d
    # dbus-daemon never searches /etc/dbus-1/system-services; /usr/local/share/dbus-1/system-services
    # is writable on immutable distros and searched by both dbus-daemon and dbus-broker
    DBUS_SVC_DIR=/usr/local/share/dbus-1/system-services
    UDEV_DIR=/etc/udev/rules.d
    DESKTOP_DIR="$PREFIX/share/applications"
else
    UNITDIR="$PREFIX/lib/systemd/system"
    DBUS_CONF_DIR=/usr/share/dbus-1/system.d
    DBUS_SVC_DIR=/usr/share/dbus-1/system-services
    UDEV_DIR=/usr/lib/udev/rules.d
    DESKTOP_DIR=/usr/share/applications
fi

# What starts the daemon: systemd, OpenRC or runit at boot; any other init, D-Bus on the first
# call. Only one of them may: a D-Bus-started daemon would make the init's own exit and respawn.
if [ -n "$DESTDIR" ]; then
    INIT=package    # a systemd unit, which systemd prefers to the D-Bus Exec others use
elif [ -d /run/systemd/system ]; then
    INIT=systemd
elif [ -d /run/openrc ]; then
    INIT=openrc
elif [ -d /run/runit ]; then
    INIT=runit
else
    INIT=other
fi
case "$INIT" in
    systemd|openrc|runit) DBUS_EXEC=/bin/false ;;
    *) DBUS_EXEC="$BINDIR/waydroid-manager-daemon" ;;
esac
if [ -d /etc/runit/sv ]; then
    SV_DIR=/etc/runit/sv; SV_LINKS=/run/runit/service       # Artix
else
    SV_DIR=/etc/sv; SV_LINKS=/var/service                   # Void
fi

if [ -z "$DESTDIR" ]; then
    command -v waydroid >/dev/null || { echo "stock Waydroid must be installed first" >&2; exit 1; }
    for dep in lxc-start dnsmasq ip openssl; do
        command -v "$dep" >/dev/null || { echo "missing dependency: $dep" >&2; exit 1; }
    done
    python3 -c "import dbus, gi, gbinder" 2>/dev/null || {
        echo "missing Python modules (python3-dbus, python3-gi / python3-gobject, python3-gbinder)" >&2; exit 1; }
    if command -v dpkg-query >/dev/null && { dpkg-query -S "$LIBDIR" >/dev/null 2>&1 || dpkg-query -S /usr/lib/waydroid-manager >/dev/null 2>&1; }; then
        echo "waydroid-manager is installed as a package; update it with the .deb, or remove it" >&2
        echo "first (sudo apt remove waydroid-manager keeps your instances)." >&2
        exit 1
    fi
    if command -v rpm >/dev/null && { rpm -qf "$LIBDIR" >/dev/null 2>&1 || rpm -qf /usr/lib/waydroid-manager >/dev/null 2>&1; }; then
        echo "waydroid-manager is installed as an RPM package; update it with the .rpm, or remove it" >&2
        echo "first (sudo dnf remove waydroid-manager or rpm-ostree uninstall waydroid-manager)." >&2
        exit 1
    fi
    if [ "$ACTIVATE" = 1 ] && [ "$(cat /sys/fs/selinux/enforce 2>/dev/null || true)" = 1 ] && \
       ! command -v semodule >/dev/null 2>&1; then
        echo "SELinux is enforcing, but semodule is missing (install policycoreutils)" >&2
        exit 1
    fi
fi

echo "Installing to $D$LIBDIR"
rm -rf "$D$LIBDIR"
mkdir -p "$D$LIBDIR"
cp -r "$SRC/waydroid_manager" "$SRC/data" "$D$LIBDIR/"
find "$D$LIBDIR" -name __pycache__ -prune -exec rm -rf {} +
chmod -R u=rwX,go=rX "$D$LIBDIR"
chmod 755 "$D$LIBDIR"/data/*.sh
install -m755 "$SRC/scripts/uninstall.sh" "$D$LIBDIR/uninstall.sh"
install -m755 "$SRC/scripts/migrate-from-waydroid-multi.sh" "$D$LIBDIR/migrate-from-waydroid-multi.sh"
# libgbinder with the protocols Android 15+ need (packaging/build-libgbinder.sh); without
# it, the system's is used and those versions can't install apps or share the clipboard
if [ -f "$SRC/build/lib/libgbinder.so.1" ]; then
    install -Dm644 "$SRC/build/lib/libgbinder.so.1" "$D$LIBDIR/lib/libgbinder.so.1"
    install -Dm644 "$SRC/build/lib/libgbinder.LICENSE" "$D$LIBDIR/lib/libgbinder.LICENSE"
fi
echo "$METHOD" > "$D$LIBDIR/install-method"
if [ "$METHOD" = script ]; then
    cat > "$D$LIBDIR/install-paths" <<EOF
UNITDIR=$UNITDIR
DBUS_CONF_DIR=$DBUS_CONF_DIR
DBUS_SVC_DIR=$DBUS_SVC_DIR
UDEV_DIR=$UDEV_DIR
DESKTOP_DIR=$DESKTOP_DIR
EOF
    chmod 644 "$D$LIBDIR/install-paths"
fi

mkdir -p "$D$BINDIR"
for tool in waydroid-manager:waydroid_manager waydroid-manager-gui:"waydroid_manager gui" \
            waydroid-manager-daemon:waydroid_manager.daemon.main; do
    name="${tool%%:*}"
    module="${tool#*:}"
    # D-Bus starts the daemon with an empty environment (no init does it): without PATH,
    # Python has no sys.executable to start its helpers with
    cat > "$D$BINDIR/$name" <<EOF
#!/bin/sh
export PATH="\${PATH:-/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin}"
PYTHONPATH="$LIBDIR\${PYTHONPATH:+:\$PYTHONPATH}" exec python3 -m $module "\$@"
EOF
    chmod 755 "$D$BINDIR/$name"
done

install -Dm644 "$SRC/data/dbus/io.github.waydroidmanager.Manager.conf" \
    "$D$DBUS_CONF_DIR/io.github.waydroidmanager.Manager.conf"
mkdir -p "$D$DBUS_SVC_DIR"
sed "s|@DBUS_EXEC@|$DBUS_EXEC|" "$SRC/data/dbus/system-services/io.github.waydroidmanager.Manager.service" \
    > "$D$DBUS_SVC_DIR/io.github.waydroidmanager.Manager.service"
chmod 644 "$D$DBUS_SVC_DIR/io.github.waydroidmanager.Manager.service"
mkdir -p "$D$UNITDIR"
sed "s|@BINDIR@|$BINDIR|g" "$SRC/data/systemd/waydroid-manager.service" > "$D$UNITDIR/waydroid-manager.service"
chmod 644 "$D$UNITDIR/waydroid-manager.service"
if [ "$INIT" = openrc ]; then
    sed "s|@BINDIR@|$BINDIR|g" "$SRC/data/openrc/waydroid-manager" > /etc/init.d/waydroid-manager
    chmod 755 /etc/init.d/waydroid-manager
elif [ "$INIT" = runit ]; then
    mkdir -p "$SV_DIR/waydroid-manager"
    sed "s|@BINDIR@|$BINDIR|g" "$SRC/data/runit/waydroid-manager/run" > "$SV_DIR/waydroid-manager/run"
    chmod 755 "$SV_DIR/waydroid-manager/run"
fi
install -Dm644 "$SRC/data/udev/70-waydroid-manager.rules" "$D$UDEV_DIR/70-waydroid-manager.rules"
mkdir -p "$D$DESKTOP_DIR"
if [ "$BINDIR" != /usr/bin ]; then
    sed "s|^Exec=waydroid-manager-gui$|Exec=$BINDIR/waydroid-manager-gui|" \
        "$SRC/data/applications/io.github.waydroidmanager.desktop" \
        > "$D$DESKTOP_DIR/io.github.waydroidmanager.desktop"
else
    cp "$SRC/data/applications/io.github.waydroidmanager.desktop" \
        "$D$DESKTOP_DIR/io.github.waydroidmanager.desktop"
fi
chmod 644 "$D$DESKTOP_DIR/io.github.waydroidmanager.desktop"
# a configuration file: never overwrite the user's copy
if [ -n "$DESTDIR" ] || [ ! -f /etc/waydroid-manager/daemon.conf ]; then
    install -Dm644 "$SRC/data/daemon.conf" "$D/etc/waydroid-manager/daemon.conf"
fi

[ "$ACTIVATE" = 1 ] || exit 0

"$LIBDIR/migrate-from-waydroid-multi.sh"
# Restore SELinux file contexts whenever SELinux is enabled (enforcing or permissive)
if command -v restorecon >/dev/null 2>&1 && [ -e /sys/fs/selinux/enforce ]; then
    restorecon -RF "$LIBDIR" \
        "$BINDIR/waydroid-manager" "$BINDIR/waydroid-manager-gui" "$BINDIR/waydroid-manager-daemon" \
        "$UNITDIR/waydroid-manager.service" \
        "$DBUS_CONF_DIR/io.github.waydroidmanager.Manager.conf" \
        "$DBUS_SVC_DIR/io.github.waydroidmanager.Manager.service" \
        "$UDEV_DIR/70-waydroid-manager.rules" \
        "$DESKTOP_DIR/io.github.waydroidmanager.desktop" \
        /etc/waydroid-manager 2>/dev/null || true
fi
# Install the SELinux policy module for Android 14-17 APEX binder calls whenever SELinux is enabled
if [ -e /sys/fs/selinux/enforce ]; then
    SELINUX_ENFORCING="$(cat /sys/fs/selinux/enforce 2>/dev/null || true)"
    if command -v semodule >/dev/null 2>&1; then
        if ! semodule -X 400 -i "$LIBDIR/data/selinux/waydroid-manager.cil"; then
            if [ "$SELINUX_ENFORCING" = 1 ]; then
                echo "failed to install SELinux policy module ($LIBDIR/data/selinux/waydroid-manager.cil)" >&2
                exit 1
            fi
            echo "warning: could not install SELinux policy module; Android 14-17 need it under SELinux enforcing" >&2
        fi
    elif [ "$SELINUX_ENFORCING" = 1 ]; then
        echo "SELinux is enforcing, but semodule is missing (install policycoreutils)" >&2
        exit 1
    else
        echo "warning: semodule not found; SELinux policy module was not installed" >&2
    fi
fi
if command -v udevadm >/dev/null 2>&1; then
    udevadm control --reload 2>/dev/null || true
fi
# (re)start: a restarted daemon adopts running instances
if [ "$INIT" = systemd ]; then
    systemctl daemon-reload
    systemctl reload dbus 2>/dev/null || true
    systemctl enable waydroid-manager.service
    systemctl restart waydroid-manager.service
else
    dbus-send --system --type=method_call --dest=org.freedesktop.DBus / org.freedesktop.DBus.ReloadConfig
    case "$INIT" in
        openrc)
            rc-update add waydroid-manager default >/dev/null
            rc-service waydroid-manager restart ;;
        runit)
            if [ -e "$SV_LINKS/waydroid-manager" ]; then
                sv restart "$SV_LINKS/waydroid-manager"
            else
                ln -s "$SV_DIR/waydroid-manager" "$SV_LINKS/"      # runsvdir starts it within 5 s
            fi ;;
        *)
            # D-Bus starts the new one; the old one stops on SIGTERM
            if pkill -f -- '-m waydroid_manager\.daemon\.main'; then
                for _ in 1 2 3 4 5 6 7 8 9 10; do
                    pgrep -f -- '-m waydroid_manager\.daemon\.main' >/dev/null || break
                    sleep 1
                done
            fi
            dbus-send --system --type=method_call --print-reply=literal --dest=org.freedesktop.DBus / \
                org.freedesktop.DBus.StartServiceByName string:io.github.waydroidmanager.Manager uint32:0 \
                >/dev/null ;;
    esac
    # OpenRC and runit return before it's on the bus (systemd's Type=dbus waits): don't let the
    # first command after this fail
    for _ in $(seq 1 20); do
        dbus-send --system --print-reply --dest=org.freedesktop.DBus / org.freedesktop.DBus.NameHasOwner \
            string:io.github.waydroidmanager.Manager 2>/dev/null | grep -q true && break
        sleep 0.5
    done
fi
echo "Done. Open 'Waydroid Manager' from your app grid, or:"
echo "  waydroid-manager create --name \"Account 1\"    # create an instance (prints its number)"
echo "  waydroid-manager start 1                       # start it and open its window"
echo "Uninstall later with: sudo $LIBDIR/uninstall.sh [--purge]"
