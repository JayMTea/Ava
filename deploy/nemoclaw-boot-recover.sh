#!/usr/bin/env bash
# nemoclaw-boot-recover.sh — Restore the NemoClaw sandbox forwards on boot.
#
# WHAT CHANGED (2026-08-23): this script used to try
#     openshell gateway start --name nemoclaw >/dev/null 2>&1 || true
# to bring up the shared host gateway. That subcommand DOES NOT EXIST — openshell
# 0.0.85 offers only add/remove/login/logout/select/info/list under `gateway` —
# and the `|| true` swallowed the "unrecognized subcommand" every single boot.
# So the gateway was never started, and `nemoclaw recover` then failed five
# times with "transport error / Connection refused", which is precisely what it
# warns about: "this sandbox-scoped command will not restart the shared host
# gateway".
#
# The host gateway is now supervised by openshell-gateway.service (a user unit),
# because the only thing in the NemoClaw source that ever launches it is
# `onboard`, and it launches it DETACHED with no supervisor. This script's job
# is therefore only to wait for that gateway and restore the sandbox forwards.
set -uo pipefail

export PATH="$HOME/.local/bin:$HOME/.npm-global/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

SANDBOX="${AVA_OC_SANDBOX:-my-assistant}"
GW_PORT="${OPENSHELL_SERVER_PORT:-8080}"
log() { echo "[nemoclaw-boot-recover] $*"; }

# 1) Wait for the Docker daemon to be responsive.
for i in $(seq 1 60); do
  docker info >/dev/null 2>&1 && break
  log "waiting for docker ($i/60)"; sleep 5
done

# 2) Wait for the host OpenShell gateway to accept connections. Without this the
#    recover below cannot succeed, and retrying it is pure noise.
for i in $(seq 1 60); do
  if (exec 3<>/dev/tcp/127.0.0.1/"$GW_PORT") 2>/dev/null; then
    exec 3>&- 3<&- 2>/dev/null || true
    log "host gateway is listening on 127.0.0.1:$GW_PORT"
    break
  fi
  if [ "$i" -eq 1 ]; then
    systemctl --user start openshell-gateway.service 2>/dev/null || true
  fi
  log "waiting for host gateway ($i/60)"; sleep 5
done

# 3) Ensure the sandbox container is RUNNING — actively, not by waiting.
#    Its restart policy is `unless-stopped`, which brings it back after a reboot
#    but deliberately does NOT restart a container that was stopped explicitly.
#    A gateway cutover stops it exactly that way, so the old passive wait sat
#    through all 60 attempts and then handed `recover` a container that was
#    never going to appear. `nemoclaw <name> start` is the documented way to
#    restart a stopped sandbox, and it also re-probes the host forwards.
for i in $(seq 1 30); do
  cid=$(docker ps --filter "name=openshell-${SANDBOX}" --filter status=running -q | head -1)
  [ -n "$cid" ] && { log "sandbox container is running"; break; }
  if [ "$i" -eq 1 ] || [ $(( i % 6 )) -eq 0 ]; then
    log "sandbox container not running — asking nemoclaw to start it"
    nemoclaw "$SANDBOX" start >/dev/null 2>&1 || true
  fi
  log "waiting for sandbox container ($i/30)"; sleep 5
done

# 4) Restore the sandbox gateway + dashboard port-forward, with retries.
for i in $(seq 1 5); do
  if nemoclaw "$SANDBOX" recover; then
    log "recover succeeded on attempt $i"
    exit 0
  fi
  log "recover attempt $i/5 failed; retrying in 5s"; sleep 5
done

log "recover failed after 5 attempts"
exit 1
