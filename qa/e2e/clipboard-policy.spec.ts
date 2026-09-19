// Run the built Ava shell with a separate-origin clipboard fixture. This checks
// the browser's actual permission boundary, without any real research tokens.
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

const dist = resolve(dirname(fileURLToPath(import.meta.url)), '../../frontend/dist');
const fixture = 'clipboard-regression-fixture';
const child = createServer((_req, res) => {
  res.setHeader('Content-Type', 'text/html');
  res.end(`<button id="copy">Copy test value</button><output id="result"></output>
    <script>document.querySelector('#copy').onclick = async () => {
      try { await navigator.clipboard.writeText('${fixture}');
        document.querySelector('#result').textContent = 'copied';
      } catch (error) { document.querySelector('#result').textContent = 'refused: ' + error.message; }
    };</script>`);
});
await new Promise<void>(done => child.listen(0, '127.0.0.1', done));
const childOrigin = `http://127.0.0.1:${(child.address() as { port: number }).port}`;
const shell = createServer(async (req, res) => {
  const path = new URL(req.url!, 'http://localhost').pathname;
  const responses: Record<string, unknown> = {
    '/api/apps': { apps: [{ id: 'clipboard', label: 'Clipboard fixture', section: 'apps',
      order: 1, embed: 'iframe', has_api: false, url: '/apps/clipboard/' }] },
    '/api/apps/clipboard/embed': { url: childOrigin + '/apps/clipboard/', isolated: true },
    '/api/apps/health': { apps: [] },
    '/api/chats': { chats: [] },
    '/api/brand': { name: 'Ava', tagline: '' },
    '/api/domains': { enabled: false, realms: [], domains: [] },
    '/api/model': { available: [], model: '', mode: '' },
  };
  if (path.startsWith('/api/')) {
    res.setHeader('Content-Type', 'application/json');
    res.end(JSON.stringify(responses[path] ?? {}));
    return;
  }
  try {
    const file = path === '/' ? '/index.html' : path;
    const target = resolve(dist, '.' + file);
    assert(target.startsWith(dist + '/') || target.startsWith(dist + '\\'));
    const data = await readFile(target);
    res.setHeader('Content-Type', file.endsWith('.js') ? 'text/javascript'
      : file.endsWith('.css') ? 'text/css' : file.endsWith('.html') ? 'text/html'
      : 'application/octet-stream');
    res.end(data);
  } catch { res.writeHead(404); res.end(); }
});
await new Promise<void>(done => shell.listen(0, '127.0.0.1', done));
const shellOrigin = `http://127.0.0.1:${(shell.address() as { port: number }).port}`;
const browser = await chromium.launch();
try {
  const context = await browser.newContext({ serviceWorkers: 'block' });
  // Browser permission and iframe policy are separate gates. Allow writes in
  // headless Chromium; the negative control still exercises the iframe gate.
  await context.grantPermissions(['clipboard-write'], { origin: childOrigin });
  const page = await context.newPage();
  await page.goto(shellOrigin + '/#clipboard');
  const element = page.locator('iframe[title="Clipboard fixture"]');
  const frame = page.frameLocator('iframe[title="Clipboard fixture"]');
  await frame.locator('#copy').click();
  await frame.locator('#result').filter({ hasText: /copied|refused/ }).waitFor();
  assert.equal(await frame.locator('#result').textContent(), 'copied');
  // Only the parent gets clipboard-read permission to verify the copied value.
  await context.grantPermissions(['clipboard-read'], { origin: shellOrigin });
  assert.equal(await page.evaluate(() => navigator.clipboard.readText()), fixture);
  const policies = await frame.locator('body').evaluate(() => {
    const policy = (document as Document & {
      featurePolicy: { allowsFeature(name: string): boolean };
    }).featurePolicy;
    return { write: policy.allowsFeature('clipboard-write'), read: policy.allowsFeature('clipboard-read') };
  });
  assert.deepEqual(policies, { write: true, read: false });
  assert.equal(await element.getAttribute('allow'), "clipboard-write 'src'");
  console.log('PASS: embedded app writes the clipboard; clipboard reads remain blocked');

  // Negative control: the original frame must reproduce the reported failure.
  await element.evaluate(el => {
    el.removeAttribute('allow');
    (el as HTMLIFrameElement).src += '?without-permission=1';
  });
  await frame.locator('#copy').click();
  await frame.locator('#result').filter({ hasText: 'refused' }).waitFor();
  console.log('PASS: removing delegation reproduces the original clipboard refusal');
} finally {
  await browser.close();
  child.closeAllConnections(); shell.closeAllConnections();
  child.close(); shell.close();
}
