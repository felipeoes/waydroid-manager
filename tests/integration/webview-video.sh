#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Usage: webview-video.sh ADB_SERIAL SDK_DIRECTORY [short-H264.mp4]
# Requires JDK, Android SDK platform 35 and build-tools 36.0.0. No game assets are bundled.
set -eu
SRC=$(cd "$(dirname "$0")/compat" && pwd)
SERIAL=${1:?ADB serial required}
SDK=${2:?Android SDK directory required}
BT=$SDK/build-tools/36.0.0
ANDROID_JAR=$SDK/platforms/android-35/android.jar
PKG=io.github.waydroidmanager.compatprobe
if adb -s "$SERIAL" shell pm path "$PKG" | grep -q '^package:'; then
    echo "Remove the existing $PKG test app before running this probe." >&2
    exit 1
fi
TMP=$(mktemp -d)
cleanup() {
    adb -s "$SERIAL" uninstall "$PKG" >/dev/null 2>&1 || true
    rm -rf "$TMP"
}
trap cleanup EXIT
mkdir "$TMP/classes" "$TMP/assets"
if [ "$#" -gt 2 ]; then cp "$3" "$TMP/assets/sample.mp4"; fi
cd "$TMP"
javac -source 8 -target 8 -classpath "$ANDROID_JAR" -d classes "$SRC/Probe.java"
"$BT/d8" --lib "$ANDROID_JAR" --output . classes/io/github/waydroidmanager/compatprobe/Probe.class
"$BT/aapt" package -f -M "$SRC/AndroidManifest.xml" -A assets -I "$ANDROID_JAR" -F unsigned.apk
"$BT/aapt" add unsigned.apk classes.dex
"$BT/zipalign" -f 4 unsigned.apk aligned.apk
keytool -genkeypair -keystore key.p12 -storepass android -keypass android -alias probe \
    -keyalg RSA -dname CN=Probe -validity 1 >/dev/null 2>&1
"$BT/apksigner" sign --ks key.p12 --ks-pass pass:android --out probe.apk aligned.apk
adb -s "$SERIAL" install --no-incremental probe.apk
probe() {
    adb -s "$SERIAL" shell am force-stop "$PKG"
    adb -s "$SERIAL" shell am start -W -n "$PKG/.Probe" --ez video "$2" >/dev/null
    PID=$(adb -s "$SERIAL" shell pidof "$PKG" | tr -d '\r')
    if [ -z "$PID" ]; then echo "$1 probe did not start" >&2; return 1; fi
    for _ in $(seq 1 30); do
        LOG=$(adb -s "$SERIAL" logcat -d --pid="$PID" -s CompatProbe:I '*:S')
        if echo "$LOG" | grep -q "$1 PASS"; then echo "$LOG"; return; fi
        if echo "$LOG" | grep -qE 'FAIL|exception'; then echo "$LOG"; return 1; fi
        sleep 1
    done
    echo "$1 timed out" >&2
    return 1
}
probe UA false
if [ "$#" -gt 2 ]; then probe VIDEO true; fi
