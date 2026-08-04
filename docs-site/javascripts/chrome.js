// The header home link needs no JS: the brand wordmark now occupies the logo
// slot (stylesheets/extra.css masks docs/assets/ava-wordmark.svg onto
// a.md-logo), and that anchor is already a real link to the site root with an
// accessible name. The previous block here retrofitted the "Ava" text title
// into a fake link because the logo button was hidden — that is no longer the
// case, and a genuine <a> beats role="link" on a <span>.

// "View on GitHub" pill, left of the search box. The stock repo widget is
// hidden via CSS; its anchor still supplies the URL (fork-safe).
(function () {
  function boot() {
    var search = document.querySelector('.md-header .md-header__search') ||
                 document.querySelector('.md-header [data-md-component="search"]');
    var src = document.querySelector('.md-header__source a.md-source');
    if (!search || !src || document.querySelector('.ava-gh-btn')) return;
    var a = document.createElement('a');
    a.className = 'ava-gh-btn';
    a.href = src.getAttribute('href');
    a.target = '_blank';
    a.rel = 'noopener';
    a.innerHTML = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27s1.36.09 2 .27c1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8z"/></svg><span>View on GitHub</span>';
    search.parentNode.insertBefore(a, search);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();

// Corner "expand" buttons -> native fullscreen of their target element.
//
// This lived in javascripts/carousel.js. When the landing page dropped its
// gallery for the single <video class="ava-reel"> embed, carousel.js was taken
// out of mkdocs.yml's extra_javascript as "a 93-line no-op" — but it was not a
// no-op: it also owned this handler. overrides/home.html still renders the
// button, stylesheets/extra.css still styles it, and it still announces itself
// as "Expand video", so for every visitor it was a live-looking control that
// did nothing. It lives here now because chrome.js is the file that is always
// loaded. If a gallery ever comes back and carousel.js is loaded again, do not
// re-add wireExpand() THERE, or the listener binds twice and the second click
// immediately exits fullscreen.
(function () {
  function wireExpand() {
    document.querySelectorAll('[data-expand-target]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var t = document.querySelector(btn.getAttribute('data-expand-target'));
        if (!t) return;
        if (document.fullscreenElement) {
          (document.exitFullscreen || document.webkitExitFullscreen || function () {}).call(document);
          return;
        }
        var req = t.requestFullscreen || t.webkitRequestFullscreen || t.msRequestFullscreen;
        if (req) { var p = req.call(t); if (p && p.catch) p.catch(function () {}); }
      });
    });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', wireExpand);
  else wireExpand();
})();
