#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Install waydroid-multi system-wide.  Usage: sudo scripts/install.sh [PREFIX]
set -eu

PREFIX="${1:-/usr}"
LIBDIR="$PREFIX/lib/waydroid-multi"
BINDIR="$PREFIX/bin"
SRC="$(cd "$(dirname "$0")/.." && pwd)"

[ "$(id -u)" = 0 ] || { echo "run as root: sudo $0" >&2; exit 1; }
command -v waydroid >/dev/null || { echo "stock Waydroid must be installed first" >&2; exit 1; }
for dep in lxc-start dnsmasq ip; do
    command -v "$dep" >/dev/null || { echo "missing dependency: $dep" >&2; exit 1; }
done
python3 -c "import dbus, gi, gbinder" 2>/dev/null || {
    echo "missing Python modules (python3-dbus, python3-gi, python3-gbinder)" >&2; exit 1; }

echo "Installing to $LIBDIR"
rm -rf "$LIBDIR"
mkdir -p "$LIBDIR"
cp -r "$SRC/waydroid_multi" "$SRC/data" "$LIBDIR/"
find "$LIBDIR" -name __pycache__ -prune -exec rm -rf {} +
chmod 755 "$LIBDIR"/data/*.sh

mkdir -p "$BINDIR"
cat > "$BINDIR/waydroid-multi" <<EOF
#!/bin/sh
PYTHONPATH="$LIBDIR\${PYTHONPATH:+:\$PYTHONPATH}" exec python3 -m waydroid_multi "\$@"
EOF
cat > "$BINDIR/waydroid-multi-gui" <<EOF
#!/bin/sh
PYTHONPATH="$LIBDIR\${PYTHONPATH:+:\$PYTHONPATH}" exec python3 -m waydroid_multi gui "\$@"
EOF
cat > "$BINDIR/waydroid-multi-daemon" <<EOF
#!/bin/sh
PYTHONPATH="$LIBDIR\${PYTHONPATH:+:\$PYTHONPATH}" exec python3 -m waydroid_multi.daemon.main "\$@"
EOF
chmod 755 "$BINDIR/waydroid-multi" "$BINDIR/waydroid-multi-gui" "$BINDIR/waydroid-multi-daemon"

install -Dm644 "$SRC/data/dbus/io.github.waydroidmulti.Manager.conf" /usr/share/dbus-1/system.d/io.github.waydroidmulti.Manager.conf
install -Dm644 "$SRC/data/dbus/system-services/io.github.waydroidmulti.Manager.service" \
    /usr/share/dbus-1/system-services/io.github.waydroidmulti.Manager.service
sed "s|@BINDIR@|$BINDIR|g" "$SRC/data/systemd/waydroid-multi.service" > /etc/systemd/system/waydroid-multi.service
install -Dm644 "$SRC/data/applications/io.github.waydroidmulti.desktop" /usr/share/applications/io.github.waydroidmulti.desktop
if [ ! -f /etc/waydroid-multi/daemon.conf ]; then
    install -d /etc/waydroid-multi
    cat > /etc/waydroid-multi/daemon.conf <<'EOF'
# waydroid-multi daemon configuration
[network]
# Shared bridge for all waydroid-multi instances (stock Waydroid uses waydroid0)
bridge = wdmulti0
# Must not overlap any network you use; instance N gets .(10+N)
subnet = 192.168.241.0/24
# Prevent instances from reaching each other
isolate_instances = true
# Use an nftables table ("waydroid_multi") instead of iptables rules
use_nft = false
EOF
fi
# Drop a dev copy of the policy if one was installed by hand
rm -f /etc/dbus-1/system.d/io.github.waydroidmulti.Manager.conf

systemctl daemon-reload
systemctl reload dbus 2>/dev/null || true
systemctl enable waydroid-multi.service
# (re)start: a restarted daemon adopts running instances
systemctl restart waydroid-multi.service
echo "Done. Next steps:"
echo "  waydroid-multi images sync      # copy the stock images into the image store (once per Waydroid upgrade)"
echo "  waydroid-multi create game1     # create an instance"
echo "  waydroid-multi start game1      # start it (or use the 'Waydroid Multi-Instance Manager' app)"
