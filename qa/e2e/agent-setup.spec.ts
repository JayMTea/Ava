// Real setup panels, responsive geometry, and controlled runtime recovery cases.
// Run after setup-flow.spec.ts; no real agent is provisioned by this test.
import assert from 'node:assert/strict';
import { mkdir } from 'node:fs/promises';
import { chromium, type Page, type WebSocketRoute } from 'playwright';

const BRIDGE = process.env.BRIDGE_URL!;
const browser = await chromium.launch();
const page = await browser.newPage({ serviceWorkers: 'block', reducedMotion: 'reduce' });
const errors: string[] = [];
page.on('pageerror', (error) => errors.push(error.message));
const screenshotDir = process.env.AVA_QA_SCREENSHOTS;
if (screenshotDir) await mkdir(screenshotDir, { recursive: true });

async function layout(p: Page, name: string) {
  const issues = await p.locator('.agent-setup-panels').evaluate((root) => {
    const problems: string[] = [];
    const bounds = root.getBoundingClientRect();
    const cards = [...root.querySelectorAll('.db-panel')].map((el) => el.getBoundingClientRect());
    for (let i = 1; i < cards.length; i++) {
      if (cards[i].top < cards[i - 1].bottom + 8) problems.push('cards touch or overlap');
    }
    for (const el of root.querySelectorAll('.db-panel, .hub-btn, .hub-input, .hub-select, .stat-row, .skill-head')) {
      const box = el.getBoundingClientRect();
      if (!box.width || !box.height) continue;
      if (box.left < bounds.left - 1 || box.right > bounds.right + 1) {
        problems.push(`${el.className}: outside panel (${Math.round(box.width)}px)`);
      }
    }
    if (document.documentElement.scrollWidth > innerWidth) problems.push('page scrolls horizontally');
    return problems;
  });
  assert.deepEqual(issues, [], name);
}

try {
  await page.goto(BRIDGE + '/login');
  await page.fill('input[name=password]', process.env.QA_PASSWORD!);
  await Promise.all([page.waitForURL((url) => !url.pathname.includes('/login')), page.keyboard.press('Enter')]);

  for (const width of [1440, 768, 390, 320]) {
    await page.setViewportSize({ width, height: 1000 });
    for (const tab of ['runtime', 'brain', 'persona', 'skills', 'memory', 'voice', 'providers']) {
      await page.goto(`${BRIDGE}/#hub/agent/${tab}`);
      await page.locator(`#agent-tab-${tab}[aria-selected=true]`).waitFor();
      await page.locator('.agent-setup-panels .db-panel').first().waitFor();
      await page.waitForTimeout(350);
      if (tab === 'brain') {
        await page.getByRole('button', { name: 'Add a model', exact: true }).click();
        await page.getByLabel('Name', { exact: true }).fill('a-long-model-name-for-layout-review');
        await page.getByLabel('Base URL', { exact: true }).fill('https://inference.example.test/very-long-endpoint/v1');
      }
      await layout(page, `${tab} at ${width}px`);
      if (screenshotDir && [1440, 390].includes(width)) {
        await page.screenshot({ path: `${screenshotDir}/${tab}-${width}.png`, fullPage: true });
      }
      console.log(`PASS  ${tab} layout at ${width}px`);
    }
  }

  await page.getByRole('tab', { name: 'Providers', exact: true }).focus();
  await page.keyboard.press('Home');
  await page.locator('#agent-tab-runtime[aria-selected=true]').waitFor();
  assert.equal(await page.locator('#agent-tab-runtime').evaluate((el) => document.activeElement === el), true);
  await page.keyboard.press('ArrowRight');
  await page.locator('#agent-tab-brain[aria-selected=true]').waitFor();
  console.log('PASS  keyboard navigation and focus');

  // A long remote host, partially verified skills, and a failed job restored on reload.
  const counts = { deployed: 2, stale: 0, undeployed: 0, unknown: 1, total: 3 };
  let forceReads = 0;
  let starts = 0;
  await page.route('**/api/hub/agent/status', (route) => route.fulfill({ json: {
    runtime: 'remote', name: 'remote', display_name: 'Remote agent', enabled: true,
    available: true, tools: true, location: 'remote', required: false,
    url: `https://${'agent-'.repeat(24)}example.test/runtime`,
  } }));
  await page.route('**/api/hub/agent/provision/state*', (route) => {
    if (route.request().url().includes('force=1')) forceReads++;
    return route.fulfill({ json: { ok: true, enabled: true, pending: 0, counts,
      sandbox: { live: true }, scopes: { skills: { counts, state: 'unknown' } } } });
  });
  await page.route('**/api/hub/agent/provision/status*', (route) => route.fulfill({ json: {
    id: 'failed-run', status: 'error', started_at: 1, ended_at: 3, rc: 1,
    steps: [], log: ['Connection refused by the agent host'], seq: 1,
    detail: 'Could not apply the updated skills.', observable: true,
  } }));
  await page.route(/\/api\/hub\/agent\/provision\?scope=/, (route) => { starts++; return route.abort(); });
  await page.goto(`${BRIDGE}/#hub/agent`);
  await page.getByText('2 of 3 live · 1 unchecked', { exact: true }).waitFor();
  await page.getByRole('button', { name: 'Hide log', exact: true }).click();
  assert.equal(await page.locator('.hub-preview pre').count(), 0);
  await page.getByRole('button', { name: 'Show log', exact: true }).click();
  assert.match(await page.locator('.hub-preview pre').innerText(), /Connection refused/);
  await page.getByRole('button', { name: 'Re-check agent', exact: true }).click();
  await page.waitForFunction(() => !document.querySelector('.agent-apply-section[aria-busy=true]'));
  assert.ok(forceReads > 0);
  assert.equal(starts, 0);
  await layout(page, 'long remote host and failed job');
  if (screenshotDir) await page.screenshot({ path: `${screenshotDir}/runtime-recovery-320.png`, fullPage: true });
  console.log('PASS  completed failure recovery, log toggle, honest partial status, and forced re-check');

  let applying = false;
  let finished = false;
  await page.route('**/api/hub/agent/provision/state*', (route) => route.fulfill({ json: {
    ok: true, enabled: true, pending: finished ? 0 : 2, sandbox: { live: true },
    scopes: { skills: { state: finished ? 'deployed' : 'stale', counts: {
      deployed: finished ? 3 : 1, stale: finished ? 0 : 2, undeployed: 0, unknown: 0, total: 3,
    } } },
  } }));
  await page.route('**/api/hub/agent/provision/status*', (route) => route.fulfill({ json: {
    id: applying ? 'resumable-run' : null, status: finished ? 'done' : applying ? 'running' : 'idle',
    started_at: applying ? Date.now() / 1000 : null, ended_at: finished ? Date.now() / 1000 : null,
    rc: finished ? 0 : null, steps: [], log: [], seq: 0, detail: '', observable: true,
  } }));
  await page.route(/\/api\/hub\/agent\/provision\?scope=/, (route) => {
    applying = true; starts++;
    return route.fulfill({ json: { ok: true, job_id: 'resumable-run' } });
  });
  await page.getByRole('button', { name: 'Re-check agent', exact: true }).click();
  await page.getByRole('button', { name: 'Apply 2 changes', exact: true }).click();
  await page.getByText('Applying changes', { exact: false }).waitFor();
  assert.equal(await page.getByRole('button', { name: 'Apply 2 changes', exact: true }).count(), 0);
  await page.reload();
  await page.getByText('Applying changes', { exact: false }).waitFor();
  finished = true;
  await page.getByText('all 3 live', { exact: true }).waitFor();
  assert.equal(starts, 1, 'reload follows the running job without starting it again');
  console.log('PASS  apply resumes after reload and refreshes configuration on completion');

  let failSkills = true;
  await page.route('**/api/hub/agent/skills', (route) => failSkills
    ? route.fulfill({ status: 503, json: { error: 'Temporary skills outage' } }) : route.continue());
  await page.goto(`${BRIDGE}/#hub/agent/skills`);
  await page.getByRole('alert').filter({ hasText: 'Temporary skills outage' }).waitFor();
  failSkills = false;
  await page.getByRole('button', { name: 'Try again', exact: true }).click();
  await page.getByRole('alert').filter({ hasText: 'Temporary skills outage' }).waitFor({ state: 'detached' });
  console.log('PASS  skills recover after a failed request');

  // Microphone permission may resolve after the user has left Voice.
  await page.route('**/api/hub/voice/status', (route) => route.fulfill({ json: {
    enabled: true, deps_ok: true, deps_error: '', enrolled: true, threshold: 0.7,
  } }));
  await page.evaluate(`(() => {
    const state = window.voiceReview = { stops: 0, requests: 0, resolve: null };
    Object.defineProperty(navigator.mediaDevices, 'getUserMedia', { configurable: true,
      value: () => { state.requests++; return new Promise((resolve) => { state.resolve = resolve; }); } });
  })()`);
  await page.goto(`${BRIDGE}/#hub/agent/voice`);
  await page.getByRole('button', { name: 'Record clip 1', exact: true }).click();
  await page.getByRole('tab', { name: 'Skills', exact: true }).click();
  const stopped = await page.evaluate(`(async () => {
    const state = window.voiceReview;
    state.resolve({ getTracks: () => [{ stop: () => state.stops++ }] });
    await new Promise((resolve) => setTimeout(resolve, 30));
    return state.stops;
  })()`);
  assert.equal(stopped, 1, 'leaving Voice releases a late microphone stream');
  await page.evaluate(`(() => {
    window.voiceReview.stops = 0;
    Object.defineProperty(navigator.mediaDevices, 'getUserMedia', { configurable: true,
      value: async () => ({ getTracks: () => [{ stop: () => window.voiceReview.stops++ }] }) });
    window.MediaRecorder = class {
      constructor(stream) { this.stream = stream; this.state = 'inactive'; }
      start() { this.state = 'recording'; }
      stop() { this.state = 'inactive'; }
    };
  })()`);
  await page.getByRole('tab', { name: 'Voice', exact: true }).click();
  await page.getByRole('button', { name: 'Record clip 1', exact: true }).click();
  await page.getByRole('button', { name: 'Stop recording', exact: true }).waitFor();
  await page.getByRole('tab', { name: 'Skills', exact: true }).click();
  assert.equal(await page.evaluate('window.voiceReview.stops'), 1);
  console.log('PASS  leaving Voice releases microphone access');

  // Mount Providers while disconnected, then deliver the gateway ready event.
  let gatewaySocket: WebSocketRoute | undefined;
  await page.routeWebSocket('**/ws/gateway', (socket) => {
    gatewaySocket = socket;
    socket.send(JSON.stringify({ op: 'state', phase: 'down', why: 'reconnecting' }));
  });
  let providerReads = 0;
  await page.route('**/api/gateway/rpc', (route) => {
    const method = route.request().postDataJSON().method;
    if (method === 'models.list') providerReads++;
    return route.fulfill({ json: { ok: true, payload: method === 'models.list'
      ? { models: [{ id: 'recovered-model', name: 'Recovered provider model' }] } : { total: 0 } } });
  });
  await page.goto(`${BRIDGE}/#hub/agent/providers`);
  await page.reload();
  await page.getByText(/The agent gateway is not connected/).waitFor();
  assert.ok(gatewaySocket);
  gatewaySocket.send(JSON.stringify({ op: 'state', phase: 'ready' }));
  await page.getByText('Recovered provider model', { exact: true }).waitFor();
  assert.ok(providerReads > 0);
  await layout(page, 'providers after gateway recovery');
  console.log('PASS  providers load after the gateway reconnects');
  assert.deepEqual(errors, [], 'browser errors');
} finally {
  await browser.close();
}
