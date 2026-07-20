// E2E: Vitals / Operations / Data views rendered from the REAL API.
// This is the drift-catcher the mock-based verify scripts can't be:
// if a live endpoint's shape breaks a view, it fails here.
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
  const crashes: string[] = [];
  page.on('pageerror', (e) => crashes.push(String(e)));

  await page.goto(BRIDGE + '/login');
  await page.fill('input[name=password]', PASSWORD);
  await Promise.all([page.waitForURL((u) => !u.pathname.includes('/login')),
                     page.keyboard.press('Enter')]);

  for (const [hash, marker] of [
    ['vitals', 'Vitals'], ['ops', 'Operations'], ['data', 'Data'],
  ] as const) {
    await page.evaluate((h) => { location.hash = h; }, hash);
    const heading = page.getByText(marker, { exact: false }).first();
    let ok = true;
    await heading.waitFor({ timeout: 20000 }).catch(() => { ok = false; });
    check(`#${hash} view renders`, ok);
  }

  // Vitals specifics against live data: KPI strip + Connected apps panel.
  await page.evaluate(() => { location.hash = 'vitals'; });
  const kpi = page.getByText('Throughput', { exact: false }).first();
  let ok = true;
  await kpi.waitFor({ timeout: 15000 }).catch(() => { ok = false; });
  check('Vitals KPI strip renders from real /api/perf', ok);
  const apps = page.getByText('Connected apps', { exact: false }).first();
  ok = true;
  await apps.waitFor({ timeout: 15000 }).catch(() => { ok = false; });
  check('Connected apps panel present', ok);

  check('no uncaught page errors across views', crashes.length === 0,
        crashes.join(' | ').slice(0, 300));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
