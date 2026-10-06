#!/bin/sh -
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Shared network for waydroid-manager instances. Derived from Waydroid's
# waydroid-net.sh (itself derived from LXC's lxc-net), but fully parameterised
# so it never collides with the stock waydroid0 bridge or LXC's lxcbr0:
#   - its own bridge, subnet, dnsmasq instance, pid/lease/hosts files
#   - iptables rules scoped to its own bridge and subnet
#   - an nftables table named "waydroid_manager" (never "lxc")
#
# Usage: waydroid-manager-net.sh {setup|teardown|run-dnsmasq|reload}
#   setup        create the bridge and firewall rules
#   teardown     remove firewall rules and the bridge
#   run-dnsmasq  exec dnsmasq in the foreground (for a systemd service)
#   reload       make a running dnsmasq re-read the static hosts file
#
# Configuration comes from the environment (normally an EnvironmentFile
# written by the daemon); every variable has a default.

: "${WDM_BRIDGE:=wdm0}"
: "${WDM_BRIDGE_MAC:=02:57:44:4d:00:00}"
: "${WDM_ADDR:=192.168.241.1}"
: "${WDM_NETMASK:=255.255.255.0}"
: "${WDM_NETWORK:=192.168.241.0/24}"
: "${WDM_DHCP_START:=192.168.241.0}"
: "${WDM_VARRUN:=/run/waydroid-manager}"
: "${WDM_HOSTSFILE:=${WDM_VARRUN}/dhcp-hosts}"
: "${WDM_LEASEFILE:=/var/lib/misc/dnsmasq.${WDM_BRIDGE}.leases}"
: "${WDM_USE_NFT:=false}"
: "${WDM_NFT_TABLE:=waydroid_manager}"

IPTABLES_BIN="$(command -v iptables-legacy)"
[ -n "$IPTABLES_BIN" ] || IPTABLES_BIN="$(command -v iptables)"

use_nft() {
    [ "$WDM_USE_NFT" = "true" ] && command -v nft >/dev/null 2>&1 && nft list ruleset >/dev/null 2>&1
}

ipt() {
    "$IPTABLES_BIN" $IPT_LOCK "$@"
}

if ! use_nft && [ -n "$IPTABLES_BIN" ]; then
    IPT_LOCK="-w"
    "$IPTABLES_BIN" -w -L -n >/dev/null 2>&1 || IPT_LOCK=""
fi

_netmask2cidr() {
    # Assumes there's no "255." after a non-255 byte in the mask
    local x=${1##*255.}
    set -- 0^^^128^192^224^240^248^252^254^ $(( (${#1} - ${#x})*2 )) ${x%%.*}
    x=${1%%$3*}
    echo $(( $2 + (${#x}/4) ))
}

iptables_rules() {
    # $1 is -I (insert) or -D (delete)
    op="$1"
    ipt $op INPUT -i "$WDM_BRIDGE" -p udp --dport 67 -j ACCEPT
    ipt $op INPUT -i "$WDM_BRIDGE" -p tcp --dport 67 -j ACCEPT
    ipt $op INPUT -i "$WDM_BRIDGE" -p udp --dport 53 -j ACCEPT
    ipt $op INPUT -i "$WDM_BRIDGE" -p tcp --dport 53 -j ACCEPT
    ipt $op FORWARD -i "$WDM_BRIDGE" -j ACCEPT
    ipt $op FORWARD -o "$WDM_BRIDGE" -j ACCEPT
    if [ "$op" = "-I" ]; then nat_op="-A"; else nat_op="-D"; fi
    ipt -t nat $nat_op POSTROUTING -s "$WDM_NETWORK" ! -d "$WDM_NETWORK" -j MASQUERADE
    ipt -t mangle $nat_op POSTROUTING -o "$WDM_BRIDGE" -p udp -m udp --dport 68 -j CHECKSUM --checksum-fill
}

nft_setup() {
    nft "add table inet ${WDM_NFT_TABLE};
flush table inet ${WDM_NFT_TABLE};
add chain inet ${WDM_NFT_TABLE} input { type filter hook input priority 0; };
add rule inet ${WDM_NFT_TABLE} input iifname ${WDM_BRIDGE} udp dport { 53, 67 } accept;
add rule inet ${WDM_NFT_TABLE} input iifname ${WDM_BRIDGE} tcp dport { 53, 67 } accept;
add chain inet ${WDM_NFT_TABLE} forward { type filter hook forward priority 0; };
add rule inet ${WDM_NFT_TABLE} forward iifname ${WDM_BRIDGE} accept;
add rule inet ${WDM_NFT_TABLE} forward oifname ${WDM_BRIDGE} accept;
add table ip ${WDM_NFT_TABLE};
flush table ip ${WDM_NFT_TABLE};
add chain ip ${WDM_NFT_TABLE} postrouting { type nat hook postrouting priority 100; };
add rule ip ${WDM_NFT_TABLE} postrouting ip saddr ${WDM_NETWORK} ip daddr != ${WDM_NETWORK} counter masquerade"
}

nft_teardown() {
    nft "add table inet ${WDM_NFT_TABLE};
delete table inet ${WDM_NFT_TABLE};
add table ip ${WDM_NFT_TABLE};
delete table ip ${WDM_NFT_TABLE};"
}

setup() {
    if [ -f "${WDM_VARRUN}/network_up" ]; then
        teardown force
    fi

    FAILED=1
    cleanup() {
        set +e
        if [ "$FAILED" = "1" ]; then
            echo "Failed to set up waydroid-manager network" >&2
            teardown force
            exit 1
        fi
    }
    trap cleanup EXIT HUP INT TERM
    set -e

    mkdir -p "$WDM_VARRUN"
    if command -v restorecon >/dev/null 2>&1; then
        restorecon "$WDM_VARRUN" || true
    fi
    [ -f "$WDM_HOSTSFILE" ] || : > "$WDM_HOSTSFILE"

    [ -d "/sys/class/net/${WDM_BRIDGE}" ] || ip link add dev "$WDM_BRIDGE" type bridge
    echo 1 > /proc/sys/net/ipv4/ip_forward
    echo 0 > "/proc/sys/net/ipv6/conf/${WDM_BRIDGE}/accept_dad" || true

    ip addr flush dev "$WDM_BRIDGE"
    ip addr add "${WDM_ADDR}/$(_netmask2cidr "$WDM_NETMASK")" broadcast + dev "$WDM_BRIDGE"
    ip link set dev "$WDM_BRIDGE" address "$WDM_BRIDGE_MAC"
    ip link set dev "$WDM_BRIDGE" up

    if use_nft; then
        nft_setup
    else
        iptables_rules -I
    fi

    touch "${WDM_VARRUN}/network_up"
    FAILED=0
    trap - EXIT HUP INT TERM
}

teardown() {
    [ -f "${WDM_VARRUN}/network_up" ] || [ "$1" = "force" ] || { echo "waydroid-manager network isn't up"; return 0; }
    set +e
    if use_nft; then
        nft_teardown
    elif [ -f "${WDM_VARRUN}/network_up" ]; then
        iptables_rules -D 2>/dev/null
    fi
    if [ -d "/sys/class/net/${WDM_BRIDGE}" ]; then
        ip addr flush dev "$WDM_BRIDGE"
        ip link set dev "$WDM_BRIDGE" down
        # Leave the bridge alone if containers are still attached to it
        ls "/sys/class/net/${WDM_BRIDGE}/brif/"* >/dev/null 2>&1 || ip link delete "$WDM_BRIDGE"
    fi
    rm -f "${WDM_VARRUN}/network_up"
}

run_dnsmasq() {
    for DNSMASQ_USER in lxc-dnsmasq dnsmasq nobody; do
        getent passwd "$DNSMASQ_USER" >/dev/null && break
    done
    mkdir -p "$(dirname "$WDM_LEASEFILE")"
    [ -f "$WDM_HOSTSFILE" ] || : > "$WDM_HOSTSFILE"
    # Static mode: only MACs listed in the hosts file get an address, so every
    # instance always gets the same IP. SIGHUP re-reads the hosts file.
    exec dnsmasq --keep-in-foreground --conf-file=/dev/null -u "$DNSMASQ_USER" \
        --strict-order --bind-interfaces --pid-file="${WDM_VARRUN}/dnsmasq.pid" \
        --listen-address "$WDM_ADDR" --dhcp-range "${WDM_DHCP_START},static" \
        --dhcp-hostsfile="$WDM_HOSTSFILE" --dhcp-no-override \
        --except-interface=lo --interface="$WDM_BRIDGE" \
        --dhcp-leasefile="$WDM_LEASEFILE" --dhcp-authoritative
}

reload() {
    pid=$(cat "${WDM_VARRUN}/dnsmasq.pid" 2>/dev/null) && kill -HUP "$pid"
}

case "$1" in
    setup) setup ;;
    teardown) teardown "$2" ;;
    run-dnsmasq) run_dnsmasq ;;
    reload) reload ;;
    *)
        echo "Usage: $0 {setup|teardown|run-dnsmasq|reload}" >&2
        exit 2
        ;;
esac
