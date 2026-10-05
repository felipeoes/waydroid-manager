#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Build the libgbinder Waydroid Manager ships: Android 15+ need servicemanager protocols
# (aidl5, aidl6) that Waydroid's own libgbinder 1.1.43 lacks. Output: build/lib/libgbinder.so.1
# It links the system libglibutil (>= 1.0.52); libglibutil's source is only used for headers.
#   packaging/build-libgbinder.sh     needs git, make, gcc, pkg-config, libglib2.0-dev
set -eu
GBINDER_TAG=1.1.53
GBINDER_COMMIT=fd67150ffc3e5ea08f4ba93e1b6c47fb282189e4
GLIBUTIL_TAG=1.0.80
GLIBUTIL_COMMIT=e2fe548f8cce21551fd59a461618ea586b5f98fb

SRC="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$SRC/build/lib"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

fetch() {  # repo tag commit dir
    git -c advice.detachedHead=false clone -q --depth 1 -b "$2" "$1" "$4"
    [ "$(git -C "$4" rev-parse HEAD)" = "$3" ] || { echo "$1 $2 is not commit $3" >&2; exit 1; }
}
fetch https://github.com/sailfishos/libglibutil.git "$GLIBUTIL_TAG" "$GLIBUTIL_COMMIT" "$WORK/glibutil"
fetch https://github.com/mer-hybris/libgbinder.git "$GBINDER_TAG" "$GBINDER_COMMIT" "$WORK/gbinder"
make -s -C "$WORK/glibutil" release >/dev/null
make -s -C "$WORK/gbinder" LIBGLIBUTIL_PATH="$WORK/glibutil" release >/dev/null
mkdir -p "$OUT"
install -m644 "$WORK/gbinder/build/release/libgbinder.so.$GBINDER_TAG" "$OUT/libgbinder.so.1"
echo "$OUT/libgbinder.so.1"
