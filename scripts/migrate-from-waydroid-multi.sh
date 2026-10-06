#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# One-time move of a waydroid-multi 0.5 install to Waydroid Manager 1.0 (run as root by
# install.sh and the .deb's postinst): its instances, image store and network settings
# carry over. A waydroid-multi .deb is removed by dpkg (Conflicts), whose prerm stops it;
# a source install is removed here with its own uninstaller, which keeps the instances.
set -u
OLD=/var/lib/waydroid-multi
NEW=/var/lib/waydroid-manager
[ -d "$OLD" ] && [ ! -e "$NEW" ] || exit 0

if [ -x /usr/lib/waydroid-multi/uninstall.sh ] && ! dpkg-query -S /usr/lib/waydroid-multi >/dev/null 2>&1; then
    /usr/lib/waydroid-multi/uninstall.sh
fi
if awk -v d="$OLD" '$2 == d || index($2, d "/") == 1 {f=1} END {exit !f}' /proc/self/mounts; then
    echo "waydroid-manager: waydroid-multi's instances are still mounted; reboot, then run" >&2
    echo "  sudo /usr/lib/waydroid-manager/migrate-from-waydroid-multi.sh" >&2
    exit 0
fi

mv "$OLD" "$NEW"
for f in "$NEW"/instances/*/instance.cfg; do
    [ -f "$f" ] && sed -i "s|$OLD/|$NEW/|g" "$f"
done
# The Android version of an API level stock Waydroid's images can have (10 isn't offered)
android_of() {
    case "$1" in 30) echo 11 ;; 33) echo 13 ;; esac
}
# Its image sets are copies of stock Waydroid's images: describe them as the daemon's sync does
# (image.cfg), so #0 knows its Android. A set of the official GAPPS builds is also one of its
# version, as the daemon's install adopts it: the devices on it keep it, offline too.
ota="$(sed -n 's/^system_ota *= *//p' /var/lib/waydroid/waydroid.cfg 2>/dev/null)"
mnt="$(mktemp -d)"
for d in "$NEW"/images/*; do
    [ -f "$d/system.img" ] && [ ! -L "$d" ] && [ ! -e "$d/image.cfg" ] || continue
    sdk=0
    if mount -o ro,loop "$d/system.img" "$mnt" 2>/dev/null; then
        sdk="$(sed -n 's/^ro\.build\.version\.sdk=//p' "$mnt/system/build.prop")"
        umount "$mnt"
    fi
    id="${d##*/}"
    printf '[image]\nsdk = %s\nbuilt = %s\nstock = true\nchannel = %s\n' "${sdk:-0}" "${id%%-*}" "$ota" > "$d/image.cfg"
    a="$(android_of "$sdk")"
    if [ -n "$a" ] && [ "$ota" = https://ota.waydro.id/system/lineage/waydroid_x86_64/GAPPS.json ]; then
        echo "android = $a" >> "$d/image.cfg"
    fi
done
rmdir "$mnt"
# Each device keeps the Android its data is (a device without one would get 13, for good)
for f in "$NEW"/instances/*/instance.cfg; do
    [ -f "$f" ] && ! grep -q '^android *=' "$f" || continue
    index="$(sed -n 's/^index *= *//p' "$f")"
    [ "$index" != 0 ] || continue
    sdk="$(sed -n 's/^sdk *= *//p' "$NEW/images/$(sed -n 's/^image_id *= *//p' "$f")/image.cfg" 2>/dev/null)"
    a="$(android_of "$sdk")"
    if [ -n "$a" ]; then
        sed -i "/^\[instance\]\$/a android = $a" "$f"
    else
        echo "waydroid-manager: #$index runs an Android Waydroid Manager doesn't offer (API level ${sdk:-unknown}):" \
             "its first start moves its data to Android 13" >&2
    fi
done
if [ -f /etc/waydroid-multi/daemon.conf ]; then
    mkdir -p /etc/waydroid-manager
    sed 's/wdmulti0/wdm0/' /etc/waydroid-multi/daemon.conf > /etc/waydroid-manager/daemon.conf
fi
rm -rf /etc/waydroid-multi
rm -f /var/lib/misc/dnsmasq.wdmulti0.leases
getent passwd | while IFS=: read -r user _ uid _ _ home _; do
    [ "$uid" -ge 1000 ] 2>/dev/null && [ "$uid" -lt 60000 ] && [ -d "$home/.cache/waydroid-multi" ] || continue
    runuser -u "$user" -- rm -rf "$home/.cache/waydroid-multi" 2>/dev/null
done
echo "Moved waydroid-multi's instances to Waydroid Manager."
