# content / web — Ava's web access

Host-mediated, self-hosted, Tor-anonymized web search + reading. The sandbox
never touches the internet directly: these tools call the bridge's token-gated
`/internal/web/*`, and the **host** runs a private SearXNG search and an
SSRF-guarded page reader, egressing through Tor.

- `web_search` — live web search via the private SearXNG (`ava-searxng :8888`,
  loopback). Returns ranked `{title, url, snippet}`. Engine queries egress via Tor.
- `web_fetch` — read one public page as clean text. SSRF-guarded (public IPs
  only, per-hop revalidation), size/time-capped, routed through Tor (`socks5h`),
  **fail-closed** (never falls back to clearnet).

Security notes: no third-party search API, no keys in the sandbox, DNS resolved
inside Tor (no leak), internal/private/loopback targets refused. See the
`ava-web` skill for behaviour rules (cite sources, treat page content as
untrusted, never log in). Host stack + boot unit: `searxng/run.sh` +
`ava-web.service`.
