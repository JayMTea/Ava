"""Run the injected route-memory shim and watch what it posts.

`ava_bridge/assets/app_route.js` is the half of this feature that lives in
somebody else's document. Nothing on the host executes it, so every host-side
check on it is a check on a string — which is exactly the blind spot
tests/test_connector_codegen.py was written for, and it is answered the same
way: `node`, a recorded harness, and assertions on what the code actually did.

The file is a real .js rather than a Python string so this can run it. It uses
only `window`, `history`, `location` and `setTimeout`, so the harness hands it
those four and nothing else — no jsdom, no new dependency — and the timer is a
queue the harness drains by hand, which is what makes "coalesces" and
"de-duplicates" observable rather than a race.

The case that decides the DESIGN is `capturedBefore` / `capturedAfter`: a script
that captured `window.history.pushState` after the shim still routes through the
shim's wrapper, and one that captured it before does not. Next.js's app router
captures at startup and calls its captured reference, so the shim has to be
inline and first in `<head>` — four of the six apps this was measured against
are that router. A regression that moves the tag later would leave every test
that only inspects the source passing.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from ava_bridge import config, embed_route

SHELL = "https://ava.example"
APPS = "https://apps.example"
CID = "demo"

HARNESS = r"""
import { readFileSync } from 'node:fs';

const sources = {
  configured: readFileSync(new URL('./shim.js', import.meta.url), 'utf8'),
  unset: readFileSync(new URL('./shim-unset.js', import.meta.url), 'utf8'),
};

// A frame, in the four globals the shim actually uses. `setTimeout` is a queue
// the test drains, so coalescing is observed rather than waited for.
function makeFrame(opts) {
  const timers = [], posts = [], listeners = new Map();
  const loc = {};
  loc.go = (next) => {
    const u = new URL(next, loc.href || opts.href);
    loc.href = u.href; loc.origin = u.origin;
    loc.pathname = u.pathname; loc.search = u.search; loc.hash = u.hash;
  };
  loc.go(opts.href);
  if (opts.ancestorOrigins) loc.ancestorOrigins = opts.ancestorOrigins;

  const parent = { postMessage: (data, origin) => posts.push({ data, origin }) };
  if (opts.parentOrigin) parent.location = { origin: opts.parentOrigin };
  else Object.defineProperty(parent, 'location',
    { get() { throw new Error('cross-origin'); } });

  const history = {
    pushState(_s, _t, url) { loc.go(url); },
    replaceState(_s, _t, url) { loc.go(url); },
  };
  if (opts.frozenHistory) {
    // What a browser extension that redefined pushState with
    // Object.defineProperty leaves behind, and what a frozen history is.
    Object.freeze(history);
  }
  const window = {
    addEventListener(type, fn) {
      if (!listeners.has(type)) listeners.set(type, []);
      listeners.get(type).push(fn);
    },
    removeEventListener(type, fn) {
      const list = listeners.get(type) || [];
      const at = list.indexOf(fn);
      if (at >= 0) list.splice(at, 1);
    },
  };
  window.parent = opts.framed === false ? window : parent;
  if (opts.navigationApi) {
    window.navigation = { addEventListener: (t, fn) => window.addEventListener(t, fn) };
  }
  return {
    window, history, location: loc, posts, parent,
    drain() { while (timers.length) timers.shift()(); },
    fire(type, event) { (listeners.get(type) || []).slice().forEach(fn => fn(event || {})); },
    start() {
      const run = new Function('window', 'history', 'location', 'setTimeout',
                               sources[opts.source || 'configured']);
      run(window, history, loc, (fn) => timers.push(fn));
    },
  };
}

const routes = (frame) => frame.posts.filter(p => p.data.type === 'ava:navigation');
const out = {};

// Where the frame opened is the first thing worth saying — with the launch
// parameters gone and the fragment kept.
{
  const f = makeFrame({
    href: SHELL + '/apps/demo/machine-learning?t=tok&theme=dark&embedded=1&v=1&tab=models#pane',
    parentOrigin: SHELL });
  f.start(); f.drain();
  out.load = f.posts;
  const api = f.window.__avaShell;
  out.api = { version: api.version, cid: api.cid, origin: api.origin,
              report: typeof api.report };
}

// A query string is reported EXACTLY as the app wrote it, minus the launch
// parameters. This string is what the shell stores and what the frame is
// reopened at, so re-serialising it hands the app a URL it never produced.
{
  out.queries = [
    '?q=hello%20world&theme=dark',
    '?filter=kind:pod,ns:default',
    '?flag',
    '?t=tok',
    '?a=1&b=2',
  ].map((query) => {
    const f = makeFrame({ href: SHELL + '/apps/demo/x' + query,
                          parentOrigin: SHELL });
    f.start(); f.drain();
    return routes(f).map(p => p.data.path)[0] || null;
  });
}

// A history whose pushState cannot be assigned to. In strict mode that throws,
// and everything after it -- the listeners, the shell resolution, the first
// report -- would never run, in somebody ELSE's document.
{
  const f = makeFrame({ href: SHELL + '/apps/demo/a', parentOrigin: SHELL,
                        frozenHistory: true });
  f.start(); f.drain();
  const api = f.window.__avaShell;
  f.location.go('/apps/demo/b'); f.fire('popstate'); f.drain();
  out.frozenHistory = { paths: routes(f).map(p => p.data.path),
                        origin: api ? api.origin : null };
}

// Every way an SPA changes its address.
{
  const f = makeFrame({ href: SHELL + '/apps/demo/', parentOrigin: SHELL });
  f.start(); f.drain();
  f.history.pushState(null, '', '/apps/demo/a'); f.drain();
  f.history.replaceState(null, '', '/apps/demo/b'); f.drain();
  f.location.go('/apps/demo/c'); f.fire('popstate'); f.drain();
  f.location.go('/apps/demo/c#x'); f.fire('hashchange'); f.drain();
  out.changes = routes(f).map(p => p.data.path);
  out.targets = [...new Set(f.posts.map(p => p.origin))];
}

// A burst of router calls is one message, and re-stating a route is none.
{
  const f = makeFrame({ href: SHELL + '/apps/demo/', parentOrigin: SHELL });
  f.start();
  f.history.pushState(null, '', '/apps/demo/a');
  f.history.pushState(null, '', '/apps/demo/b');
  f.history.replaceState(null, '', '/apps/demo/c');
  f.drain();
  out.coalesced = routes(f).map(p => p.data.path);
  f.history.pushState(null, '', '/apps/demo/c'); f.drain();
  out.deduped = routes(f).length;
}

// The Navigation API commits without pushState or popstate.
{
  const f = makeFrame({ href: SHELL + '/apps/demo/', parentOrigin: SHELL,
                        navigationApi: true });
  f.start(); f.drain();
  f.location.go('/apps/demo/n'); f.fire('navigatesuccess'); f.drain();
  out.navigationApi = routes(f).map(p => p.data.path);
}

// Not framed at all: no shell, nothing to say, and nothing published.
{
  const f = makeFrame({ href: SHELL + '/apps/demo/x', framed: false });
  f.start(); f.drain();
  out.unframed = { posts: f.posts.length, api: f.window.__avaShell === undefined };
}

// An address outside the app's own mount is not this app's page.
{
  const f = makeFrame({ href: SHELL + '/elsewhere', parentOrigin: SHELL });
  f.start(); f.drain();
  out.outsidePrefix = routes(f).length;
}

// The browser naming our ancestor settles it either way.
{
  const f = makeFrame({ href: APPS + '/apps/demo/a',
                        ancestorOrigins: ['https://somewhere-else.example'] });
  f.start(); f.drain();
  out.wrongAncestor = f.posts.length;
}
{
  const f = makeFrame({ href: APPS + '/apps/demo/a', ancestorOrigins: [SHELL] });
  f.start(); f.drain();
  out.rightAncestor = routes(f).map(p => ({ path: p.data.path, origin: p.origin }));
}
{
  const f = makeFrame({ href: APPS + '/apps/demo/a', source: 'unset',
                        ancestorOrigins: ['http://192.0.2.7:8096'] });
  f.start(); f.drain();
  out.nothingConfigured = routes(f).map(p => p.origin);
}

// The handshake, for a cross-origin parent the browser will not name.
{
  const f = makeFrame({ href: APPS + '/apps/demo/a' });
  f.start(); f.drain();
  out.handshakeAsk = f.posts.map(p => ({ type: p.data.type, origin: p.origin }));
  f.fire('message', { source: f.parent, origin: SHELL,
                      data: { type: 'ava:theme', theme: 'dark' } });
  f.drain();
  out.handshakeAnswered = routes(f).map(p => ({ path: p.data.path, origin: p.origin }));
}
{
  const f = makeFrame({ href: APPS + '/apps/demo/a' });
  f.start(); f.drain();
  f.fire('message', { source: f.parent, origin: 'https://somewhere-else.example',
                      data: { type: 'ava:theme' } });
  f.drain();
  out.handshakeWrongOrigin = routes(f).length;
}

// Why the tag is inline and FIRST.
{
  const f = makeFrame({ href: SHELL + '/apps/demo/', parentOrigin: SHELL });
  const before = f.history.pushState;     // a bundle that ran ahead of the shim
  f.start(); f.drain();
  const after = f.history.pushState;      // one that ran behind it
  const settled = routes(f).length;
  before.call(f.history, null, '', '/apps/demo/early'); f.drain();
  out.capturedBefore = routes(f).length - settled;
  after.call(f.history, null, '', '/apps/demo/late'); f.drain();
  out.capturedAfter = routes(f).slice(settled).map(p => p.data.path);
}

console.log(JSON.stringify(out));
"""


def _shim(shell: str) -> str:
    with mock.patch("ava_bridge.apps_origin.shell_origin", return_value=shell):
        return embed_route.shim(CID)


@unittest.skipUnless(shutil.which("node"), "node not installed")
class ShimBehaviourTests(unittest.TestCase):
    out: dict

    @classmethod
    def setUpClass(cls):
        tmp = tempfile.mkdtemp(prefix="ava-shim-")
        cls.addClassCleanup(shutil.rmtree, tmp, ignore_errors=True)
        files = {
            "shim.js": _shim(SHELL),
            "shim-unset.js": _shim(""),
            "run.mjs": (f"const SHELL = {json.dumps(SHELL)};\n"
                        f"const APPS = {json.dumps(APPS)};\n" + HARNESS),
        }
        for name, body in files.items():
            with open(os.path.join(tmp, name), "w", encoding="utf-8") as f:
                f.write(body)
        r = subprocess.run(["node", "run.mjs"], cwd=tmp,
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        cls.out = json.loads(r.stdout.strip().splitlines()[-1])

    def test_it_reports_where_the_frame_opened(self):
        self.assertEqual(self.out["load"], [{
            "data": {"type": "ava:navigation", "cid": CID,
                     "path": "/machine-learning?tab=models#pane"},
            "origin": SHELL}],
            "the launch parameters describe the mount, not the page — and `t` "
            "is a credential; the fragment is part of where the owner was")

    def test_it_publishes_the_shell_it_found(self):
        self.assertEqual(self.out["api"],
                         {"version": 1, "cid": CID, "origin": SHELL,
                          "report": "function"},
                         "an app has to be able to find its shell, and a "
                         "self-reporting app has to be able to stand down")

    def test_a_query_string_survives_byte_for_byte(self):
        self.assertEqual(
            self.out["queries"],
            ["/x?q=hello%20world", "/x?filter=kind:pod,ns:default", "/x?flag",
             "/x", "/x?a=1&b=2"],
            "URLSearchParams re-serialises every survivor — `%20` becomes `+`, "
            "`:` and `,` become `%3A`/`%2C`, a bare flag gains `=` — and this "
            "string is what the frame is REOPENED at")

    def test_a_history_it_cannot_patch_does_not_take_the_rest_down(self):
        self.assertEqual(
            self.out["frozenHistory"], {"paths": ["/a", "/b"], "origin": SHELL},
            "a non-writable pushState makes the assignment throw in strict "
            "mode, and a script Ava injects into somebody else's document must "
            "never throw into it — popstate still reports, and the shell is "
            "still resolved")

    def test_every_way_an_spa_changes_its_address_is_heard(self):
        self.assertEqual(self.out["changes"],
                         ["/", "/a", "/b", "/c", "/c#x"])
        self.assertEqual(self.out["navigationApi"], ["/", "/n"])

    def test_it_posts_to_the_configured_shell_and_to_nothing_else(self):
        self.assertEqual(self.out["targets"], [SHELL])
        self.assertEqual(self.out["rightAncestor"],
                         [{"path": "/a", "origin": SHELL}])

    def test_a_burst_is_one_message_and_a_repeat_is_none(self):
        self.assertEqual(self.out["coalesced"], ["/c"],
                         "a router commonly replaces its own entry right after "
                         "pushing one; the shell should hear where it landed")
        self.assertEqual(self.out["deduped"], 1)

    def test_it_stays_silent_when_there_is_nothing_to_report_to(self):
        self.assertEqual(self.out["unframed"], {"posts": 0, "api": True})
        self.assertEqual(self.out["outsidePrefix"], 0)

    def test_a_framer_that_is_not_the_shell_is_told_nothing(self):
        self.assertEqual(self.out["wrongAncestor"], 0,
                         "a page that frames the app elsewhere must not be "
                         "told where the owner is inside it")
        self.assertEqual(self.out["handshakeWrongOrigin"], 0)

    def test_with_no_shell_configured_the_browsers_own_answer_is_taken(self):
        self.assertEqual(self.out["nothingConfigured"], ["http://192.0.2.7:8096"],
                         "server.public_url unset leaves nothing to check "
                         "against, and the browser's fact is all there is")

    def test_the_handshake_carries_nothing_and_is_the_only_unaddressed_message(self):
        self.assertEqual(self.out["handshakeAsk"],
                         [{"type": "ava:theme-request", "origin": "*"}])
        self.assertEqual(self.out["handshakeAnswered"],
                         [{"path": "/a", "origin": SHELL}])

    def test_a_bundle_that_captured_pushstate_after_the_shim_still_routes_through_it(self):
        self.assertEqual(self.out["capturedAfter"], ["/late"])
        self.assertEqual(
            self.out["capturedBefore"], 0,
            "this is the whole reason the tag is inline and first in <head>: a "
            "router holding the ORIGINAL pushState never reaches the wrapper, "
            "and Next.js's app router captures at startup")


class ShimSourceIsRunnableWithoutNode(unittest.TestCase):
    """Runs on a minimal image, so a regression is caught even without node."""

    def test_the_shim_needs_only_the_four_globals_the_harness_supplies(self):
        with mock.patch.object(config, "PUBLIC_URL", SHELL):
            src = embed_route.shim(CID)
        # Comments stripped, because they are where the rest of the document is
        # DISCUSSED — the check is on what the code reaches for.
        code = "\n".join(line.split("//")[0] for line in src.splitlines())
        for name in ("document.", "require(", "fetch(", "localStorage",
                     "requestAnimationFrame"):
            self.assertNotIn(name, code,
                             f"{name!r} would make the shim need more of the "
                             "page than a script injected into somebody else's "
                             "document may assume")


if __name__ == "__main__":
    unittest.main()
