// Shared helpers for Ava's MCP tool modules.
//
// Every tool module can import { http } from the server context instead of
// reimplementing networking. All outbound HTTP goes through `curl` so the
// sandbox guard proxy AND its TLS-MITM CA are honored — Node's built-in fetch
// ignores the proxy and gets blocked by the deny-by-default network guard.

import { execFile } from 'node:child_process';

// The guard proxy URL is delivered as `--proxy <url>` at launch (args are always
// passed, unlike env which gateway-spawned procs don't inherit). HTTPS_PROXY is
// a fallback for running outside the sandbox.
export function resolveArg(flag) {
  const i = process.argv.indexOf(flag);
  return (i >= 0 && process.argv[i + 1]) ? process.argv[i + 1] : '';
}

export function resolveProxy() {
  return resolveArg('--proxy') || process.env.HTTPS_PROXY || process.env.https_proxy || '';
}

export const PROXY = resolveProxy();
// Scoped bearer token for this MCP server's host-bridge callbacks, handed to us
// at launch (env doesn't survive gateway spawn). The bridge enforces route
// scopes, so this is not a general /internal/* credential.
export const INTERNAL_TOKEN = resolveArg('--internal-token') || process.env.AVA_INTERNAL_TOKEN || '';
// The guard's proxy does TLS interception with its own CA; trust it explicitly.
export const CA_BUNDLE =
  process.env.CURL_CA_BUNDLE || process.env.SSL_CERT_FILE || '/etc/openshell-tls/ca-bundle.pem';

// curl exit codes that mean the request didn't complete for a TRANSIENT reason
// (DNS, connect, TLS handshake, reset, empty reply, timeout) — safe to retry.
// The guard proxy + Tor path is occasionally flaky, so one blip shouldn't fail
// an otherwise-fine bridge/web call. HTTP errors (22, via --fail) are NOT here.
const RETRYABLE_CURL = new Set([6, 7, 28, 35, 52, 56]);

function run(args, timeout, tries = 3) {
  return new Promise((resolve, reject) => {
    const attempt = (n) => {
      execFile('/usr/bin/curl', args, { maxBuffer: 16 * 1024 * 1024 }, (err, stdout, stderr) => {
        if (err) {
          const code = typeof err.code === 'number' ? err.code : null;
          if (n < tries && code !== null && RETRYABLE_CURL.has(code)) {
            setTimeout(() => attempt(n + 1), 500 * n);   // brief backoff, fresh circuit
            return;
          }
          reject(new Error(`request failed: ${(stderr || err.message).trim()}`));
          return;
        }
        resolve(stdout);
      });
    };
    attempt(1);
  });
}

// GET JSON through the guard proxy (for internet services on :443).
// Set { direct: true } for host-local services reached via an /etc/hosts alias
// + policy (the proxy can't CONNECT to non-443 ports, so host services go direct).
export async function httpGetJson(url, { timeout = 12, direct = false, headers = {} } = {}) {
  const args = ['-s', '--max-time', String(timeout), '--fail', '--show-error'];
  if (!direct && PROXY) args.push('-x', PROXY);
  if (CA_BUNDLE) args.push('--cacert', CA_BUNDLE);
  for (const [k, v] of Object.entries(headers)) args.push('-H', `${k}: ${v}`);
  args.push(url);
  const out = await run(args, timeout);
  try { return JSON.parse(out); }
  catch (e) { throw new Error(`bad JSON from ${url}: ${e.message}`); }
}

// POST JSON. Defaults to a DIRECT route (host-local services like the bridge).
export async function httpPostJson(url, body, { timeout = 30, direct = true, headers = {} } = {}) {
  const args = ['-s', '--max-time', String(timeout), '--fail', '--show-error',
    '-H', 'Content-Type: application/json', '-d', JSON.stringify(body)];
  if (!direct && PROXY) args.push('-x', PROXY);
  if (CA_BUNDLE) args.push('--cacert', CA_BUNDLE);
  for (const [k, v] of Object.entries(headers)) args.push('-H', `${k}: ${v}`);
  args.push(url);
  const out = await run(args, timeout);
  try { return out ? JSON.parse(out) : {}; }
  catch (e) { throw new Error(`bad JSON from ${url}: ${e.message}`); }
}
