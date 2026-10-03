#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Install waydroid-multi system-wide.
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
        *) PREFIX="$1"; shift ;;          # old positional PREFIX
    esac
done
LIBDIR="$PREFIX/lib/waydroid-multi"
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
        echo "waydroid-multi is installed as a package; update it with the .deb, or remove it" >&2
        echo "first (sudo apt remove waydroid-multi keeps your instances)." >&2
        exit 1
    fi
fi

echo "Installing to $D$LIBDIR"
rm -rf "$D$LIBDIR"
mkdir -p "$D$LIBDIR"
cp -r "$SRC/waydroid_multi" "$SRC/data" "$D$LIBDIR/"
find "$D$LIBDIR" -name __pycache__ -prune -exec rm -rf {} +
chmod -R u=rwX,go=rX "$D$LIBDIR"
chmod 755 "$D$LIBDIR"/data/*.sh
install -m755 "$SRC/scripts/uninstall.sh" "$D$LIBDIR/uninstall.sh"
echo "$METHOD" > "$D$LIBDIR/install-method"

mkdir -p "$D$BINDIR"
for tool in waydroid-multi:waydroid_multi waydroid-multi-gui:"waydroid_multi gui" \
            waydroid-multi-daemon:waydroid_multi.daemon.main; do
    name="${tool%%:*}"
    module="${tool#*:}"
    cat > "$D$BINDIR/$name" <<EOF
#!/bin/sh
PYTHONPATH="$LIBDIR\${PYTHONPATH:+:\$PYTHONPATH}" exec python3 -m $module "\$@"
EOF
    chmod 755 "$D$BINDIR/$name"
done

install -Dm644 "$SRC/data/dbus/io.github.waydroidmulti.Manager.conf" \
    "$D/usr/share/dbus-1/system.d/io.github.waydroidmulti.Manager.conf"
install -Dm644 "$SRC/data/dbus/system-services/io.github.waydroidmulti.Manager.service" \
    "$D/usr/share/dbus-1/system-services/io.github.waydroidmulti.Manager.service"
mkdir -p "$D$UNITDIR"
sed "s|@BINDIR@|$BINDIR|g" "$SRC/data/systemd/waydroid-multi.service" > "$D$UNITDIR/waydroid-multi.service"
chmod 644 "$D$UNITDIR/waydroid-multi.service"
install -Dm644 "$SRC/data/applications/io.github.waydroidmulti.desktop" \
    "$D/usr/share/applications/io.github.waydroidmulti.desktop"
# a configuration file: never overwrite the user's copy
if [ -n "$DESTDIR" ] || [ ! -f /etc/waydroid-multi/daemon.conf ]; then
    install -Dm644 "$SRC/data/daemon.conf" "$D/etc/waydroid-multi/daemon.conf"
fi

[ "$ACTIVATE" = 1 ] || exit 0

# Leftovers of older installs: the unit used to live in /etc (which would shadow this
# one), and a hand-installed dev copy of the D-Bus policy
if [ -f /etc/systemd/system/waydroid-multi.service ]; then
    systemctl disable waydroid-multi.service 2>/dev/null || true    # drops its wants/ link
    rm -f /etc/systemd/system/waydroid-multi.service
fi
rm -f /etc/dbus-1/system.d/io.github.waydroidmulti.Manager.conf
systemctl daemon-reload
systemctl reload dbus 2>/dev/null || true
systemctl enable waydroid-multi.service
# (re)start: a restarted daemon adopts running instances
systemctl restart waydroid-multi.service
echo "Done. Open 'Waydroid Multi-Instance Manager' from your app grid, or:"
echo "  waydroid-multi create --name \"Account 1\"    # create an instance (prints its number)"
echo "  waydroid-multi start 1                       # start it and open its window"
echo "Uninstall later with: sudo $LIBDIR/uninstall.sh [--purge]"
