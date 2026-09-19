// Run the built Ava shell with a separate-origin clipboard fixture. This checks
// the browser's actual permission boundary, without any real research tokens.
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

const dist = resolve(dirname(fileURLToPath(import.meta.url)), '../../frontend/dist');
const child = createServer((_req, res) => {
  res.setHeader('Content-Type', 'text/html');
  res.end(`<button id="models">Models</button><button id="detail">Open model</button><button id="home">Home</button><input id="draft" /><output id="route"></output>
    <script>
    const origin=new URL(document.referrer).origin;
    function report(){ const path=location.pathname.slice('/apps/navigation'.length)+location.search;
      document.querySelector('#route').textContent=path;
      parent.postMessage({type:'ava:navigation',cid:'navigation',path},origin); }
    function go(path){history.pushState(null,'','/apps/navigation'+path);report();}
    document.querySelector('#models').onclick=()=>go('/machine-learning?tab=models');
    document.querySelector('#detail').onclick=()=>go('/machine-learning?tab=models&kind=model&item=123');
    document.querySelector('#home').onclick=()=>go('/');
    addEventListener('popstate',report);report();
    </script>`);
});
await new Promise<void>(done => child.listen(0, '127.0.0.1', done));
const childOrigin = `http://127.0.0.1:${(child.address() as { port: number }).port}`;
let grants = 0;
const shell = createServer(async (req, res) => {
  const path = new URL(req.url!, 'http://localhost').pathname;
  if (path === '/api/apps/navigation/embed') grants++;
  const responses: Record<string, unknown> = {
    '/api/apps': { apps: [{ id: 'navigation', label: 'Navigation fixture', section: 'apps',
      order: 1, embed: 'iframe', has_api: false, url: '/apps/navigation/' }] },
    '/api/apps/navigation/embed': { url: childOrigin + '/apps/navigation/', isolated: true },
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
  const page = await context.newPage();
  const errors: string[]=[];page.on('pageerror', e=>errors.push(e.message));
  await page.goto(shellOrigin + '/#navigation');
  const frame = page.frameLocator('iframe[title="Navigation fixture"]');
  await frame.locator('#models').click();
  await page.waitForURL('**/#navigation/machine-learning?tab=models');
  await frame.locator('#draft').fill('keep this');
  await frame.locator('#detail').click();
  await page.waitForURL('**/#navigation/machine-learning?tab=models&kind=model&item=123');
  assert.equal(await frame.locator('#draft').inputValue(),'keep this');
  assert.equal(grants,1,'Reporting a route must not remount or mint another lease');
  await page.reload();
  await frame.locator('#detail').waitFor();
  assert.equal(await frame.locator('#route').textContent(),'/machine-learning?tab=models&kind=model&item=123');
  await frame.locator('#home').click();
  await page.waitForURL('**/#navigation/');
  await page.reload();
  await frame.locator('#home').waitFor();
  assert.equal(await frame.locator('#route').textContent(),'/');
  await frame.locator('#models').click();
  await page.waitForURL('**/#navigation/machine-learning?tab=models');
  await frame.locator('#detail').click();
  await page.waitForURL('**/#navigation/machine-learning?tab=models&kind=model&item=123');
  await page.goBack();
  await page.waitForURL('**/#navigation/machine-learning?tab=models');
  await page.goForward();
  await page.waitForURL('**/#navigation/machine-learning?tab=models&kind=model&item=123');
  await page.evaluate(()=>{location.hash='hub';});
  await page.waitForURL('**/#hub**');
  await page.evaluate(()=>{location.hash='navigation';});
  await frame.locator('#detail').waitFor();
  // A tile with no subpath must stamp the kept-alive page before refresh.
  await page.waitForURL('**/#navigation/machine-learning?tab=models&kind=model&item=123');
  await page.reload();
  await frame.locator('#detail').waitFor();
  assert.equal(await frame.locator('#route').textContent(),'/machine-learning?tab=models&kind=model&item=123');
  // A refresh taken on ANOTHER tab: the fragment then describes that tab and
  // says nothing about the app, so the memory has to outlive the address bar.
  await page.evaluate(()=>{location.hash='hub';});
  await page.waitForURL('**/#hub**');
  await page.reload();
  await page.waitForURL('**/#hub**');
  assert.equal(await page.locator('iframe[title="Navigation fixture"]').count(),0,
    'A tab the user is not on must not load the app frame');
  await page.evaluate(()=>{location.hash='navigation';});
  await frame.locator('#detail').waitFor();
  assert.equal(await frame.locator('#route').textContent(),'/machine-learning?tab=models&kind=model&item=123');
  await page.waitForURL('**/#navigation/machine-learning?tab=models&kind=model&item=123');
  // ...but "open it at its home" still means home, and is remembered as such.
  await page.evaluate(()=>{location.hash='navigation/';});
  await frame.locator('#home').waitFor();
  assert.equal(await frame.locator('#route').textContent(),'/');
  await page.evaluate(()=>{location.hash='hub';});
  await page.waitForURL('**/#hub**');
  await page.reload();
  await page.evaluate(()=>{location.hash='navigation';});
  await frame.locator('#home').waitFor();
  assert.equal(await frame.locator('#route').textContent(),'/');
  assert.deepEqual(errors,[]);
  console.log('PASS: separate-origin page/tab/detail survive refresh, Back/Forward, switching apps and a refresh taken on another tab, without remounting');
} finally {
  await browser.close();
  child.closeAllConnections(); shell.closeAllConnections();
  child.close(); shell.close();
}
