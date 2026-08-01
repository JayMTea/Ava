// E2E: login through the real form, send a chat message in the real SPA,
// see the fake model's reply render. The full user loop, zero mocks.
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

  // Login page (instance was set up by setup-flow.spec.ts).
  await page.goto(BRIDGE + '/');
  check('login page shown', page.url().endsWith('/login'), page.url());
  await page.fill('input[name=password]', PASSWORD);
  await Promise.all([page.waitForURL((u) => !u.pathname.includes('/login')),
                     page.keyboard.press('Enter')]);
  check('logged in', !page.url().includes('/login'), page.url());

  // The composer lives on the chat view. (Landing is Setup; the walkthrough
  // was marked seen by setup-flow.spec.ts, which runs first — without that the
  // overlay puts #root in `inert` and nothing below can type.)
  await page.evaluate(() => { location.hash = 'chat'; });
  const composer = page.locator('textarea#text');
  await composer.waitFor({ timeout: 20000 });
  check('composer present', true);

  await composer.fill('Hello Ava, quick smoke check?');
  await page.keyboard.press('Enter');

  // The fake model's canned reply must render in the conversation.
  const reply = page.getByText('QA fake model', { exact: false }).first();
  let ok = true;
  await reply.waitFor({ timeout: 45000 }).catch(() => { ok = false; });
  check('assistant reply rendered', ok);

  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
