/**
 * The accent cascade, measured in a real browser against the BUILT bundle.
 *
 * A2 claims two things that only a browser can settle, because both are about
 * how CSS resolves rather than about what the source says:
 *
 *   1. An install that has never been re-branded is byte-for-byte the palette
 *      that shipped. The [data-brand] rules exist in the bundle but must never
 *      match, so --accent must still compute to #007acc.
 *   2. Setting one custom property recolours the whole token family, in both
 *      themes, including the tints that used to be hardcoded rgba literals.
 *
 * Asserting these from the CSS text would prove the rule was written, not that
 * it wins the cascade — and the light-theme rule specifically depends on
 * (0,3,0) beating (0,2,0), which is exactly the kind of claim that is wrong in
 * a way source-reading cannot see.
 *
 * Serves the built dist over a throwaway HTTP server rather than file://: the
 * built index.html references /assets/... with an ABSOLUTE path, which over
 * file:// resolves to the filesystem root and silently loads no stylesheet at
 * all — every custom property then reads back as "" and the whole spec passes
 * vacuously or fails for the wrong reason. No bridge is needed, only a static
 * server.
 */
import { chromium } from 'playwright';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import http from 'node:http';
import fs from 'node:fs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const DIST = path.resolve(HERE, '../../frontend/dist');

const TYPES: Record<string, string> = {
  '.html': 'text/html', '.css': 'text/css', '.js': 'text/javascript',
  '.json': 'application/json', '.webmanifest': 'application/manifest+json',
  '.png': 'image/png', '.ico': 'image/x-icon', '.svg': 'image/svg+xml',
};

function serve(): Promise<{ url: string; close: () => void }> {
  const srv = http.createServer((req, res) => {
    const rel = decodeURIComponent((req.url || '/').split('?')[0]);
    const file = path.join(DIST, rel === '/' ? 'index.html' : rel);
    if (!file.startsWith(DIST) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) {
      res.writeHead(404).end(); return;
    }
    res.writeHead(200, { 'Content-Type': TYPES[path.extname(file)] || 'application/octet-stream' });
    fs.createReadStream(file).pipe(res);
  });
  return new Promise((ok) => srv.listen(0, '127.0.0.1', () => {
    const port = (srv.address() as any).port;
    ok({ url: `http://127.0.0.1:${port}/`, close: () => srv.close() });
  }));
}

const read = (page: any, prop: string) =>
  page.evaluate(
    (p: string) =>
      getComputedStyle(document.documentElement).getPropertyValue(p).trim(),
    prop,
  );

async function main() {
  const server = await serve();
  const browser = await chromium.launch();
  const page = await browser.newPage();
  // The SPA's API calls will fail against a static server — irrelevant. Only the
  // stylesheet needs to load for a computed-style read on <html>.
  await page.goto(server.url).catch(() => {});
  await page.waitForFunction(
    () => getComputedStyle(document.documentElement).getPropertyValue('--accent').trim() !== '',
    null, { timeout: 10_000 },
  );

  const fails: string[] = [];
  const check = (name: string, got: string, want: string) => {
    if (got.toLowerCase() !== want.toLowerCase()) fails.push(`${name}: got ${got}, want ${want}`);
    else console.log(`  ok  ${name} = ${got}`);
  };

  // ---- 1. Unbranded is untouched -----------------------------------------
  await page.evaluate(() => {
    document.documentElement.setAttribute('data-theme', 'dark');
    document.documentElement.removeAttribute('data-brand');
  });
  check('dark  unbranded --accent', await read(page, '--accent'), '#007acc');
  check('dark  unbranded --accent-hover', await read(page, '--accent-hover'), '#0068ad');
  check('dark  unbranded --accent-tint', await read(page, '--accent-tint'), '#8ec9f0');

  await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'light'));
  check('light unbranded --accent', await read(page, '--accent'), '#006bb3');
  check('light unbranded --accent-tint', await read(page, '--accent-tint'), '#005a94');

  // ---- 2. One input recolours the family ---------------------------------
  await page.evaluate(() => {
    const d = document.documentElement;
    d.setAttribute('data-theme', 'dark');
    d.style.setProperty('--brand-accent', '#8b1a3d');
    d.style.setProperty('--brand-accent-light', '#a52348');
    d.setAttribute('data-brand', 'on');
  });
  check('dark  branded --accent', await read(page, '--accent'), '#8b1a3d');
  check('dark  branded --chart-1', await read(page, '--chart-1'), '#8b1a3d');

  // The derived tokens must have actually RESOLVED, not sat unresolved as the
  // literal string "color-mix(...)". getComputedStyle on a custom property
  // returns the substituted value, so a non-empty result that no longer names
  // the fallback blue is the proof the derivation ran.
  for (const p of ['--accent-hover', '--accent-soft', '--accent-tint', '--accent-line']) {
    const v = await read(page, p);
    if (!v) fails.push(`${p} resolved empty under a custom accent`);
    else if (v.includes('007acc') || v.includes('0,122,204')) {
      fails.push(`${p} still carries the shipped blue: ${v}`);
    } else console.log(`  ok  dark  branded ${p} = ${v}`);
  }

  // ---- 3. The light rule must WIN — this is the (0,3,0) vs (0,2,0) claim --
  await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'light'));
  check('light branded --accent', await read(page, '--accent'), '#a52348');

  // ---- 4. A converted stray literal now follows the accent ---------------
  // .badge-turn was `background: rgba(0,122,204,.18)` — a blue that no re-brand
  // could reach. It is the canary for the other seven.
  const badge = await page.evaluate(() => {
    const el = document.createElement('span');
    el.className = 'badge-turn';
    document.body.appendChild(el);
    const bg = getComputedStyle(el).backgroundColor;
    el.remove();
    return bg;
  });
  console.log(`  .badge-turn background under a custom accent = ${badge}`);
  if (badge.includes('0, 122, 204')) fails.push(`.badge-turn still hardcodes the shipped blue: ${badge}`);

  await browser.close();
  server.close();
  if (fails.length) {
    console.error('\nFAILURES:\n  ' + fails.join('\n  '));
    process.exit(1);
  }
  console.log('\nall brand-token assertions passed');
}

main();
