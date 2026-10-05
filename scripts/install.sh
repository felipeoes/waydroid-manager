#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Install waydroid-manager system-wide.
#   sudo scripts/install.sh [--prefix /usr]          install and (re)start the daemon
#   scripts/install.sh --destdir DIR --no-activate    stage the files only (packaging)
# This script is the single source of the installed file layout: the .deb is built
# from its staged output (packaging/deb/build.sh).
set -eu

PREFIX=/usr
DESTDIR=""
ACTIVATE=1
METHOD=script
while [ $# -gt 0 ]; do
    case "$1" in
        --prefix) PREFIX="$2"; shift 2 ;;
        --destdir) DESTDIR="$2"; shift 2 ;;
        --no-activate) ACTIVATE=0; shift ;;
        --method) METHOD="$2"; shift 2 ;;
        -h|--help) sed -n '3,7p' "$0"; exit 0 ;;
        -*) echo "unknown option: $1" >&2; exit 2 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done
LIBDIR="$PREFIX/lib/waydroid-manager"
BINDIR="$PREFIX/bin"
UNITDIR="$PREFIX/lib/systemd/system"
SRC="$(cd "$(dirname "$0")/.." && pwd)"
D="$DESTDIR"

if [ -z "$DESTDIR" ]; then
    [ "$(id -u)" = 0 ] || { echo "run as root: sudo $0" >&2; exit 1; }
    command -v waydroid >/dev/null || { echo "stock Waydroid must be installed first" >&2; exit 1; }
    for dep in lxc-start dnsmasq ip; do
        command -v "$dep" >/dev/null || { echo "missing dependency: $dep" >&2; exit 1; }
    done
    python3 -c "import dbus, gi, gbinder" 2>/dev/null || {
        echo "missing Python modules (python3-dbus, python3-gi, python3-gbinder)" >&2; exit 1; }
    if command -v dpkg-query >/dev/null && dpkg-query -S "$LIBDIR" >/dev/null 2>&1; then
        echo "waydroid-manager is installed as a package; update it with the .deb, or remove it" >&2
        echo "first (sudo apt remove waydroid-manager keeps your instances)." >&2
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
fi
echo "$METHOD" > "$D$LIBDIR/install-method"

mkdir -p "$D$BINDIR"
for tool in waydroid-manager:waydroid_manager waydroid-manager-gui:"waydroid_manager gui" \
            waydroid-manager-daemon:waydroid_manager.daemon.main; do
    name="${tool%%:*}"
    module="${tool#*:}"
    cat > "$D$BINDIR/$name" <<EOF
#!/bin/sh
PYTHONPATH="$LIBDIR\${PYTHONPATH:+:\$PYTHONPATH}" exec python3 -m $module "\$@"
EOF
    chmod 755 "$D$BINDIR/$name"
done

install -Dm644 "$SRC/data/dbus/io.github.waydroidmanager.Manager.conf" \
    "$D/usr/share/dbus-1/system.d/io.github.waydroidmanager.Manager.conf"
install -Dm644 "$SRC/data/dbus/system-services/io.github.waydroidmanager.Manager.service" \
    "$D/usr/share/dbus-1/system-services/io.github.waydroidmanager.Manager.service"
mkdir -p "$D$UNITDIR"
sed "s|@BINDIR@|$BINDIR|g" "$SRC/data/systemd/waydroid-manager.service" > "$D$UNITDIR/waydroid-manager.service"
chmod 644 "$D$UNITDIR/waydroid-manager.service"
install -Dm644 "$SRC/data/udev/70-waydroid-manager.rules" "$D/usr/lib/udev/rules.d/70-waydroid-manager.rules"
install -Dm644 "$SRC/data/applications/io.github.waydroidmanager.desktop" \
    "$D/usr/share/applications/io.github.waydroidmanager.desktop"
# a configuration file: never overwrite the user's copy
if [ -n "$DESTDIR" ] || [ ! -f /etc/waydroid-manager/daemon.conf ]; then
    install -Dm644 "$SRC/data/daemon.conf" "$D/etc/waydroid-manager/daemon.conf"
fi

[ "$ACTIVATE" = 1 ] || exit 0

"$LIBDIR/migrate-from-waydroid-multi.sh"
systemctl daemon-reload
systemctl reload dbus 2>/dev/null || true
systemctl enable waydroid-manager.service
# (re)start: a restarted daemon adopts running instances
systemctl restart waydroid-manager.service
echo "Done. Open 'Waydroid Manager' from your app grid, or:"
echo "  waydroid-manager create --name \"Account 1\"    # create an instance (prints its number)"
echo "  waydroid-manager start 1                       # start it and open its window"
echo "Uninstall later with: sudo $LIBDIR/uninstall.sh [--purge]"
