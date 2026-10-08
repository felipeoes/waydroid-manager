#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Check ADB before/after Android setup and restart. Creates and deletes test instances.
#   tests/integration/adb.sh [11 13 14 15 16 17]
set -eu
SRC="$(cd "$(dirname "$0")/../.." && pwd)"
if [ -n "${WDM_FROM_REPO:-}" ]; then
    W="env PYTHONPATH=$SRC python3 -m waydroid_manager"
else
    W="waydroid-manager"
fi
# Python's -m would otherwise import the checkout even through the installed wrapper.
cd /
command -v adb >/dev/null
N=""
cleanup() {
    if [ -n "$N" ]; then
        $W stop "$N" >/dev/null 2>&1 || true
        $W delete -y "$N" >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT
sh_in() { $W shell "$N" "$@"; }
check_adb() {
    for _ in $(seq 1 30); do
        if [ "$(timeout 5 adb -s "$SERIAL" shell getprop waydroid.manager.instance 2>/dev/null)" = "$N" ]; then
            break
        fi
        sleep 1
    done
    [ "$(timeout 5 adb -s "$SERIAL" shell getprop waydroid.manager.instance)" = "$N" ]
    [ "$(sh_in /system/bin/settings get global adb_enabled)" = 1 ]
    [ "$(sh_in /system/bin/getprop persist.sys.usb.config)" = adb ]
    [ "$(sh_in /system/bin/getprop init.svc.adbd)" = running ]
    [ "$(sh_in /system/bin/getprop ro.adb.secure)" = 1 ]
    [ "$(sh_in /system/bin/getprop persist.adb.tradeinmode)" != 1 ]
    echo "PASS Android $VERSION: $1 (normal, authenticated ADB)"
}
[ "$#" -gt 0 ] || set -- 11 13 14 15 16 17
for VERSION do
    N=$($W create --android "$VERSION" --name "ADB Test $VERSION" --idle-action none --no-launcher |
        sed -n 's/^Created instance #\([0-9]*\).*/\1/p')
    [ -n "$N" ]
    SERIAL="$($W status "$N" | awk '$1=="adb_host"{print $2}'):5555"
    $W start "$N" --background --wait >/dev/null
    check_adb "first boot"
    sh_in /system/bin/settings put global device_provisioned 1
    sh_in /system/bin/settings put secure user_setup_complete 1
    sleep 3
    check_adb "setup complete"
    $W stop "$N" >/dev/null
    $W start "$N" --background --wait >/dev/null
    check_adb "restart"
    cleanup
    N=""
done
