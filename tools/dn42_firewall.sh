#!/usr/bin/env bash
# Restrict the web UI port to DN42 sources.
#
# tools/webui.py has no authentication: anyone who reaches the port has full
# control of the emulated radio. Binding to :: makes it reachable from the public
# internet, so the port has to be filtered by source address instead.
#
# The important detail: the INPUT policy on this host is ACCEPT, so a rule that
# only *allows* DN42 does nothing at all -- without any rules the port is already
# reachable. What actually restricts anything is the final DROP. Order matters:
# accept loopback and DN42 first, then drop the rest.
#
# DN42 ranges: 172.20.0.0/14 (v4) and fd00::/8 (v6). This host already had rules
# using those same prefixes for SIP, so the convention is established.
#
# These rules do NOT survive a reboot. Re-run this script, or persist them with
# iptables-persistent / a systemd unit.
#
# Usage:
#   tools/dn42_firewall.sh apply [port]     # default port 8080
#   tools/dn42_firewall.sh remove [port]
#   tools/dn42_firewall.sh show [port]
set -uo pipefail

ACTION="${1:-show}"
PORT="${2:-8080}"
TAG="uvk5-webui"

need_root() {
    if [ "$(id -u)" -ne 0 ]; then
        echo "needs root for iptables" >&2
        exit 1
    fi
}

# Add a rule only when an identical one is absent, so re-running is safe.
add4() { iptables -C INPUT "$@" 2>/dev/null || iptables -A INPUT "$@"; }
add6() { ip6tables -C INPUT "$@" 2>/dev/null || ip6tables -A INPUT "$@"; }
del4() { while iptables -C INPUT "$@" 2>/dev/null; do iptables -D INPUT "$@"; done; }
del6() { while ip6tables -C INPUT "$@" 2>/dev/null; do ip6tables -D INPUT "$@"; done; }

case "$ACTION" in
apply)
    need_root
    # Accept first...
    add4 -p tcp --dport "$PORT" -s 127.0.0.1     -j ACCEPT -m comment --comment "$TAG: loopback"
    add4 -p tcp --dport "$PORT" -s 172.20.0.0/14 -j ACCEPT -m comment --comment "$TAG: dn42 v4"
    add6 -p tcp --dport "$PORT" -s ::1           -j ACCEPT -m comment --comment "$TAG: loopback"
    add6 -p tcp --dport "$PORT" -s fd00::/8      -j ACCEPT -m comment --comment "$TAG: dn42 v6"
    # ...then drop everything else. This is the rule that does the work.
    add4 -p tcp --dport "$PORT" -j DROP -m comment --comment "$TAG: deny non-dn42"
    add6 -p tcp --dport "$PORT" -j DROP -m comment --comment "$TAG: deny non-dn42"
    echo "applied: port $PORT reachable from DN42 and loopback only"
    ;;
remove)
    need_root
    del4 -p tcp --dport "$PORT" -j DROP -m comment --comment "$TAG: deny non-dn42"
    del6 -p tcp --dport "$PORT" -j DROP -m comment --comment "$TAG: deny non-dn42"
    del4 -p tcp --dport "$PORT" -s 127.0.0.1     -j ACCEPT -m comment --comment "$TAG: loopback"
    del4 -p tcp --dport "$PORT" -s 172.20.0.0/14 -j ACCEPT -m comment --comment "$TAG: dn42 v4"
    del6 -p tcp --dport "$PORT" -s ::1           -j ACCEPT -m comment --comment "$TAG: loopback"
    del6 -p tcp --dport "$PORT" -s fd00::/8      -j ACCEPT -m comment --comment "$TAG: dn42 v6"
    echo "removed: port $PORT is no longer filtered by these rules"
    ;;
show)
    echo "--- v4 ---"
    iptables -S INPUT 2>/dev/null | grep -- "--dport $PORT" || echo "(none)"
    echo "--- v6 ---"
    ip6tables -S INPUT 2>/dev/null | grep -- "--dport $PORT" || echo "(none)"
    echo "--- packet counts (a rising DROP count means the filter is working) ---"
    iptables  -L INPUT -v -n 2>/dev/null | grep "dpt:$PORT" || true
    ip6tables -L INPUT -v -n 2>/dev/null | grep "dpt:$PORT" || true
    ;;
*)
    echo "usage: $0 {apply|remove|show} [port]" >&2
    exit 2
    ;;
esac
