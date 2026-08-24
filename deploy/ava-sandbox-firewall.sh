#!/usr/bin/env bash
# ava-sandbox-firewall.sh — let the OpenShell sandbox bridge reach host services.
#
# WHY THIS EXISTS
# ---------------
# The host runs `-P INPUT DROP`. Containers on the OpenShell docker bridge reach
# host services via the bridge GATEWAY ip (172.27.0.1 = `host.openshell.internal`
# inside the sandbox), and that traffic lands in INPUT, so each service the
# sandbox is allowed to reach needs an explicit ACCEPT.
#
# Those rules were previously added by hand and lived only in memory. Nothing
# persisted them, so a reboot dropped all of them: the sandbox could no longer
# fetch its policy from the gateway on :8080 and exited after 5 attempts, ~once
# a minute. That is the crash loop recorded in ava.yaml on 2026-08-13.
#
# THE OTHER FAILURE MODE
# ----------------------
# The rules match on `-i br-<netid>`, and that interface name is derived from
# the docker NETWORK ID. Recreate the network and every rule keeps matching an
# interface that no longer exists — silently, with no error anywhere. So this
# script never hardcodes the interface: it asks docker, every run, and it
# removes its own stale rules before adding current ones.
#
# Rules are tagged with an iptables comment so cleanup can be exact rather than
# heuristic. Nothing untagged is ever touched.
set -uo pipefail

NET="${AVA_SANDBOX_NET:-openshell-docker}"
# 8080 openshell gateway (policy plane — without this the sandbox cannot start)
# 8010 ava inference router      8096 ava bridge      8189 ava comfyui
PORTS="${AVA_SANDBOX_PORTS:-8080 8010 8096 8189}"
TAG="ava-sandbox-fw"
log() { echo "[ava-sandbox-fw] $*"; }

# Wait for docker AND for the network to exist; at boot we may beat both.
for i in $(seq 1 60); do
  if docker network inspect "$NET" >/dev/null 2>&1; then break; fi
  [ "$i" -eq 60 ] && { log "network '$NET' never appeared; nothing to do"; exit 0; }
  sleep 2
done

NETID=$(docker network inspect "$NET" -f '{{.Id}}' 2>/dev/null | cut -c1-12)
SUBNET=$(docker network inspect "$NET" -f '{{(index .IPAM.Config 0).Subnet}}' 2>/dev/null)
GW=$(docker network inspect "$NET" -f '{{(index .IPAM.Config 0).Gateway}}' 2>/dev/null)
IFACE="br-${NETID}"

if [ -z "$NETID" ] || [ -z "$SUBNET" ] || [ -z "$GW" ]; then
  log "could not read network '$NET' (id='$NETID' subnet='$SUBNET' gw='$GW')"; exit 1
fi
if ! ip link show "$IFACE" >/dev/null 2>&1; then
  log "bridge '$IFACE' is not present yet"; exit 1
fi
log "network=$NET iface=$IFACE subnet=$SUBNET gateway=$GW"

# 1) Drop OUR previous rules, whatever interface they named. This is the step
#    that makes a recreated network self-healing instead of silently broken.
#    Delete by RULE NUMBER, not by re-feeding an `iptables-save` line back in:
#    that line renders the comment as --comment "ava-sandbox-fw" WITH literal
#    quotes, and unquoted word-splitting passes the quotes through as part of
#    the comment, so the delete silently matches nothing.
removed=0
for _ in $(seq 1 64); do
  num=$(iptables -L INPUT --line-numbers -n 2>/dev/null \
        | awk -v tag="$TAG" '$0 ~ tag {print $1; exit}')
  [ -z "$num" ] && break
  iptables -D INPUT "$num" 2>/dev/null || break
  removed=$((removed+1))
done
[ "$removed" -gt 0 ] && log "removed $removed stale rule(s)"

# 2) Add the current ones.
for p in $PORTS; do
  iptables -I INPUT 1 -s "$SUBNET" -d "${GW}/32" -i "$IFACE" \
    -p tcp -m tcp --dport "$p" \
    -m comment --comment "$TAG" -j ACCEPT \
    && log "allow $SUBNET -> ${GW}:${p} in $IFACE" \
    || log "FAILED to add rule for port $p"
done

log "done"
