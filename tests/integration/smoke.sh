#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# End-to-end smoke test on a real host (needs: running daemon, Wayland session,
# sudo for the shell checks). Creates instances named "Smoke …" and deletes them.
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
CREATED=""
pass() { echo "  PASS  $*"; }
fail() { echo "  FAIL  $*"; FAIL=1; }
# lxc-attach chowns its stdio: always go through pipes
sh_in() { $W shell "$@" 2>/dev/null < /dev/null | cat; }
state() { $W status "$1" 2>/dev/null | awk '$1=="state"{print $2}'; }
field() { $W status "$1" 2>/dev/null | awk -v k="$2" '$1==k{print $2}'; }
# create/clone print "Created instance #N …"
num() { sed -n 's/^Created instance #\([0-9]*\).*/\1/p'; }

stock_before=$($W list | awk '$1=="0"{print $(NF-4)}')
cleanup() {
    for n in $CREATED; do
        $W stop "$n" >/dev/null 2>&1
        $W delete -y "$n" >/dev/null 2>&1
    done
}
trap cleanup EXIT

echo "== create + start two instances"
n1=$($W create --name "Smoke One" --width 800 --height 450 --dpi 160 --no-launcher | num)
n2=$($W create --name "Smoke Two" --width 450 --height 800 --memory 3G --device pixel_7 --no-launcher | num)
CREATED="$n1 $n2"
[ -n "$n1" ] && [ -n "$n2" ] && [ "$n1" != "$n2" ] && pass "created #$n1 and #$n2" || fail "create"
$W start "$n1" --wait >/dev/null && pass "#$n1 booted" || fail "#$n1 boot"
$W start "$n2" --wait >/dev/null && pass "#$n2 booted" || fail "#$n2 boot"

echo "== references, isolation and networking"
[ "$(field "Smoke One" id)" = "$n1" ] && pass "name resolves to #$n1" || fail "name reference"
ip1=$(field "$n1" ip)
ip2=$(field "$n2" ip)
[ -n "$ip1" ] && [ "$ip1" != "$ip2" ] && pass "distinct fixed IPs $ip1 $ip2" || fail "IPs"
[ "$(sh_in "$n1" /system/bin/getprop sys.boot_completed)" = "1" ] && pass "#$n1 boot_completed" || fail "#$n1 boot_completed"
sh_in "$n1" /system/bin/ping -c1 -W3 1.1.1.1 | grep -q " 0% packet loss" && pass "#$n1 internet" || fail "#$n1 internet"
sh_in "$n1" /system/bin/ping -c1 -W2 "$ip2" | grep -q " 0% packet loss" && fail "#$n1 can reach #$n2" || pass "#$n1 cannot reach #$n2"
[ "$(sh_in "$n1" /system/bin/wm size | grep -o '[0-9]*x[0-9]*')" = "800x450" ] && pass "#$n1 screen 800x450" || fail "#$n1 screen"
[ "$(cat /sys/fs/cgroup/lxc.payload.wdm-$n2/memory.high 2>/dev/null)" = "3221225472" ] && pass "#$n2 memory.high" || fail "#$n2 memory.high"
[ "$(sh_in "$n2" /system/bin/getprop ro.product.model)" = "Pixel 7" ] && pass "#$n2 device model Pixel 7" || fail "#$n2 device model"

if [ -n "$APK" ]; then
    echo "== app install isolation"
    # third-party packages only: system apps (e.g. Google's) can appear late on their own
    user_pkgs() { sh_in "$1" /system/bin/pm list packages -3 | sed 's/^package://' | sort; }
    before=$(user_pkgs "$n1")
    $W app install "$n1" "$APK" >/dev/null && pass "app install" || fail "app install"
    pkg=$(user_pkgs "$n1" | while read -r p; do echo "$before" | grep -qx "$p" || echo "$p"; done | head -1)
    if [ -n "$pkg" ] && ! user_pkgs "$n2" | grep -qx "$pkg"; then
        pass "new app $pkg is only in #$n1"
    else
        fail "app isolation (pkg='$pkg')"
    fi
fi

echo "== clone with identity reset"
id1=$(sh_in "$n1" /system/bin/settings get secure android_id)
n3=$($W clone "$n1" --name "Smoke Three" --no-launcher | num)
CREATED="$CREATED $n3"
[ -n "$n3" ] && pass "cloned #$n1 into #$n3" || fail "clone"
$W start "$n3" --wait >/dev/null && pass "#$n3 booted" || fail "#$n3 boot"
for _ in $(seq 1 30); do
    [ "$(field "$n3" pending_id_reset)" = "false" ] && break
    sleep 2
done
id3=$(sh_in "$n3" /system/bin/settings get secure android_id)
[ -n "$id3" ] && [ "$id1" != "$id3" ] && pass "new android_id ($id1 -> $id3)" || fail "android_id reset"
[ "$(field "$n3" width)" = "800" ] && pass "clone kept the source's settings" || fail "clone settings"

echo "== daemon restart keeps instances"
if systemctl is-active -q waydroid-multi; then
    sudo systemctl restart waydroid-multi
    sleep 4
    st=$(state "$n2")
    [ "$st" = "RUNNING" ] || [ "$st" = "FROZEN" ] && pass "#$n2 survived restart" || fail "#$n2 after restart ($st)"
else
    echo "  SKIP  (needs the installed service)"
fi

echo "== stop, delete, number reuse"
for n in $n1 $n2 $n3; do $W stop "$n" >/dev/null 2>&1; done
[ "$(state "$n2")" = "STOPPED" ] && [ "$(state "$n3")" = "STOPPED" ] && pass "stopped" || fail "stop"
findmnt -rn -o TARGET | grep -qE "waydroid-multi/instances/($n1|$n2|$n3)/" && fail "mounts left" || pass "no mounts left"
$W delete -y "$n1" >/dev/null && pass "deleted #$n1" || fail "delete"
n4=$($W create --name "Smoke Four" --no-launcher | num)
CREATED="$n2 $n3 $n4"
[ "$n4" = "$n1" ] && pass "freed number #$n1 reused" || fail "number reuse (got #$n4)"
sudo test -d "/var/lib/waydroid-multi/instances/$n1/data/system" && fail "old data left in #$n1" || pass "reused number starts clean"
cleanup
CREATED=""
sudo test -d "/var/lib/waydroid-multi/instances/$n2" && fail "instance dir left" || pass "instance dirs removed"
[ "$($W list | awk '$1=="0"{print $(NF-4)}')" = "$stock_before" ] && pass "stock Waydroid unchanged" || fail "stock state changed"

echo
[ "$FAIL" = 0 ] && echo "ALL PASSED" || echo "SOME CHECKS FAILED"
exit "$FAIL"
