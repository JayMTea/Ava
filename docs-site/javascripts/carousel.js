// Lightweight, dependency-free carousel for the landing-page screenshots.
// Arrows, dots, keyboard (left/right), swipe, and gentle autoplay that pauses on
// hover/focus. No-ops if the page has no .ava-carousel.
(function () {
  function init(root) {
    var track = root.querySelector('.ava-carousel__track');
    var slides = Array.prototype.slice.call(root.querySelectorAll('.ava-slide'));
    var prev = root.querySelector('.ava-carousel__arrow--prev');
    var next = root.querySelector('.ava-carousel__arrow--next');
    var caption = root.querySelector('.ava-carousel__caption');
    var dotsWrap = root.querySelector('.ava-carousel__dots');
    if (!track || slides.length === 0) return;

    var i = 0;
    var timer = null;

    var dots = slides.map(function (_, n) {
      var b = document.createElement('button');
      b.type = 'button';
      b.setAttribute('role', 'tab');
      b.setAttribute('aria-label', 'Screenshot ' + (n + 1));
      b.addEventListener('click', function () { go(n); });
      dotsWrap.appendChild(b);
      return b;
    });

    function render() {
      track.style.transform = 'translateX(' + (-i * 100) + '%)';
      if (caption) {
        var s = slides[i];
        caption.innerHTML = '<strong>' + (s.dataset.title || '') + '</strong> ' + (s.dataset.caption || '');
      }
      dots.forEach(function (d, n) {
        var on = n === i;
        d.setAttribute('aria-selected', on ? 'true' : 'false');
        d.classList.toggle('is-active', on);
      });
      slides.forEach(function (s, n) { s.setAttribute('aria-hidden', n === i ? 'false' : 'true'); });
    }
    function go(n) { i = (n + slides.length) % slides.length; render(); restart(); }
    function start() { timer = window.setInterval(function () { go(i + 1); }, 6000); }
    function stop() { if (timer) { clearInterval(timer); timer = null; } }
    function restart() { stop(); start(); }

    if (prev) prev.addEventListener('click', function () { go(i - 1); });
    if (next) next.addEventListener('click', function () { go(i + 1); });

    root.setAttribute('tabindex', '0');
    root.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowLeft') { go(i - 1); e.preventDefault(); }
      else if (e.key === 'ArrowRight') { go(i + 1); e.preventDefault(); }
    });

    var x0 = null;
    root.addEventListener('touchstart', function (e) { x0 = e.touches[0].clientX; }, { passive: true });
    root.addEventListener('touchend', function (e) {
      if (x0 === null) return;
      var dx = e.changedTouches[0].clientX - x0; x0 = null;
      if (Math.abs(dx) > 40) go(i + (dx < 0 ? 1 : -1));
    });

    root.addEventListener('mouseenter', stop);
    root.addEventListener('mouseleave', start);
    root.addEventListener('focusin', stop);
    root.addEventListener('focusout', start);

    render();
    start();
  }

  // Corner "expand" buttons -> native fullscreen of their target element.
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

  // Render the real desktop app (design 1280x820) scaled to fit the demo box, so
  // it stays the full dashboard, just smaller. Fullscreen is handled by CSS.
  var DESIGN_W = 1280, DESIGN_H = 820;
  function scaleDemo() {
    var vp = document.querySelector('.ava-demo__viewport');
    var frame = document.querySelector('.ava-demo__iframe');
    if (!vp || !frame) return;
    if (document.fullscreenElement) { frame.style.transform = ''; vp.style.height = ''; return; }
    var w = vp.clientWidth;
    if (w < 620) {
      // Narrow screens: let the app use its own responsive layout, no scaling.
      frame.style.width = w + 'px';
      frame.style.height = '620px';
      frame.style.transform = '';
      vp.style.height = '';
    } else {
      frame.style.width = DESIGN_W + 'px';
      frame.style.height = DESIGN_H + 'px';
      var s = w / DESIGN_W;
      frame.style.transform = 'scale(' + s + ')';
      vp.style.height = Math.round(DESIGN_H * s) + 'px';
    }
  }

  var rt = null;
  function onResize() { if (rt) clearTimeout(rt); rt = setTimeout(scaleDemo, 120); }

  function boot() {
    document.querySelectorAll('.ava-carousel').forEach(init);
    wireExpand();
    scaleDemo();
    window.addEventListener('resize', onResize);
    document.addEventListener('fullscreenchange', scaleDemo);
    document.addEventListener('webkitfullscreenchange', scaleDemo);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
