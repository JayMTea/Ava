// Route memory for an embedded connector app, run INSIDE the app's own document.
//
// The shell can remember where an app was left, but only if something tells it.
// After a top-level reload the iframe always re-loads its `src` and its in-page
// location is never restored, and a client-side route change makes no HTTP
// request at all -- so the bridge cannot see it and the parent cannot read a
// cross-origin frame's location. The only code that can observe an SPA
// navigation is code in the app's document. Asking every app author to add that
// code makes route memory a feature only the apps that opted in have, so the
// proxy adds this file for them (ava_bridge/embed_route.py) and it speaks the
// same `ava:navigation` protocol an app would send by hand -- the shell
// (frontend/src/lib/embedNavigation.ts) validates it identically and does not
// know or care which of the two sent it.
//
// `__CID__` and `__SHELL__` are substituted by embed_route.shim() before this
// ever reaches a browser; both arrive as complete JSON literals, never as
// fragments spliced into one. Unsubstituted, the file is still parseable JS, so
// `node --check` is a real check on it.
//
// ASCII ONLY. The tag is spliced into a byte stream whose encoding is whatever
// the app declared, and an ASCII body is the one thing every encoding the gate
// admits (UTF-8, latin-1, windows-1252 -- never UTF-16) agrees on.
(function () {
  'use strict';

  var CID = __CID__;
  var SHELL = __SHELL__;

  // Not framed: `parent` is us, there is no shell, and there is nothing to say.
  if (window.parent === window) return;
  // Installed twice -- a second injection, or an app that ships its own copy of
  // this file. One reporter is enough and two would double every message.
  if (window.__avaShell) return;

  var PREFIX = '/apps/' + CID;
  // The launch parameters the shell puts in the frame's `src`. They describe
  // THIS mount, not the page the owner is on, and `t` is a credential -- a
  // remembered address carrying a spent embed token is a URL that cannot be
  // reopened and a token in the browser's history for no reason. The shell
  // strips these again on arrival; stripping them here keeps the message itself
  // clean, which is what an app reading `__avaShell` sees.
  var DROP = /^(t|theme|embedded|v)$/;

  var origin = '';   // the ONE target we ever post to; '' until it is proven
  var last = null;   // what we said last, so a router that re-states a route is free
  var timer = 0;

  // The same pairs minus the launch ones, BYTE FOR BYTE for the rest.
  //
  // Deliberately not `URLSearchParams`: writing through it re-serialises every
  // survivor -- `%20` becomes `+`, `:` and `,` become `%3A`/`%2C`, and a bare
  // flag gains `=`. This string is what the shell stores and what the frame is
  // REOPENED at, so a re-serialised one hands the app back a URL its own router
  // never produced. Only the KEY is decoded, and only to judge it. The shell
  // filters the raw pairs the same way and for the same reason
  // (frontend/src/lib/embedNavigation.ts).
  function withoutLaunch(raw) {
    var body = raw.slice(1);
    if (!body) return '';
    var pairs = body.split('&');
    var kept = [];
    for (var i = 0; i < pairs.length; i++) {
      var key = pairs[i].split('=')[0];
      var name = key;
      try {
        name = decodeURIComponent(key.replace(/\+/g, ' '));
      } catch (e) {
        // undecodable: judge it by the spelling it arrived in
      }
      if (!DROP.test(name)) kept.push(pairs[i]);
    }
    if (kept.length === pairs.length) return raw;   // nothing dropped: as it came
    return kept.length ? raw.charAt(0) + kept.join('&') : '';
  }

  // Where the app is, relative to its own mount -- the shape the shell validates:
  // leading slash, query and fragment kept, launch parameters gone. `null` when
  // the address is outside the app's prefix, which is not this app's page and
  // not ours to report.
  function currentPath() {
    var p = location.pathname;
    if (p !== PREFIX && p.slice(0, PREFIX.length + 1) !== PREFIX + '/') return null;
    var rel = p.slice(PREFIX.length) || '/';
    // The fragment is passed on exactly as it arrived: it is the app's own, the
    // shell strips credentials out of a parameter-shaped one on arrival, and
    // re-encoding it here would have the same effect as re-encoding the query.
    return rel + (location.search ? withoutLaunch(location.search) : '')
               + location.hash;
  }

  function send() {
    timer = 0;
    if (!origin) return;
    var next = currentPath();
    if (next === null || next === last) return;
    last = next;
    try {
      window.parent.postMessage({ type: 'ava:navigation', cid: CID, path: next }, origin);
    } catch (e) {
      // A frame torn down mid-report is an ordinary end, not a failure worth
      // throwing into the app's own error handlers.
    }
  }

  // Coalesce with a timer, NOT requestAnimationFrame. The shell keeps visited
  // tiles mounted and hides the inactive ones with `display:none`, and a frame
  // that is not laid out may never be handed an animation frame -- the app would
  // navigate and the shell would never hear about it. Coalescing still earns its
  // keep, because a router commonly replaces its own entry immediately after
  // pushing one.
  function report() {
    if (!timer) timer = setTimeout(send, 0);
  }

  function adopt(value) {
    origin = value;
    api.origin = value;
    report();
  }

  // A browser-stated origin is a fact about WHO FRAMED US; it is not a fact that
  // the framer is Ava. Believe it only when it is the shell this bridge is
  // configured for, so an app embedded somewhere else is never told where the
  // owner is inside it. With `server.public_url` unset there is nothing to check
  // against and the browser's fact is all there is.
  function agrees(candidate) {
    return usable(candidate) && (!SHELL || candidate === SHELL);
  }

  // An opaque origin serialises as the STRING "null", which is truthy and is not
  // a legal `targetOrigin` -- postMessage throws on it. Nothing to report to.
  function usable(candidate) {
    return !!candidate && candidate !== 'null';
  }

  function onMessage(event) {
    if (event.source !== window.parent) return;
    var data = event.data;
    if (!data || data.type !== 'ava:theme') return;
    window.removeEventListener('message', onMessage);
    if (agrees(event.origin)) adopt(event.origin);
  }

  function resolveShell() {
    // 1. A READABLE parent location. It is readable only when the parent is
    //    same-origin with this document -- which is the document Ava's own proxy
    //    just served, so nothing but Ava can be on that origin. Self-proving,
    //    and therefore trusted without matching `SHELL`: `server.public_url`
    //    defaults to `http://localhost:<port>`, so demanding a match here would
    //    silently switch route memory off for every install reached by its LAN
    //    address or hostname -- the common single-origin case. With `apps.origin`
    //    set this throws instead, and the checked paths below take over.
    try {
      var here = window.parent.location.origin;
      if (usable(here)) return adopt(here);
    } catch (e) {
      // cross-origin parent: expected, and the answer "not this way"
    }
    // 2. The browser naming our ancestor. Available where implemented, and it
    //    settles the question either way: a match is the target, a mismatch
    //    means this frame is not in Ava and asking around would only find
    //    somebody willing to answer.
    try {
      var chain = location.ancestorOrigins;
      if (chain && chain.length) {
        if (agrees(chain[0])) adopt(chain[0]);
        return;
      }
    } catch (e) {
      // not implemented here
    }
    // 3. The handshake. `ava:theme-request` carries nothing, so it is the one
    //    message that may go out unaddressed; the shell answers `ava:theme` and
    //    the BROWSER stamps `event.origin` on the reply. Deliberately not
    //    `document.referrer`: it is emptied by a redirect on the way in and,
    //    after any full navigation inside the frame, names the app's own
    //    previous page.
    window.addEventListener('message', onMessage);
    try { window.parent.postMessage({ type: 'ava:theme-request' }, '*'); } catch (e) {}
  }

  // What an app can find out about its shell, and how a self-reporting app can
  // stand down rather than send every message twice.
  var api = {
    version: 1,
    cid: CID,
    origin: '',
    // For a route the address bar does not carry -- one held in the app's own
    // memory. Everything in the address bar is already covered below.
    report: report
  };
  try {
    Object.defineProperty(window, '__avaShell', { value: api, configurable: true });
  } catch (e) {
    window.__avaShell = api;
  }

  // Patched HERE, before the app's bundle runs, which is why the bridge puts
  // this tag inline and first in <head>. Next.js's app router reads
  // `window.history.pushState` once at startup and calls the reference it
  // captured, so a wrapper installed afterwards is never reached: the app would
  // navigate, the address would change, and nothing would hear it.
  //
  // The assignment is GUARDED because it is an assignment into somebody else's
  // page: a browser extension that redefined `pushState` with
  // `Object.defineProperty`, or a frozen `history`, makes it non-writable, and
  // a non-writable assignment in strict mode throws -- which would abandon the
  // rest of this file (the listeners, the shell resolution, the first report)
  // and throw into the app's own error handlers on top. A script Ava injects
  // into somebody else's document must never do that; losing the wrapper costs
  // route memory for `pushState` only, and popstate/hashchange still report.
  ['pushState', 'replaceState'].forEach(function (name) {
    var original = history[name];
    if (typeof original !== 'function') return;
    try {
      history[name] = function () {
        var out = original.apply(this, arguments);
        report();
        return out;
      };
    } catch (e) {
      // not ours to patch: carry on with everything else
    }
  });
  window.addEventListener('popstate', report);
  window.addEventListener('hashchange', report);
  // The Navigation API commits an entry without either of the above, and
  // Chromium ships it. `navigatesuccess` fires after the commit, so `location`
  // already reads as the new page.
  if (window.navigation && typeof window.navigation.addEventListener === 'function') {
    window.navigation.addEventListener('navigatesuccess', report);
  }

  resolveShell();
  report();   // where the frame opened is the first thing worth saying
})();
