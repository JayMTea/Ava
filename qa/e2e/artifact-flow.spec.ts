// Real persisted chats, authenticated APIs, and the built SPA. No mocked chart payloads.
import assert from 'node:assert/strict';
import { mkdir } from 'node:fs/promises';
import { chromium } from 'playwright';

const base = process.env.BRIDGE_URL!;
const first = process.env.QA_CHART_CHAT!;
const other = process.env.QA_OTHER_CHAT!;
const imageChat = process.env.QA_IMAGE_CHAT!;
const artifactId = process.env.QA_CHART_ID!;
const output = process.env.QA_SCREENSHOTS;

const browser = await chromium.launch();
try {
  const page = await browser.newPage({ viewport: { width: 1500, height: 950 } });
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto(base + '/');
  await page.fill('input[name=password]', process.env.QA_PASSWORD!);
  await Promise.all([page.waitForURL(url => !url.pathname.includes('/login')), page.keyboard.press('Enter')]);
  await page.locator('a[href="#chat"]:visible').first().click();
  const openChat = async (id: string) => {
    await page.evaluate(chat => window.dispatchEvent(new CustomEvent('ava:open-chat', { detail: { id: chat } })), id);
  };
  await page.locator('#text').waitFor();
  await openChat(first);
  const preview = page.getByRole('button', { name: /^Open chart:/ });
  await preview.waitFor();
  assert.equal(await page.locator('.chat-artifact-panel').count(), 0, 'a result does not force open the panel');
  assert.ok(await page.getByRole('link', { name: 'Census PUMS documentation' }).first().isVisible());
  if (output) { await mkdir(output, { recursive: true }); await page.screenshot({ path: `${output}/chart-preview.png` }); }
  const payload = await (await page.request.get(`${base}/api/artifact/analytics/${artifactId}`)).json();
  for (const row of payload.result.rows) assert.ok((await preview.innerText()).includes(new Intl.NumberFormat('en-US').format(row.value)));
  await page.locator('#text').fill('Tell me more about Oregon');
  await preview.click();
  const panel = page.getByRole('complementary', { name: 'Conversation visualization' });
  await panel.waitFor();
  await panel.getByRole('link', { name: 'Census PUMS documentation' }).waitFor();
  assert.ok(await page.locator('#text').isVisible(), 'composer remains usable beside the chart');
  const paneBox = (await panel.boundingBox())!;
  const chatBox = (await page.locator('.chat-workspace').boundingBox())!;
  const headerBox = (await page.locator('#appCol > header').boundingBox())!;
  assert.ok(paneBox.y >= headerBox.y + headerBox.height - 1, 'chart stays beneath the common header');
  assert.ok(paneBox.x >= chatBox.x && paneBox.x + paneBox.width <= chatBox.x + chatBox.width + 1);
  assert.equal(await panel.getByRole('tab').count(), 0, 'no analytics tabs in the viewer');
  assert.ok(await panel.getByRole('link', { name: 'Census PUMS documentation' }).isVisible());
  if (output) await page.screenshot({ path: `${output}/chart-split.png` });
  const divider = page.getByRole('separator', { name: 'Resize visualization' });
  await divider.focus(); await page.keyboard.press('ArrowLeft');
  assert.equal(await divider.getAttribute('aria-valuenow'), '55');
  await page.getByRole('button', { name: 'Expand visualization', exact: true }).click();
  assert.equal(await page.locator('#text').isVisible(), false);
  await page.getByRole('button', { name: 'Restore split view' }).click();
  assert.equal(await page.locator('#text').inputValue(), 'Tell me more about Oregon');
  await page.getByRole('button', { name: 'Send', exact: true }).click();
  await page.getByText('QA fake model', { exact: false }).first().waitFor({ timeout: 45000 });
  assert.ok(await panel.isVisible(), 'a follow-up uses the same conversation with the chart open');
  await page.keyboard.press('Escape');
  assert.equal(await panel.count(), 0);
  assert.ok(await preview.evaluate(el => el === document.activeElement), 'closing returns keyboard focus to the chart preview');
  await preview.click();
  await openChat(other);
  await page.getByText('A separate conversation.', { exact: true }).waitFor();
  assert.equal(await panel.count(), 0, 'another conversation does not inherit this chart');
  let release!: () => void;
  let started!: () => void;
  const waiting = new Promise<void>(resolve => { started = resolve; });
  const held = new Promise<void>(resolve => { release = resolve; });
  await page.route(`**/api/chats/${first}`, async route => { started(); await held; await route.continue(); });
  await openChat(first);
  await waiting;
  assert.ok(await page.getByRole('button', { name: 'Send', exact: true }).isDisabled(), 'sending waits for the selected conversation to load');
  await openChat(other);
  await page.getByText('A separate conversation.', { exact: true }).waitFor();
  const late = page.waitForResponse(response => response.url().endsWith(`/api/chats/${first}`));
  release();
  await late;
  await page.unroute(`**/api/chats/${first}`);
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  assert.equal(await page.evaluate(() => localStorage.getItem('ava.chat')), other, 'a late history response cannot switch conversations');
  assert.equal(await panel.count(), 0);
  await openChat(first);
  await panel.waitFor();
  await page.reload();
  await panel.waitFor();
  await page.evaluate(() => { location.hash = 'hub'; });
  await page.waitForFunction(() => !document.querySelector('.chat-workspace'));
  assert.equal(await panel.count(), 0, 'other Ava views are not squeezed by an artifact');
  await page.goto(`${base}/?artifact=${artifactId}#chat`);
  await panel.waitFor();
  assert.equal(await page.evaluate(() => localStorage.getItem('ava.chat')), first, 'artifact links restore the owning chat');
  await page.setViewportSize({ width: 390, height: 844 });
  await page.waitForFunction(() => !document.querySelector('#scrim')?.classList.contains('open'));
  await panel.getByRole('link', { name: 'Census PUMS documentation' }).waitFor();
  assert.ok(await panel.isVisible());
  assert.equal(await page.locator('#text').isVisible(), false);
  const [mobile, mobileHeader] = await page.evaluate(() => ['.chat-artifact-panel', '#appCol > header'].map(selector => {
    const rect = document.querySelector(selector)!.getBoundingClientRect();
    return { x: rect.x, y: rect.y, width: rect.width, height: rect.height };
  }));
  assert.ok(mobile.y >= mobileHeader.y + mobileHeader.height - 1);
  assert.ok(mobile.x >= 0 && mobile.x + mobile.width <= 391);
  if (output) await page.screenshot({ path: `${output}/chart-mobile.png`, animations: 'disabled' });
  await page.getByRole('button', { name: 'Close visualization and return to chat' }).click();
  assert.ok(await page.locator('#text').isVisible());
  await page.setViewportSize({ width: 1500, height: 950 });
  await openChat(imageChat);
  await page.locator('.analysis-chart svg').first().waitFor();
  assert.equal(await page.locator('.analysis-chart-image').count(), 0, 'recorded values follow the active theme');
  if (output) {
    await preview.click();
    await panel.getByRole('link', { name: 'Census PUMS documentation' }).waitFor();
    await panel.locator('.analysis-chart svg').first().waitFor();
    await page.screenshot({ path: `${output}/chart-from-analytics.png`, animations: 'disabled' });
  }
  // The underlying image route remains authenticated.
  const anon = await browser.newContext();
  const response = await anon.request.get(`${base}/api/artifact/analytics/${process.env.QA_IMAGE_ID}/chart`);
  assert.notEqual(response.headers()['content-type'], 'image/svg+xml');
  await anon.close();
  await openChat(process.env.QA_GENERIC_CHAT!);
  await page.getByRole('img', { name: /USD by Department/ }).first().waitFor();
  assert.ok((await preview.innerText()).includes('-12.5'));
  assert.ok((await preview.innerText()).includes('25.75'));
  assert.equal(await page.getByText(/Weighted PUMS/).count(), 0);
  assert.equal(await page.getByText(/Margin of error unavailable/).count(), 0);
  for (const theme of ['light', 'dark']) {
    await page.evaluate(value => document.documentElement.setAttribute('data-theme', value), theme);
    assert.ok(await page.locator('.analysis-chart svg rect').first().evaluate(el => {
      const fill = getComputedStyle(el).fill;
      return fill !== 'none' && el.getBoundingClientRect().width > 0;
    }));
  }
  if (output) await page.screenshot({ path: `${output}/generic-chart.png`, animations: 'disabled' });
  // A failed preview request stays within the conversation and can be retried.
  let fail = true;
  await page.route(`**/api/artifact/analytics/${artifactId}`, async route => {
    if (fail) await route.fulfill({ status: 503, body: '{}' }); else await route.continue();
  });
  await openChat(first);
  await page.getByRole('alert').first().waitFor();
  fail = false;
  await page.getByRole('button', { name: 'Try again', exact: true }).first().click();
  await preview.waitFor();
  assert.deepEqual(errors, []);
  console.log('PASS artifact flow: preview, exact recorded values, sources, split, resize, expand, focus, chat isolation, reload, links, mobile, image auth, retry');
} finally {
  await browser.close();
}
