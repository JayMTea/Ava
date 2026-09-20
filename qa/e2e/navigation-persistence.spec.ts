// Run the built Ava shell with a separate-origin app fixture that reports its
// route, and check the route survives refresh, history and switching tabs.
//
// Both servers send `Referrer-Policy: same-origin` on every response, as a
// reverse proxy in front of a real install commonly does. This spec used to pass
// without it while every refresh in production landed on the app's home page:
// the policy strips the referrer from the cross-origin frame navigation, the
// app reads its shell's origin from document.referrer, finds nothing, and never
// reports a route. The fixture discovers the shell the same way on purpose.
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

const dist = resolve(dirname(fileURLToPath(import.meta.url)), '../../frontend/dist');
const child = createServer((_req, res) => {
  res.setHeader('Referrer-Policy', 'same-origin');
  res.setHeader('Content-Type', 'text/html');
  res.end(`<button id="models">Models</button><button id="detail">Open model</button><button id="home">Home</button><input id="draft" /><output id="route"></output>
    <button id="hello">Ask the shell</button><output id="shell"></output>
    <script>
    // Like a deployed app: the shell is whoever the referrer names, and with no
    // referrer the app keeps working but stays silent.
    const origin=document.referrer?new URL(document.referrer).origin:'';
    function report(){ const path=location.pathname.slice('/apps/navigation'.length)+location.search;
      document.querySelector('#route').textContent=path;
      if(origin)parent.postMessage({type:'ava:navigation',cid:'navigation',path},origin); }
    function go(path){history.pushState(null,'','/apps/navigation'+path);report();}
    document.querySelector('#models').onclick=()=>go('/machine-learning?tab=models');
    document.querySelector('#detail').onclick=()=>go('/machine-learning?tab=models&kind=model&item=123');
    document.querySelector('#home').onclick=()=>go('/');
    // The fallback for an app that cannot trust its referrer: ask anyone, and
    // believe the origin of the answer that comes from the parent window.
    document.querySelector('#hello').onclick=()=>{
      addEventListener('message',function reply(e){
        if(e.data?.type!=='ava:theme')return;
        removeEventListener('message',reply);
        document.querySelector('#shell').textContent=JSON.stringify({origin:e.origin,fromParent:e.source===parent});
      });
      parent.postMessage({type:'ava:theme-request'},'*');
    };
    addEventListener('popstate',report);report();
    </script>`);
});
await new Promise<void>(done => child.listen(0, '127.0.0.1', done));
const childOrigin = `http://127.0.0.1:${(child.address() as { port: number }).port}`;
let grants = 0;
const shell = createServer(async (req, res) => {
  res.setHeader('Referrer-Policy', 'same-origin');
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
    const file = path.endsWith('/') ? '/index.html' : path;
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
  // Opened below its root, so the exact referrer check below proves the shell's
  // path stays home.
  await page.goto(shellOrigin + '/opened/from/a/link/#navigation');
  const frame = page.frameLocator('iframe[title="Navigation fixture"]');
  // The shell introduces itself with its origin alone: none of its path (and no
  // referrer ever carries a fragment, where the app's route lives).
  // (#models exists only in the app's document, never the frame's initial blank one.)
  assert.equal(await frame.locator('#models').evaluate(() => document.referrer), shellOrigin + '/',
    'Under Referrer-Policy: same-origin a cross-origin frame gets an empty document.referrer unless '
    + 'the shell sets referrerpolicy="origin" on the iframe; without it the app cannot report its route');
  // The handshake an app falls back on without a referrer. Wait until the frame
  // is shown, which is when the shell's load-time theme has already been sent,
  // so what arrives after the question is the answer to it.
  await page.locator('iframe[title="Navigation fixture"]').waitFor({ state: 'visible' });
  await frame.locator('#hello').click();
  await frame.locator('#shell').filter({ hasText: /./ }).waitFor();
  assert.deepEqual(JSON.parse(await frame.locator('#shell').textContent() ?? ''),
    { origin: shellOrigin, fromParent: true }, 'An ava:theme-request must be answered from the shell origin');
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
  // A reloaded frame is a first document again, and must be introduced again.
  assert.equal(await frame.locator('#detail').evaluate(() => document.referrer), shellOrigin + '/');
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
  console.log('PASS: behind Referrer-Policy: same-origin, the shell introduces itself and answers the handshake; '
    + 'separate-origin page/tab/detail survive refresh, Back/Forward, switching apps and a refresh taken on another tab, without remounting');
} finally {
  await browser.close();
  child.closeAllConnections(); shell.closeAllConnections();
  child.close(); shell.close();
}
