/**
 * The flash-free claim, measured the only way that actually proves it.
 *
 * The failure mode this guards is subtle: if you seed a brand, load the page,
 * wait for the app to settle, and THEN assert --accent, you have proved nothing
 * — the fetch has long since resolved and applied it. The page could have
 * painted default blue for 400ms and the assertion would still pass.
 *
 * So: the brand endpoint is DELAYED by 3 seconds, and the assertion runs
 * immediately after domcontentloaded. Only the pre-paint script in index.html
 * can satisfy that, because nothing else has run yet.
 *
 * Also covers the honest limitation: a browser profile with nothing cached
 * paints Ava's default for one frame. That is asserted too, so the tradeoff is
 * documented as behaviour rather than as a comment someone can quietly break.
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

const BRAND = {
  name: 'Northwind', tagline: 'the shop assistant',
  accent: '#8b1a3d', accent_light: '#a52348', chrome: '#120a0d',
  branded: true, assets: {},
};

async function main() {
  const server = await serve();
  const browser = await chromium.launch();
  const fails: string[] = [];

  // ---- 1. Cached brand + a DELIBERATELY SLOW endpoint --------------------
  {
    const ctx = await browser.newContext({ colorScheme: 'dark' });
    const page = await ctx.newPage();
    // 3s is far longer than any paint. If --accent is right before this
    // resolves, only the pre-paint script can have set it.
    await page.route('**/api/brand', async (route) => {
      await new Promise((r) => setTimeout(r, 3000));
      await route.fulfill({ json: BRAND });
    });
    await page.addInitScript((b) => {
      localStorage.setItem('ava.brand', JSON.stringify(b));
    }, BRAND);

    await page.goto(server.url, { waitUntil: 'domcontentloaded' });
    const accent = await page.evaluate(() =>
      getComputedStyle(document.documentElement).getPropertyValue('--accent').trim());
    const attr = await page.evaluate(() => document.documentElement.getAttribute('data-brand'));
    const title = await page.evaluate(() => document.title);
    const themeColor = await page.evaluate(() =>
      document.querySelector('meta[name="theme-color"]')?.getAttribute('content'));

    console.log(`  at domcontentloaded (brand fetch still 3s away):`);
    console.log(`    --accent    = ${accent}`);
    console.log(`    data-brand  = ${attr}`);
    console.log(`    title       = ${title}`);
    console.log(`    theme-color = ${themeColor}`);
    if (accent !== '#8b1a3d') fails.push(`--accent was ${accent}, want #8b1a3d BEFORE the fetch`);
    if (attr !== 'on') fails.push(`data-brand was ${attr}, want "on"`);
    if (title !== 'Northwind') fails.push(`title was ${title}`);
    if (themeColor !== '#120a0d') fails.push(`theme-color was ${themeColor}`);
    await ctx.close();
  }

  // ---- 2. A cold profile paints Ava's default (the honest limitation) ----
  {
    const ctx = await browser.newContext({ colorScheme: 'dark' });
    const page = await ctx.newPage();
    await page.route('**/api/brand', async (route) => {
      await new Promise((r) => setTimeout(r, 3000));
      await route.fulfill({ json: BRAND });
    });
    await page.goto(server.url, { waitUntil: 'domcontentloaded' });
    const accent = await page.evaluate(() =>
      getComputedStyle(document.documentElement).getPropertyValue('--accent').trim());
    console.log(`  cold profile, nothing cached: --accent = ${accent} (Ava's default, as documented)`);
    if (accent !== '#007acc') fails.push(`cold profile --accent was ${accent}, want #007acc`);
    await ctx.close();
  }

  // ---- 3. Clearing the accent restores the shipped palette ---------------
  {
    const ctx = await browser.newContext({ colorScheme: 'dark' });
    const page = await ctx.newPage();
    await page.route('**/api/brand', (route) => route.fulfill({ json: BRAND }));
    await page.addInitScript((b) => {
      localStorage.setItem('ava.brand', JSON.stringify(b));
    }, BRAND);
    await page.goto(server.url, { waitUntil: 'domcontentloaded' });
    // Reset-to-default posts "" for every field; applyBrand must then REMOVE the
    // attribute, which is what makes the shipped palette reappear.
    const after = await page.evaluate(() => {
      const d = document.documentElement;
      d.removeAttribute('data-brand');
      d.style.removeProperty('--brand-accent');
      return getComputedStyle(d).getPropertyValue('--accent').trim();
    });
    console.log(`  after clearing the brand: --accent = ${after}`);
    if (after !== '#007acc') fails.push(`clearing left --accent at ${after}, want #007acc`);
    await ctx.close();
  }

  // ---- 4. The light-theme brand accent is pre-paint too ------------------
  // Not a bonus case: headless Chromium defaults to prefers-color-scheme LIGHT,
  // so an unpinned run tests this branch by accident. Pinning both makes which
  // branch is under test explicit rather than ambient.
  {
    const ctx = await browser.newContext({ colorScheme: 'light' });
    const page = await ctx.newPage();
    await page.route('**/api/brand', async (route) => {
      await new Promise((r) => setTimeout(r, 3000));
      await route.fulfill({ json: BRAND });
    });
    await page.addInitScript((b) => {
      localStorage.setItem('ava.brand', JSON.stringify(b));
    }, BRAND);
    await page.goto(server.url, { waitUntil: 'domcontentloaded' });
    const accent = await page.evaluate(() =>
      getComputedStyle(document.documentElement).getPropertyValue('--accent').trim());
    console.log(`  light scheme, cached brand: --accent = ${accent}`);
    if (accent !== '#a52348') fails.push(`light --accent was ${accent}, want #a52348 BEFORE the fetch`);
    await ctx.close();
  }

  await browser.close();
  server.close();
  if (fails.length) {
    console.error('\nFAILURES:\n  ' + fails.join('\n  '));
    process.exit(1);
  }
  console.log('\nflash-free: the brand is applied before the fetch resolves');
}

main();
