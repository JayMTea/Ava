// E2E: connect a real (fake) app through the live API, then confirm the UI
// reflects it — the app appears in the sidebar with a live health dot.
import { chromium } from 'playwright';

const BRIDGE = process.env.BRIDGE_URL!;
const PASSWORD = process.env.QA_PASSWORD!;
const FAKE_APP = process.env.FAKE_APP_URL!;

function check(name: string, ok: boolean, extra = '') {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${extra ? ` — ${extra}` : ''}`);
  if (!ok) process.exitCode = 1;
}

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage();

  await page.goto(BRIDGE + '/login');
  await page.fill('input[name=password]', PASSWORD);
  await Promise.all([page.waitForURL((u) => !u.pathname.includes('/login')),
                     page.keyboard.press('Enter')]);

  // Connect the app the way the Hub's form does (probe -> new).
  const result = await page.evaluate(async (appUrl) => {
    const probe = await fetch('/api/hub/connectors/probe', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url: appUrl }),
    });
    const created = await fetch('/api/hub/connectors/new', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        id: 'qae2eapp', label: 'QA E2E App', kind: 'app',
        base_url: appUrl, probe: appUrl + '/health',
        // `ui: true` is what asks for the sidebar tile this spec then waits
        // for: the manifest gets a `ui:` block only when the create requests a
        // web UI, and the rail renders nothing without one.
        ui: true,
        actions: [{ id: 'ping', method: 'GET', path: '/api/ping', access: 'read' }],
      }),
    });
    return { probe: probe.status, created: created.status };
  }, FAKE_APP);
  check('probe answered', result.probe === 200, String(result.probe));
  check('connector created', result.created === 200, String(result.created));

  // The app shows up in the sidebar — live registry, no restart.
  const card = page.getByText('QA E2E App', { exact: false }).first();
  let ok = true;
  await card.waitFor({ timeout: 30000 }).catch(() => { ok = false; });
  check('new app visible in the sidebar', ok);

  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
