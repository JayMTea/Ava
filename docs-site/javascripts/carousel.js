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

  function boot() {
    document.querySelectorAll('.ava-carousel').forEach(init);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
