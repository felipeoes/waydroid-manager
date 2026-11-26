#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Build dist/waydroid-manager-<version>-1.<arch>.rpm from the files scripts/install.sh stages,
# with the libgbinder packaging/build-libgbinder.sh builds.
#   packaging/rpm/build.sh      (no root needed)
set -eu
SRC="$(cd "$(dirname "$0")/../.." && pwd)"
HERE="$SRC/packaging/rpm"
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$SRC/waydroid_manager/__init__.py")"
[ -n "$VERSION" ] || { echo "cannot read __version__" >&2; exit 1; }
OUT="$SRC/dist"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
export SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-$(git -C "$SRC" log -1 --format=%ct 2>/dev/null || date +%s)}"

[ -f "$SRC/build/lib/libgbinder.so.1" ] && [ -f "$SRC/build/lib/libgbinder.LICENSE" ] ||
    "$SRC/packaging/build-libgbinder.sh" >/dev/null

STAGE="$WORK/stage"
RPMTOP="$WORK/rpmbuild"
mkdir -p "$STAGE" "$RPMTOP/BUILD" "$RPMTOP/BUILDROOT" "$RPMTOP/RPMS" "$RPMTOP/SOURCES" "$RPMTOP/SPECS" "$RPMTOP/SRPMS"

"$SRC/scripts/install.sh" --destdir "$STAGE" --prefix /usr --no-activate --method rpm >/dev/null
install -Dm644 "$HERE/85-waydroid-manager.preset" \
    "$STAGE/usr/lib/systemd/system-preset/85-waydroid-manager.preset"
install -Dm644 "$SRC/LICENSE" "$STAGE/usr/share/licenses/waydroid-manager/LICENSE"
install -Dm644 "$SRC/build/lib/libgbinder.LICENSE" \
    "$STAGE/usr/share/licenses/waydroid-manager/libgbinder.LICENSE"
install -Dm644 "$SRC/README.md" "$STAGE/usr/share/doc/waydroid-manager/README.md"

# reproducible: fixed permissions and timestamps
find "$STAGE" -type d -exec chmod 755 {} +
find "$STAGE" -type f -perm /111 -exec chmod 755 {} +
find "$STAGE" -type f ! -perm /111 -exec chmod 644 {} +
# RPM expects shared libraries to be executable for stripping and rpmlint checks
chmod 755 "$STAGE/usr/lib/waydroid-manager/lib/libgbinder.so.1"
find "$STAGE" -exec touch -h -d "@$SOURCE_DATE_EPOCH" {} +

CHANGELOG_DATE="$(LC_ALL=C date -u -d "@$SOURCE_DATE_EPOCH" '+%a %b %d %Y')"
SPEC="$RPMTOP/SPECS/waydroid-manager.spec"
sed -e "s/@VERSION@/$VERSION/g" -e "s/@CHANGELOG_DATE@/$CHANGELOG_DATE/g" \
    "$HERE/waydroid-manager.spec.in" > "$SPEC"

rpmbuild -bb \
    --define "_topdir $RPMTOP" \
    --define "_stagedir $STAGE" \
    --define "clamp_mtime_to_source_date_epoch 1" \
    --define "use_source_date_epoch_as_buildtime 1" \
    "$SPEC" >/dev/null

RPM_BUILT="$(find "$RPMTOP/RPMS" -name '*.rpm' -type f | head -n 1)"
[ -n "$RPM_BUILT" ] || { echo "rpmbuild produced no .rpm" >&2; exit 1; }
mkdir -p "$OUT"
RPM="$OUT/$(basename "$RPM_BUILT")"
cp "$RPM_BUILT" "$RPM"
echo "$RPM"
if command -v rpmlint >/dev/null 2>&1; then
    rpmlint "$RPM" || true
fi
