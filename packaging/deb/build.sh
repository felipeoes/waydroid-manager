#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Build dist/waydroid-multi_<version>_all.deb from the files scripts/install.sh stages.
#   packaging/deb/build.sh      (no root needed)
set -eu
SRC="$(cd "$(dirname "$0")/../.." && pwd)"
HERE="$SRC/packaging/deb"
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$SRC/waydroid_multi/__init__.py")"
[ -n "$VERSION" ] || { echo "cannot read __version__" >&2; exit 1; }
OUT="$SRC/dist"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
export SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-$(git -C "$SRC" log -1 --format=%ct 2>/dev/null || date +%s)}"

"$SRC/scripts/install.sh" --destdir "$STAGE" --prefix /usr --no-activate --method deb >/dev/null
install -Dm644 "$SRC/LICENSE" "$STAGE/usr/share/doc/waydroid-multi/copyright"

mkdir -p "$STAGE/DEBIAN"
for f in preinst postinst prerm postrm; do
    install -m755 "$HERE/$f" "$STAGE/DEBIAN/$f"
done
echo /etc/waydroid-multi/daemon.conf > "$STAGE/DEBIAN/conffiles"
SIZE="$(du -sk --exclude=DEBIAN "$STAGE" | cut -f1)"
sed -e "s/@VERSION@/$VERSION/" -e "s/@SIZE@/$SIZE/" "$HERE/control.in" > "$STAGE/DEBIAN/control"
(cd "$STAGE" && find . -path ./DEBIAN -prune -o -type f -print | sed 's|^\./||' | LC_ALL=C sort |
    xargs -d '\n' md5sum > DEBIAN/md5sums)
# reproducible: fixed permissions and timestamps
find "$STAGE" -type d -exec chmod 755 {} +
find "$STAGE" -type f -perm /111 -exec chmod 755 {} +
find "$STAGE" -type f ! -perm /111 -exec chmod 644 {} +
find "$STAGE" -exec touch -h -d "@$SOURCE_DATE_EPOCH" {} +

mkdir -p "$OUT"
DEB="$OUT/waydroid-multi_${VERSION}_all.deb"
dpkg-deb --root-owner-group -Zxz --build "$STAGE" "$DEB" >/dev/null
echo "$DEB"
if command -v lintian >/dev/null; then
    lintian --no-tag-display-limit "$DEB" || true
fi
