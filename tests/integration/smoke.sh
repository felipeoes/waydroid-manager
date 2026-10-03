#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# End-to-end smoke test on a real host (needs: running daemon, Wayland session,
# sudo for the shell checks). Creates and deletes instances "smk1".."smk3".
#   tests/integration/smoke.sh [path/to/test.apk]
set -u
cd "$(dirname "$0")/../.."
if command -v waydroid-multi >/dev/null && [ -z "${WDM_FROM_REPO:-}" ]; then
    W="waydroid-multi"
else
    W="env PYTHONPATH=$PWD python3 -m waydroid_multi"
fi
APK="${1:-}"
FAIL=0
pass() { echo "  PASS  $*"; }
fail() { echo "  FAIL  $*"; FAIL=1; }
sh_in() { sudo env PYTHONPATH="$PWD" python3 -m waydroid_multi shell "$@" 2>/dev/null | cat; }
state() { $W status "$1" 2>/dev/null | awk '$1=="state"{print $2}'; }

stock_before=$(waydroid status 2>/dev/null | head -2 | tr '\n' ' ')
cleanup() {
    $W stop smk1 smk2 smk3 >/dev/null 2>&1
    $W delete -y smk1 smk2 smk3 >/dev/null 2>&1
}
trap cleanup EXIT
cleanup

echo "== create + start two instances"
$W images sync >/dev/null || fail "images sync"
$W create smk1 --name "Smoke One" --width 800 --height 450 >/dev/null && pass "create smk1" || fail "create smk1"
$W create smk2 --name "Smoke Two" --width 450 --height 640 --memory 3G >/dev/null && pass "create smk2" || fail "create smk2"
$W start smk1 --wait >/dev/null && pass "smk1 booted" || fail "smk1 boot"
$W start smk2 --wait >/dev/null && pass "smk2 booted" || fail "smk2 boot"

echo "== isolation and networking"
ip1=$($W status smk1 | awk '$1=="ip"{print $2}')
ip2=$($W status smk2 | awk '$1=="ip"{print $2}')
[ -n "$ip1" ] && [ "$ip1" != "$ip2" ] && pass "distinct fixed IPs $ip1 $ip2" || fail "IPs"
[ "$(sh_in smk1 /system/bin/getprop sys.boot_completed)" = "1" ] && pass "smk1 boot_completed" || fail "smk1 boot_completed"
sh_in smk1 /system/bin/ping -c1 -W3 1.1.1.1 | grep -q " 0% packet loss" && pass "smk1 internet" || fail "smk1 internet"
sh_in smk1 /system/bin/ping -c1 -W2 "$ip2" | grep -q " 0% packet loss" && fail "smk1 can reach smk2" || pass "smk1 cannot reach smk2"
[ "$(sh_in smk1 /system/bin/wm size | grep -o '[0-9]*x[0-9]*')" = "800x450" ] && pass "smk1 size 800x450" || fail "smk1 size"
[ "$(cat /sys/fs/cgroup/lxc.payload.wdm-smk2/memory.high 2>/dev/null)" = "3221225472" ] && pass "smk2 memory.high" || fail "smk2 memory.high"

if [ -n "$APK" ]; then
    echo "== app install isolation"
    before=$($W app list smk1 | awk '{print $1}' | sort)
    $W app install smk1 "$APK" >/dev/null && pass "app install" || fail "app install"
    pkg=$($W app list smk1 | awk '{print $1}' | sort | while read -r p; do
              echo "$before" | grep -qx "$p" || echo "$p"; done | head -1)
    if [ -n "$pkg" ] && ! $W app list smk2 | awk '{print $1}' | grep -qx "$pkg"; then
        pass "new app $pkg is only in smk1"
    else
        fail "app isolation (pkg='$pkg')"
    fi
fi

echo "== clone with identity reset"
id1=$(sh_in smk1 /system/bin/settings get secure android_id)
$W stop smk1 >/dev/null
$W clone smk1 smk3 >/dev/null && pass "clone smk1 -> smk3" || fail "clone"
$W start smk3 --wait >/dev/null && pass "smk3 booted" || fail "smk3 boot"
for _ in $(seq 1 30); do
    [ "$($W status smk3 | awk '$1=="pending_id_reset"{print $2}')" = "false" ] && break
    sleep 2
done
id3=$(sh_in smk3 /system/bin/settings get secure android_id)
[ -n "$id3" ] && [ "$id1" != "$id3" ] && pass "new android_id ($id1 -> $id3)" || fail "android_id reset"

echo "== daemon restart keeps instances"
if systemctl is-active -q waydroid-multi; then unit=waydroid-multi; else unit=waydroid-multi-dev; fi
if [ "$unit" = waydroid-multi ]; then
    sudo systemctl restart waydroid-multi
    sleep 4
    [ "$(state smk2)" = "RUNNING" ] || [ "$(state smk2)" = "FROZEN" ] && pass "smk2 survived restart" || fail "smk2 after restart"
else
    echo "  SKIP  (dev daemon; restart test needs the installed service)"
fi

echo "== stop and delete"
$W stop smk1 smk2 smk3 >/dev/null 2>&1
[ "$(state smk2)" = "STOPPED" ] && [ "$(state smk3)" = "STOPPED" ] && pass "stop" || fail "stop"
findmnt -rn -o TARGET | grep -q "waydroid-multi/instances/smk" && fail "mounts left" || pass "no mounts left"
$W delete -y smk1 smk2 smk3 >/dev/null && pass "delete" || fail "delete"
sudo test -d /var/lib/waydroid-multi/instances/smk1 && fail "instance dir left" || pass "instance dirs removed"
[ "$(waydroid status 2>/dev/null | head -2 | tr '\n' ' ')" = "$stock_before" ] && pass "stock Waydroid unchanged" || fail "stock state changed"

echo
[ "$FAIL" = 0 ] && echo "ALL PASSED" || echo "SOME CHECKS FAILED"
exit "$FAIL"
