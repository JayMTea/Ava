// E2E: login through the real form, send a chat message in the real SPA,
// see the fake model's reply render. The full user loop, zero mocks.
import { chromium } from 'playwright';
import { mkdir } from 'node:fs/promises';

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
  await page.locator('a[href="#chat"]:visible').first().click();
  const composer = page.locator('textarea#text');
  await composer.waitFor({ timeout: 20000 });
  check('composer present', true);

  await composer.fill('Hello Ava, quick smoke check?');
  await page.getByRole('button', { name: 'Send', exact: true }).click();

  // The fake model's canned reply must render in the conversation.
  const reply = page.getByText('QA fake model', { exact: false }).first();
  let ok = true;
  await reply.waitFor({ timeout: 45000 }).catch(() => { ok = false; });
  check('assistant reply rendered', ok);

  // The sidebar action clears the whole saved history, even with a search
  // active. Cancel and a failed request must both preserve the conversation.
  const clear = page.getByRole('button', { name: 'Clear all chats', exact: true });
  if (!(await clear.isVisible())) {
    await page.getByRole('button', { name: 'Open sidebar', exact: true }).click();
  }
  await clear.waitFor();
  await page.waitForFunction(() => !document.querySelector<HTMLButtonElement>('.draw-clear')?.disabled);
  const before = await page.request.get(BRIDGE + '/api/chats').then(r => r.json());
  page.once('dialog', dialog => dialog.dismiss());
  await clear.click();
  check('cancel preserves saved chats', (await page.request.get(BRIDGE + '/api/chats').then(r => r.json())).chats.length === before.chats.length);

  await page.route('**/api/chats', async route => {
    if (route.request().method() === 'DELETE') {
      await route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ error: 'Temporary failure' }) });
    } else await route.continue();
  });
  page.once('dialog', dialog => dialog.accept());
  await clear.click();
  await page.getByRole('alert').filter({ hasText: 'Could not clear chat history' }).waitFor();
  check('failed clear preserves open conversation', await reply.isVisible());
  await page.unroute('**/api/chats');

  // A second chat plus an unmatched search proves this isn't just deleting
  // whichever rows happen to be visible.
  await page.request.post(BRIDGE + '/api/chats');
  await page.getByRole('button', { name: 'Search chats', exact: true }).click();
  await page.getByPlaceholder('Search chats…').fill('no-such-conversation');
  await page.getByText('No matching chats', { exact: true }).waitFor();
  page.once('dialog', dialog => dialog.accept());
  await clear.focus();
  await page.keyboard.press('Enter');
  await page.locator('.chat-empty').waitFor();
  check('clear removes all saved chats', (await page.request.get(BRIDGE + '/api/chats').then(r => r.json())).chats.length === 0);
  check('clear resets selected conversation', await page.evaluate(() => localStorage.getItem('ava.chat')) === null);
  check('clear disabled when empty', await clear.isDisabled());
  await page.reload();
  await composer.waitFor();
  await page.getByText('No conversations yet', { exact: true }).waitFor();
  check('history stays empty after reload', (await page.request.get(BRIDGE + '/api/chats').then(r => r.json())).chats.length === 0);
  await composer.fill('Hello again after clearing history');
  await page.getByRole('button', { name: 'Send', exact: true }).click();
  await reply.waitFor({ timeout: 45000 });
  check('chat still works after clearing', true);
  const output = process.env.QA_SCREENSHOTS;
  if (output) {
    await mkdir(output, { recursive: true });
    await page.screenshot({ path: `${output}/chat-clear-desktop.png`, animations: 'disabled' });
  }
  await page.setViewportSize({ width: 390, height: 844 });
  await page.locator('#appShell.nav-closed').waitFor();
  await page.getByRole('button', { name: 'Open chats', exact: true }).click();
  await clear.waitFor();
  // Visibility precedes the drawer's slide-in animation finishing.
  await page.waitForFunction(() => {
    const bounds = document.querySelector('.draw-clear')?.getBoundingClientRect();
    return bounds && bounds.x >= 0 && bounds.right <= window.innerWidth;
  });
  const bounds = await clear.boundingBox();
  check('clear all is visible in the mobile sidebar', !!bounds && bounds.x >= 0 && bounds.x + bounds.width <= 390);
  if (output) await page.screenshot({ path: `${output}/chat-clear-mobile.png`, animations: 'disabled' });

  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
