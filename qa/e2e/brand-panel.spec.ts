/**
 * The Branding panel, driven against a real bridge.
 *
 * Everything up to now seeded localStorage or ava.yaml by hand. This is the
 * first test where a human-shaped interaction — type a colour, press Save —
 * has to produce a re-branded app, which is the actual feature.
 *
 * Uses a throwaway AVA_HOME and the dev port range, never :8096.
 */
import { chromium } from 'playwright';

const BASE = process.env.AVA_E2E_BASE || 'http://127.0.0.1:8397';
const PASSWORD = process.env.AVA_E2E_PASSWORD || 'testpass123';

/** Land on the app, let its hash effect settle, then click through to the tab. */
async function openBranding(page: any) {
  await page.goto(BASE, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('text=Vitals', { timeout: 15_000 });
  await page.evaluate(() => { window.location.hash = 'hub'; });
  await page.waitForSelector('.hub-tabs button:has-text("Branding")', { timeout: 15_000 });
  await page.click('.hub-tabs button:has-text("Branding")');
  await page.waitForSelector('text=Make it yours', { timeout: 15_000 });
}

async function main() {
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ colorScheme: 'dark' });
  const page = await ctx.newPage();
  const fails: string[] = [];
  const ok = (m: string) => console.log(`  ok  ${m}`);

  // ---- sign in ------------------------------------------------------------
  await page.goto(`${BASE}/login`);
  await page.fill('input[name="password"]', PASSWORD);
  await Promise.all([page.waitForNavigation(), page.click('button[type="submit"]')]);

  // ---- the tab exists -----------------------------------------------------
  // NOT `goto(BASE + '/#hub/branding')`. App.tsx reflects its view into the hash
  // on mount (`if (viewFromHash() !== view) window.location.hash = view`), which
  // races a goto that differs only by hash and rewrites it to `#vitals`. Land on
  // the app, let it settle, then click — which is what a person does anyway.
  await openBranding(page);
  ok('Setup → Branding renders');

  // ---- live preview is DOM-only and must not persist ----------------------
  // Snapshot the cache BEFORE typing and compare, rather than substring-matching
  // for the new colour: an earlier run that left the same colour saved would
  // make a substring check fail for a reason that has nothing to do with the
  // preview. Comparing before/after asks the actual question — did previewing
  // write anything?
  const cacheBefore = await page.evaluate(() => localStorage.getItem('ava.brand'));

  const hexInput = page.locator('input.hub-input[placeholder="#007acc"]');
  await hexInput.fill('#2f7d4f');
  await page.waitForTimeout(300);   // the 80ms debounce, generously
  const previewed = await page.evaluate(() =>
    getComputedStyle(document.documentElement).getPropertyValue('--accent').trim());
  if (previewed !== '#2f7d4f') fails.push(`preview did not apply: --accent=${previewed}`);
  else ok(`live preview applied --accent = ${previewed} with no save`);

  const cacheAfter = await page.evaluate(() => localStorage.getItem('ava.brand'));
  if (cacheAfter !== cacheBefore) {
    fails.push('preview wrote to localStorage — a refused colour would survive a refresh');
  } else ok('preview did NOT persist');

  // ---- save --------------------------------------------------------------
  await page.click('button.hub-btn.primary');
  await page.waitForTimeout(1500);
  const afterSave = await page.evaluate(() => localStorage.getItem('ava.brand'));
  if (!afterSave || !afterSave.includes('2f7d4f')) fails.push(`save did not cache the brand: ${afterSave}`);
  else ok('save persisted the brand');

  // ---- it survives a full reload, before any fetch ------------------------
  // The delay is the whole point: it must still be pending when the assertion
  // runs. try/catch because the navigation below can retire the route while the
  // timer is still going, and Playwright throws "Route is already handled" —
  // which is harness noise, not a product failure.
  await page.route('**/api/brand', async (route) => {
    await new Promise((r) => setTimeout(r, 3000));
    try { await route.continue(); } catch { /* navigated away mid-delay */ }
  });
  await page.goto(`${BASE}/`, { waitUntil: 'domcontentloaded' });
  const afterReload = await page.evaluate(() =>
    getComputedStyle(document.documentElement).getPropertyValue('--accent').trim());
  if (afterReload !== '#2f7d4f') fails.push(`after reload --accent=${afterReload}, want #2f7d4f`);
  else ok(`survives reload, applied before the (3s-delayed) fetch: ${afterReload}`);
  await page.unroute('**/api/brand');

  // ---- an unreadable colour is refused, with a usable fix -----------------
  await openBranding(page);
  await page.locator('input.hub-input[placeholder="#007acc"]').fill('#ffd93d');
  await page.waitForTimeout(300);
  await page.click('button.hub-btn.primary');
  await page.waitForTimeout(1200);
  const body = await page.textContent('body');
  if (!/unreadable|not readable/i.test(body || '')) {
    fails.push('an unreadable accent was not refused in the UI');
  } else ok('unreadable accent refused in the UI');
  const suggestBtn = page.locator('button.hub-btn', { hasText: /Use #/ });
  if (await suggestBtn.count() === 0) fails.push('no suggestion offered on refusal');
  else ok(`suggestion offered: ${(await suggestBtn.first().textContent())?.trim()}`);

  // ---- reset ---------------------------------------------------------------
  // Re-open the panel first: the previous step left a REFUSED colour in the
  // form, and reset must work from that state (which is when people reach for
  // it). The dialog handler is armed before the click that triggers it.
  page.on('dialog', (d) => d.accept());
  await page.click('button.hub-btn.ghost:has-text("Reset")');
  await page.waitForTimeout(2000);
  const serverAfterReset = await page.evaluate(async () =>
    (await (await fetch('/api/hub/branding', { credentials: 'same-origin' })).json()).accent);
  if (serverAfterReset !== '') fails.push(`server still holds accent=${JSON.stringify(serverAfterReset)} after reset`);
  else ok('reset cleared the accent server-side');
  const afterReset = await page.evaluate(() =>
    getComputedStyle(document.documentElement).getPropertyValue('--accent').trim());
  if (afterReset !== '#007acc') fails.push(`reset left --accent=${afterReset}, want #007acc`);
  else ok(`reset restored Ava's shipped accent = ${afterReset}`);

  await browser.close();
  if (fails.length) {
    console.error('\nFAILURES:\n  ' + fails.join('\n  '));
    process.exit(1);
  }
  console.log('\nbranding panel: drive it like a person and the app re-brands');
}

main();
