// Landing figure: play the convergence reveal once, then never again.
//
// WHAT THIS FILE MAY AND MAY NOT DO. It adds one class. That is the whole
// contract. The finished picture is the AUTHORED state of .ava-conv in
// stylesheets/extra.css, and every keyframe there ends on exactly that state,
// so this script can only ever add motion on top of something already correct.
// If it 404s, throws, is blocked, or never runs, the visitor sees the finished
// figure and loses nothing but the animation.
//
// That direction is not a style preference, it is the lesson of this repo's
// own history. Authoring the SCATTERED state as the default and having JS tidy
// it up is the obvious way to write this, and it turns every one of those
// failures into a blank LCP region on the most-visited page. mkdocs does not
// verify that a file listed in extra_javascript exists, sync.py copies the
// working tree rather than git ls-files, and a landing page here has already
// shipped a <video> whose source AND poster both 404'd behind a green build.
// Fail settled, never blank.
//
// WHY NOT AN IntersectionObserver ALONE. The figure sits just under the hero
// and is usually already on screen. An observer delivers its first callback
// after a render opportunity, so an observer-only trigger would paint the
// settled figure, then jump it back to scattered and replay - a visible
// flinch. Deciding "already in view" synchronously, in the same task as this
// script, avoids that: scripts load at the end of <body> with no defer and the
// document is ~40 KB with no blocking subresources of its own, so this runs
// before first paint on essentially every load. On a slow enough connection
// the parser may yield and paint first, and the reveal then reads as a replay.
// Not broken, and strictly better than risking a blank.
//
// Never a threshold of 1.0: the composition is taller than a phone viewport,
// so full intersection is unreachable and the callback would never fire.
//
// FORWARD NOTE. mkdocs.yml does not enable navigation.instant. If it ever is,
// this boot runs once for the first document only and the figure will not
// animate on an instant-navigated page; it would need a document$
// subscription. chrome.js and diagram-zoom.js carry the same exposure, so that
// is a sitewide decision rather than a change to this file alone.
(function () {
  function play(el) {
    el.classList.add('is-playing');
  }

  function boot() {
    var el = document.querySelector('.ava-conv');
    if (!el || el.classList.contains('is-playing')) return;

    // Reduced motion never builds an observer and never adds the class. The
    // CSS carries the same query as a belt, but the rest state is already the
    // whole picture, so there is nothing to substitute and nothing is lost.
    if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;

    var vh = window.innerHeight || document.documentElement.clientHeight;
    var box = el.getBoundingClientRect();
    if (box.top < vh * 0.9 && box.bottom > 0) { play(el); return; }

    if (!window.IntersectionObserver) { play(el); return; }

    var io = new IntersectionObserver(function (entries) {
      for (var i = 0; i < entries.length; i++) {
        if (!entries[i].isIntersecting) continue;
        play(entries[i].target);
        io.disconnect();
        return;
      }
    }, { threshold: 0.25 });
    io.observe(el);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
