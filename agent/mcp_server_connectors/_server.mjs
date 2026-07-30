#!/usr/bin/env node
// Ava's modular MCP tool server — zero dependencies, speaks MCP over stdio
// (newline-delimited JSON-RPC 2.0). Runs INSIDE the OpenClaw sandbox; the gateway
// spawns it and exposes its tools to Ava so she can call them natively.
//
// WHO IS WHO (MCP roles are per-connection, not identities):
//   - MCP HOST   = the OpenClaw agent runtime (the 120B model + Ava persona +
//                  gating/policy). It CONSUMES tools. "Ava the agent" lives here.
//   - MCP CLIENT = the connector OpenClaw opens to us, registered in config as
//                  mcp.servers["ava-tools"].
//   - MCP SERVER = THIS process. It only SERVES tools; it is not "Ava".
//   So Ava (host) calls these tools; she does not host them. The Ava repo
//   is neither host nor server — it is the source/config kit that declares
//   these tools and is pushed into the sandbox by ../install.sh.
//   Tools are adapters: e.g. a media tool reaches into a downstream app
//   (like the GPU service), which is a downstream service, not a host.
//
// MODULAR BY DESIGN: this harness recursively auto-loads every tool module under
// this directory, including category subfolders (persona/, daily/, health/, …).
// It loads any *.mjs whose name does NOT start with "_" (and skips dotfiles); the
// subfolder is purely organizational — a tool's identity is its `name` field, not
// its path. To add a capability, drop one module that default-exports
// { name, description, inputSchema, handler } into a category folder — no edits
// here. Use ../new-tool.sh <name> --category <cat> to scaffold one.
//
// Each handler receives (args, ctx) where ctx = { http, proxy, caBundle }.
//   ctx.http.getJson(url, opts)   — GET JSON (through guard proxy by default)
//   ctx.http.postJson(url, body)  — POST JSON (direct host route by default)

import { readdirSync } from 'node:fs';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { dirname, join, relative } from 'node:path';
import * as lib from './_lib.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const SERVER_INFO = { name: 'ava-tools-connectors', version: '2.0.0' };
const ctx = {
  http: { getJson: lib.httpGetJson, postJson: lib.httpPostJson },
  proxy: lib.PROXY,
  caBundle: lib.CA_BUNDLE,
  internalToken: lib.INTERNAL_TOKEN,
};

// ---- Auto-discover tool modules (recursively, by category subfolder) --------
// Walk the tree and load every *.mjs not starting with "_"/".". The category
// folder it lives in is organizational only; the tool is keyed by its `name`.
function collectModules(dir) {
  const found = [];
  for (const ent of readdirSync(dir, { withFileTypes: true })
      .sort((a, b) => a.name.localeCompare(b.name))) {
    if (ent.name.startsWith('_') || ent.name.startsWith('.')) continue;
    const full = join(dir, ent.name);
    if (ent.isDirectory()) found.push(...collectModules(full));
    else if (ent.name.endsWith('.mjs')) found.push(full);
  }
  return found;
}

const TOOLS = {};
for (const full of collectModules(HERE)) {
  const rel = relative(HERE, full);
  let mod;
  try { mod = await import(pathToFileURL(full).href); }
  catch (e) { process.stderr.write(`[ava-tools-connectors] failed to load ${rel}: ${e.message}\n`); continue; }
  const t = mod.default;
  if (!t || !t.name || typeof t.handler !== 'function' || !t.inputSchema) {
    process.stderr.write(`[ava-tools-connectors] skipping ${rel}: must default-export { name, description, inputSchema, handler }\n`);
    continue;
  }
  if (TOOLS[t.name]) process.stderr.write(`[ava-tools-connectors] WARNING: duplicate tool name "${t.name}" (${rel}) overrides earlier\n`);
  TOOLS[t.name] = t;
}
process.stderr.write(`[ava-tools-connectors] loaded ${Object.keys(TOOLS).length} tool(s): ${Object.keys(TOOLS).join(', ')}\n`);

// ---- Minimal MCP stdio (newline-delimited JSON-RPC 2.0) --------------------
function send(msg) { process.stdout.write(JSON.stringify(msg) + '\n'); }
function reply(id, result) { send({ jsonrpc: '2.0', id, result }); }
function fail(id, code, message) { send({ jsonrpc: '2.0', id, error: { code, message } }); }

async function handle(msg) {
  const { id, method, params } = msg;
  if (method === 'initialize') {
    reply(id, {
      protocolVersion: (params && params.protocolVersion) || '2024-11-05',
      capabilities: { tools: {} },
      serverInfo: SERVER_INFO,
    });
    return;
  }
  if (method === 'notifications/initialized' || method === 'initialized') return; // notification
  if (method === 'ping') { reply(id, {}); return; }
  if (method === 'tools/list') {
    reply(id, {
      tools: Object.values(TOOLS).map((t) => ({
        name: t.name, description: t.description, inputSchema: t.inputSchema,
      })),
    });
    return;
  }
  if (method === 'tools/call') {
    const name = params && params.name;
    const tool = TOOLS[name];
    if (!tool) { fail(id, -32602, `unknown tool: ${name}`); return; }
    try {
      const text = await tool.handler(params.arguments || {}, ctx);
      reply(id, { content: [{ type: 'text', text }] });
    } catch (e) {
      reply(id, { content: [{ type: 'text', text: `Tool error: ${e.message}` }], isError: true });
    }
    return;
  }
  if (id !== undefined) fail(id, -32601, `method not found: ${method}`);
}

let buf = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (chunk) => {
  buf += chunk;
  let nl;
  while ((nl = buf.indexOf('\n')) >= 0) {
    const line = buf.slice(0, nl).trim();
    buf = buf.slice(nl + 1);
    if (!line) continue;
    let m; try { m = JSON.parse(line); } catch { continue; }
    Promise.resolve(handle(m)).catch(() => {});
  }
});
process.stdin.on('end', () => process.exit(0));
