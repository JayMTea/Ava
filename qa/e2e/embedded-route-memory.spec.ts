// Route memory for an app that implements NO protocol of its own, through the
// real bridge.
//
// navigation-persistence.spec.ts covers the other half: an app that posts
// `ava:navigation` itself, against a hand-written fixture shell. This one is the
// case that had to be made standard — qa/fakes/fake_app.py contains no
// postMessage and no `ava:` string anywhere, so everything asserted below is
// bought by the bridge splicing its shim into the app's own document as it
// proxies it (ava_bridge/embed_route.py). If any of this passes, an app author
// gets route memory for free.
//
// The connector is the one qa/e2e/connectors-flow.spec.ts creates, which is why
// this spec runs after it in run_e2e.py's SPECS.
import assert from 'node:assert/strict';
import { chromium } from 'playwright';

const BRIDGE = process.env.BRIDGE_URL!;
const PASSWORD = process.env.QA_PASSWORD!;
const CID = 'qae2eapp';
const LABEL = 'QA E2E App';
const PREFIX = `/apps/${CID}`;
const ORIGIN = new URL(BRIDGE).origin;

const browser = await chromium.launch();
try {
  const context = await browser.newContext({ serviceWorkers: 'block' });
  const page = await context.newPage();
  const errors: string[] = [];
  page.on('pageerror', (e) => errors.push(e.message));

  // A lease is minted per FRAME MOUNT. Counting them is how "a reported route
  // must never re-mint the frame" is checked: a remount would reload the app and
  // throw away the in-page state route memory exists to preserve — and, since
  // every fresh document reports where it opened, a re-mint on report is a loop.
  let grants = 0;
  page.on('request', (r) => { if (r.url().includes(`/api/apps/${CID}/embed`)) grants++; });
  // Every document the FRAME loads, with the ETag the browser was given. The
  // app's page carries one, so a returning browser revalidates instead of
  // refetching; the bridge namespaces the tag on the way out and unmarks it on
  // the way back, which is the only reason a cached copy cannot survive without
  // the shim in it.
  const docs: { url: string; status: number; etag: string; conditional: Promise<string> }[] = [];
  page.on('response', (r) => {
    if (r.request().resourceType() === 'document' && r.url().startsWith(ORIGIN + PREFIX))
      docs.push({ url: r.url(), status: r.status(), etag: r.headers()['etag'] ?? '',
        // The headers the BROWSER actually sent, which is where a revalidation
        // is visible; `request().headers()` is the provisional set and omits
        // what the network stack adds.
        conditional: r.request().allHeaders().then((h) => h['if-none-match'] ?? '').catch(() => '') });
  });
  const shimFetches: string[] = [];
  page.on('request', (r) => { if (r.url().includes('/.ava/route.js')) shimFetches.push(r.url()); });

  await page.goto(BRIDGE + '/login');
  await page.fill('input[name=password]', PASSWORD);
  await Promise.all([page.waitForURL((u) => !u.pathname.includes('/login')),
                     page.keyboard.press('Enter')]);

  const frame = page.frameLocator(`iframe[title="${LABEL}"]`);
  // Where the APP says it is. The shell's launch parameters ride in the frame's
  // src — `t` is the embed token, the rest describe the mount — so a freshly
  // opened frame's own address carries them and the route it reports does not:
  // the shim strips exactly these before posting, which is why a remembered
  // address never holds a spent credential. Dropping them here compares the two
  // halves like for like.
  const LAUNCH = /^(t|theme|embedded|v)$/;
  const readout = async () => {
    await frame.locator('#go-home').waitFor();
    const url = new URL((await frame.locator('#route').textContent()) ?? '', ORIGIN);
    for (const key of [...url.searchParams.keys()]) if (LAUNCH.test(key)) url.searchParams.delete(key);
    return url.pathname + url.search + url.hash;
  };

  await page.evaluate((cid) => { window.location.hash = cid; }, CID);
  await frame.locator('#go-reports').waitFor();

  // 1. The shim is in the app's document, ran, and sits exactly where
  // embed_route.injection_point says it must: first in <head>, and after the
  // charset declaration so that declaration stays inside the first 1024 bytes
  // the encoding sniffer reads.
  const injected = await frame.locator('#go-reports').evaluate(() => {
    const shell = (window as unknown as { __avaShell?: { version: number; cid: string; origin: string } }).__avaShell;
    const first = document.head.querySelector('script');
    const before = first?.previousElementSibling as HTMLMetaElement | null;
    return {
      shell: shell && { version: shell.version, cid: shell.cid, origin: shell.origin },
      inline: !!first && !first.getAttribute('src'),
      afterCharset: before?.tagName === 'META' && before.getAttribute('charset') === 'utf-8',
      appScriptIsLater: document.querySelectorAll('script').length > 1,
    };
  });
  assert.deepEqual(injected.shell, { version: 1, cid: CID, origin: ORIGIN },
    'The injected shim must run and resolve the shell it may report to');
  assert.ok(injected.inline, 'With no CSP in the way the tag is inline, so it runs before the app bundle');
  assert.ok(injected.afterCharset, 'The tag goes after <meta charset>, never before it');
  assert.ok(injected.appScriptIsLater, "…and before the app's own script");
  assert.equal(grants, 1, 'One frame, one lease');
  const openedAt = { grants, docs: docs.length };

  // 2. A pushState the app makes for its own reasons reaches the shell address.
  await frame.locator('#go-reports').click();
  await page.waitForURL(`**/#${CID}/reports?tab=open`);
  assert.equal(await readout(), PREFIX + '/reports?tab=open');
  // In-page state survives the report — which is the whole point of not remounting.
  await frame.locator('#draft').fill('keep this');
  await frame.locator('#go-settings').click();
  await page.waitForURL(`**/#${CID}/settings`);
  assert.equal(await frame.locator('#draft').inputValue(), 'keep this');

  // 3. Back/Forward still move the app, because a pushState in a frame is an
  // entry in the joint session history and the shim reports what popstate lands on.
  await page.goBack();
  await page.waitForURL(`**/#${CID}/reports?tab=open`);
  assert.equal(await readout(), PREFIX + '/reports?tab=open');
  await page.goForward();
  await page.waitForURL(`**/#${CID}/settings`);
  assert.equal(await readout(), PREFIX + '/settings');

  // 4. A hash change is a route too, and is remembered as one.
  await frame.locator('#go-section').click();
  await page.waitForURL(`**/#${CID}/settings#section-two`);
  assert.equal(await readout(), PREFIX + '/settings#section-two');

  // 5. A full in-frame navigation — a real request through the proxy, a new
  // document, a freshly injected shim — is remembered the same way.
  await frame.locator('#go-load').click();
  await page.waitForURL(`**/#${CID}/reports?tab=closed`);
  assert.equal(await readout(), PREFIX + '/reports?tab=closed');

  assert.equal(grants, openedAt.grants,
    'Reporting a route must not re-mint the frame: a remount reloads the app, and a reloaded app reports again');
  assert.equal(docs.length, openedAt.docs + 1,
    'Exactly one extra document — the in-frame navigation. A client-side route change must load nothing');

  // 6. The reload. Without the shim this is where the owner loses their page:
  // the frame always re-loads its src and the browser never restores an in-page
  // location, so "closed reports" would come back as the app's home.
  await page.reload();
  await page.waitForURL(`**/#${CID}/reports?tab=closed`);
  assert.equal(await readout(), PREFIX + '/reports?tab=closed',
    'A reload must reopen the frame where the owner left it, not at the app home');
  assert.equal(grants, openedAt.grants + 1, 'A reload is one new frame, so one new lease');

  assert.ok(docs.length > 0 && docs.every((d) => d.etag.startsWith('W/"ava1-')),
    'Every framed document must carry the namespaced ETag: ' + JSON.stringify(docs));

  // 6b. …and the revalidation that ETag exists for. The app's page carries a tag
  // and the proxy's Cache-Control is `no-cache`, so a second load of the same
  // frame URL asks the app whether the copy is still good instead of fetching it
  // again. That is the case route memory lives or dies on for a browser that had
  // the app open before any of this shipped: handed back the app's OWN tag it
  // would earn a 304 and keep running a document with no shim in it, forever,
  // because nothing about that document ever changes.
  const revalidatedFrom = docs.length;
  await page.reload();
  await page.waitForURL(`**/#${CID}/reports?tab=closed`);
  assert.equal(await readout(), PREFIX + '/reports?tab=closed');
  const revalidated = docs.slice(revalidatedFrom);
  const offered = await Promise.all(revalidated.map((d) => d.conditional));
  assert.ok(offered.some((tag) => tag.startsWith('W/"ava1-')),
    'The second load of one frame URL must revalidate, offering back the tag the bridge issued — '
    + 'offering the app its own would earn a 304 for a document with no shim in it: '
    + JSON.stringify(offered));
  assert.equal(await frame.locator('#go-reports').evaluate(
    () => (window as unknown as { __avaShell?: { cid: string } }).__avaShell?.cid), CID,
    'A revalidated copy still has to be a copy the bridge injected into');

  // 7. The remembered route survives a reload taken while Ava is on ANOTHER tab,
  // where the address bar describes that tab and says nothing about the app.
  await page.evaluate(() => { window.location.hash = 'hub'; });
  await page.waitForURL('**/#hub**');
  await page.reload();
  await page.waitForURL('**/#hub**');
  assert.equal(await page.locator(`iframe[title="${LABEL}"]`).count(), 0,
    'A tab the owner is not on must not load the app frame');
  await page.evaluate((cid) => { window.location.hash = cid; }, CID);
  await page.waitForURL(`**/#${CID}/reports?tab=closed`);
  assert.equal(await readout(), PREFIX + '/reports?tab=closed');

  // 8. The app whose CSP bars inline script. The policy is never edited — the
  // bridge falls back to the external form of the same shim, served from the
  // reserved `/.ava/` namespace on the app's own mount, which is same-origin and
  // therefore already allowed by the app's own `'self'`.
  const locked = page.waitForResponse((r) => r.url() === ORIGIN + PREFIX + '/locked');
  await frame.locator('#go-locked').click();
  const lockedDoc = await locked;
  await page.waitForURL(`**/#${CID}/locked`);
  assert.equal(lockedDoc.headers()['content-security-policy'], "script-src 'self'",
    "An app's Content-Security-Policy must arrive exactly as the app wrote it");
  assert.deepEqual(shimFetches, [ORIGIN + PREFIX + '/.ava/route.js'],
    'Inline blocked, same-origin allowed: the tag becomes a file, fetched once');
  const external = await frame.locator('#go-reports').evaluate(() => {
    const shell = (window as unknown as { __avaShell?: { cid: string } }).__avaShell;
    const first = document.head.querySelector('script');
    return { cid: shell?.cid, src: first?.getAttribute('src') };
  });
  assert.deepEqual(external, { cid: CID, src: PREFIX + '/.ava/route.js' },
    'The external shim must be the head\'s first script and must have run');
  // …and it works from there: a route reported by the external form is the same
  // message, and survives the same reload.
  await frame.locator('#go-section').click();
  await page.waitForURL(`**/#${CID}/locked#section-two`);
  await page.reload();
  await page.waitForURL(`**/#${CID}/locked#section-two`);
  assert.equal(await readout(), PREFIX + '/locked#section-two');

  // 9. The shim's own address is Ava's, not a page of the app's, and must never
  // become somewhere the frame is reopened — `/.ava/` is reserved and the shell
  // refuses to remember it.
  const remembered = await page.evaluate(() => window.localStorage.getItem('ava.appPaths'));
  assert.ok(remembered && !remembered.includes('.ava'),
    'A path under the reserved namespace must never be remembered: ' + remembered);
  assert.equal(JSON.parse(remembered!)[CID], '/locked#section-two');
  assert.ok(!page.url().includes('.ava'));

  assert.deepEqual(errors, []);
  console.log('PASS: an app that speaks no protocol of its own keeps its page across reloads, '
    + 'hash changes, in-frame navigations, Back/Forward and a reload taken on another tab — '
    + 'inline where the app allows it, as a same-origin file where its CSP does not, '
    + 'and never at the cost of a second embed lease');
} finally {
  await browser.close();
}
