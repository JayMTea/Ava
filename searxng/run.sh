#!/usr/bin/env bash
# Recreate Ava's private, anonymized web-egress stack — fully under your control:
#
#   ava-web (docker network, internal)
#     ├─ ava-tor      : Tor SOCKS5 proxy, published to 127.0.0.1:9050 (loopback)
#     └─ ava-searxng  : SearXNG search, published to 127.0.0.1:8888 (loopback),
#                       all engine traffic egresses through ava-tor (settings.yml)
#
# Both are loopback-only and reached ONLY by the host bridge (ava_bridge/web.py).
# Ava's sandbox never talks to them directly, no API keys are used, and ALL
# outbound web traffic (search engines AND page fetches) exits via Tor, so sites
# see a Tor exit IP — never your real one. settings.yml holds a secret_key and is
# gitignored; a fresh one is generated if missing.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NET="ava-web"
TOR="ava-tor"
SEARX="ava-searxng"
SEARX_PORT="${AVA_SEARXNG_PORT:-8888}"
TOR_PORT="${AVA_TOR_PORT:-9050}"
SETTINGS="$HERE/settings.yml"

if [ ! -f "$SETTINGS" ]; then
  echo "[web] generating settings.yml with a fresh secret_key + Tor egress"
  SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))' 2>/dev/null \
            || openssl rand -hex 32)"
  cat > "$SETTINGS" <<YAML
use_default_settings: true
server:
  secret_key: "${SECRET}"
  limiter: false
  image_proxy: false
search:
  safe_search: 0
  formats:
    - html
    - json
# ALL engine traffic egresses through Tor (fail-closed: if Tor is down, queries
# error rather than leaking your real IP).
outgoing:
  request_timeout: 15.0
  using_tor_proxy: true
  proxies:
    "all://":
      - socks5h://${TOR}:9050
ui:
  static_use_hash: true
YAML
  chmod 600 "$SETTINGS"
fi

# 1) Private network
docker network inspect "$NET" >/dev/null 2>&1 || { echo "[web] creating network $NET"; docker network create "$NET" >/dev/null; }

# 2) Tor SOCKS proxy (loopback-published for the host bridge; on $NET for SearXNG)
echo "[web] (re)starting $TOR on 127.0.0.1:${TOR_PORT}"
docker rm -f "$TOR" >/dev/null 2>&1 || true
docker run -d --name "$TOR" --restart unless-stopped --network "$NET" \
  -p "127.0.0.1:${TOR_PORT}:9050" \
  osminogin/tor-simple:latest >/dev/null

echo "[web] waiting for Tor bootstrap…"
for i in $(seq 1 20); do
  sleep 4
  if curl -s --max-time 15 --socks5-hostname "127.0.0.1:${TOR_PORT}" https://check.torproject.org/api/ip 2>/dev/null | grep -q '"IsTor":true'; then
    echo "[web] Tor up (circuit confirmed) on 127.0.0.1:${TOR_PORT}"; break
  fi
  echo "  bootstrapping Tor… ($i)"
  [ "$i" = "20" ] && { echo "[web] ERROR: Tor did not bootstrap — check: docker logs $TOR" >&2; exit 1; }
done

# 3) SearXNG (egresses through ava-tor via settings.yml)
echo "[web] (re)starting $SEARX on 127.0.0.1:${SEARX_PORT}"
docker rm -f "$SEARX" >/dev/null 2>&1 || true
docker run -d --name "$SEARX" --restart unless-stopped --network "$NET" \
  -p "127.0.0.1:${SEARX_PORT}:8080" \
  -v "$SETTINGS:/etc/searxng/settings.yml:ro" \
  searxng/searxng:latest >/dev/null

echo "[web] waiting for SearXNG JSON API…"
for i in $(seq 1 15); do
  sleep 3
  code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${SEARX_PORT}/search?q=ping&format=json" || true)"
  if [ "$code" = "200" ]; then echo "[web] SearXNG up (json=200) on 127.0.0.1:${SEARX_PORT}"; echo "[web] stack ready — search + fetch now exit via Tor."; exit 0; fi
done
echo "[web] WARNING: SearXNG JSON API did not return 200 — check: docker logs $SEARX" >&2
exit 1
