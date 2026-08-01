// E2E: the first-run flow in a real browser against the real bridge.
// Fresh instance -> /setup password screen -> onboarding wizard -> dashboard.
import { chromium } from 'playwright';

const BRIDGE = process.env.BRIDGE_URL!;
const PASSWORD = process.env.QA_PASSWORD!;

function check(name: string, ok: boolean, extra = '') {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${extra ? ` — ${extra}` : ''}`);
  if (!ok) process.exitCode = 1;
}

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage();

  // A fresh install lands on the password screen, wherever you aim.
  await page.goto(BRIDGE + '/');
  check('redirected to /setup', page.url().endsWith('/setup'));

  // Weak password: the form's native minlength gate blocks the submit
  // client-side (the server enforces the same rule — tier 1 proves that).
  await page.fill('input[name=password]', 'short');
  await page.fill('input[name=confirm]', 'short');
  const validity = await page.evaluate(() =>
    (document.querySelector('input[name=password]') as HTMLInputElement).checkValidity());
  check('weak password blocked by the form', validity === false);

  // Real password -> the onboarding wizard.
  await page.fill('input[name=password]', PASSWORD);
  await page.fill('input[name=confirm]', PASSWORD);
  await Promise.all([page.waitForURL('**/setup/wizard'),
                     page.click('button[type=submit]')]);
  check('entered wizard', page.url().includes('/setup/wizard'), page.url());

  // Complete the wizard through its own API (the UI steps proxy to this),
  // then the app shell must load.
  const save = await page.evaluate(async () => {
    const r = await fetch('/api/setup/save', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        inference: { mode: 'local', engine: 'vllm',
                     base_url: 'http://127.0.0.1:9999/v1', model: 'qa-model' },
        features: { voice: false, web_search: false, image: true },
        connectors: [],
      }),
    });
    return r.status;
  });
  check('wizard save accepted', save === 200, String(save));

  await page.goto(BRIDGE + '/');
  await page.waitForSelector('#root, .app, main, textarea', { timeout: 15000 })
    .catch(() => {});
  const html = await page.content();
  check('SPA shell served after onboarding', html.toLowerCase().includes('<div'));
  check('no longer redirected to setup', !page.url().includes('/setup'));

  // A bare `/` must land on Setup, not Vitals. This is the whole point of the
  // landing change: the wizard redirects here with no hash, and a metrics
  // dashboard is the least explicable screen to someone who just installed.
  await page.waitForFunction(() => location.hash !== '', null, { timeout: 10000 })
    .catch(() => {});
  check('lands on Setup, not Vitals', page.url().endsWith('#hub'), page.url());

  // The first-run walkthrough must actually run on a fresh install.
  const card = page.locator('.tour-card');
  await card.waitFor({ timeout: 10000 }).catch(() => {});
  check('walkthrough appears on first run', await card.count() > 0);

  // Then get it out of the way for every spec that follows. It is server-side
  // state, so this holds for the rest of the ordered run — and it must, because
  // the overlay puts #root in `inert` and a spec cannot type through that.
  const dismissed = await page.evaluate(async () => {
    const r = await fetch('/api/hub/tour');
    const pages: string[] = (await r.json()).pages || [];
    for (const p of pages) {
      await fetch(`/api/hub/tour/seen?page=${encodeURIComponent(p)}`, { method: 'POST' });
    }
    return (await (await fetch('/api/hub/tour')).json()).seen.length;
  });
  check('walkthrough dismissed for the rest of the run', dismissed > 0, `${dismissed} pages`);

  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
