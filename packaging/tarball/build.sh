#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Build dist/waydroid-manager-<version>-linux-<arch>.tar.gz with prebuilt libgbinder so
# immutable and non-Debian/Fedora distros can install with sudo scripts/install.sh without
# compiling anything.
#   packaging/tarball/build.sh      (no root needed)
set -eu
SRC="$(cd "$(dirname "$0")/../.." && pwd)"
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$SRC/waydroid_manager/__init__.py")"
[ -n "$VERSION" ] || { echo "cannot read __version__" >&2; exit 1; }
ARCH="$(uname -m)"
OUT="$SRC/dist"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
export SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-$(git -C "$SRC" log -1 --format=%ct 2>/dev/null || date +%s)}"

[ -f "$SRC/build/lib/libgbinder.so.1" ] && [ -f "$SRC/build/lib/libgbinder.LICENSE" ] ||
    "$SRC/packaging/build-libgbinder.sh" >/dev/null

TOP="waydroid-manager-$VERSION"
ROOT="$STAGE/$TOP"
mkdir -p "$ROOT/scripts" "$ROOT/build/lib"
cp -r "$SRC/waydroid_manager" "$SRC/data" "$ROOT/"
find "$ROOT" -name __pycache__ -prune -exec rm -rf {} +
install -m755 "$SRC/scripts/install.sh" "$ROOT/scripts/install.sh"
install -m755 "$SRC/scripts/uninstall.sh" "$ROOT/scripts/uninstall.sh"
install -m755 "$SRC/scripts/migrate-from-waydroid-multi.sh" "$ROOT/scripts/migrate-from-waydroid-multi.sh"
install -m644 "$SRC/build/lib/libgbinder.so.1" "$ROOT/build/lib/libgbinder.so.1"
install -m644 "$SRC/build/lib/libgbinder.LICENSE" "$ROOT/build/lib/libgbinder.LICENSE"
install -m644 "$SRC/README.md" "$ROOT/README.md"
install -m644 "$SRC/LICENSE" "$ROOT/LICENSE"
install -m644 "$SRC/CHANGELOG.md" "$ROOT/CHANGELOG.md"

# reproducible: fixed permissions and timestamps
find "$ROOT" -type d -exec chmod 755 {} +
find "$ROOT" -type f -perm /111 -exec chmod 755 {} +
find "$ROOT" -type f ! -perm /111 -exec chmod 644 {} +
chmod 755 "$ROOT"/data/*.sh "$ROOT"/scripts/*.sh

mkdir -p "$OUT"
TARBALL="$OUT/waydroid-manager-${VERSION}-linux-${ARCH}.tar.gz"
LC_ALL=C tar --owner=0 --group=0 --numeric-owner --sort=name --mtime="@$SOURCE_DATE_EPOCH" \
    -C "$STAGE" -cf "$STAGE/out.tar" "$TOP"
gzip -n -9 < "$STAGE/out.tar" > "$TARBALL"
echo "$TARBALL"
