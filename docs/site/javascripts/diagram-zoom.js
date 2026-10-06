/* Zoom/pan lightbox for the site's diagram SVGs (architecture, system,
   network, security, agent-runtime). Click a diagram to open it fullscreen;
   scroll or +/- to zoom, drag to pan, double-click or 0 to reset, Esc to
   close. Dependency-free; markdown wraps diagrams in <a href="*.svg"> links,
   which we intercept. */
(function () {
  'use strict';

  var SCALE_MIN = 0.2;
  var SCALE_MAX = 40;

  function buildOverlay() {
    var overlay = document.createElement('div');
    overlay.className = 'dz-overlay';
    overlay.setAttribute('role', 'dialog');
    overlay.setAttribute('aria-label', 'Diagram viewer');
    overlay.innerHTML =
      '<div class="dz-stage"><img class="dz-img" alt="Diagram"></div>' +
      '<div class="dz-toolbar">' +
      '<button class="dz-btn" data-dz="out" title="Zoom out" aria-label="Zoom out">&minus;</button>' +
      '<button class="dz-btn" data-dz="in" title="Zoom in" aria-label="Zoom in">+</button>' +
      '<button class="dz-btn" data-dz="reset" title="Reset view" aria-label="Reset view">Reset</button>' +
      '<button class="dz-btn" data-dz="close" title="Close (Esc)" aria-label="Close">&times;</button>' +
      '</div>' +
      '<div class="dz-hint">Scroll to zoom &middot; drag to pan &middot; double-click to reset</div>';
    document.body.appendChild(overlay);
    return overlay;
  }

  var overlay = null, stage = null, img = null;
  var scale = 1, tx = 0, ty = 0;
  var dragging = false, lastX = 0, lastY = 0;
  var pointers = {}, pinchDist = 0;

  function apply() {
    img.style.transform = 'translate(' + tx + 'px,' + ty + 'px) scale(' + scale + ')';
  }

  function reset() { scale = 1; tx = 0; ty = 0; apply(); }

  function zoomAt(px, py, factor) {
    var next = Math.min(SCALE_MAX, Math.max(SCALE_MIN, scale * factor));
    factor = next / scale;
    // Keep the point under the cursor fixed (coords relative to stage centre).
    tx = px - factor * (px - tx);
    ty = py - factor * (py - ty);
    scale = next;
    apply();
  }

  function stagePoint(e) {
    var r = stage.getBoundingClientRect();
    return { x: e.clientX - r.left - r.width / 2, y: e.clientY - r.top - r.height / 2 };
  }

  function close() {
    overlay.classList.remove('dz-open');
    document.documentElement.classList.remove('dz-lock');
  }

  function open(src, alt) {
    if (!overlay) {
      overlay = buildOverlay();
      stage = overlay.querySelector('.dz-stage');
      img = overlay.querySelector('.dz-img');
      wire();
    }
    img.src = src;
    if (alt) img.alt = alt;
    reset();
    overlay.classList.add('dz-open');
    document.documentElement.classList.add('dz-lock');
  }

  function wire() {
    overlay.addEventListener('wheel', function (e) {
      e.preventDefault();
      var p = stagePoint(e);
      zoomAt(p.x, p.y, e.deltaY < 0 ? 1.2 : 1 / 1.2);
    }, { passive: false });

    stage.addEventListener('pointerdown', function (e) {
      pointers[e.pointerId] = e;
      if (Object.keys(pointers).length === 1) {
        dragging = true; lastX = e.clientX; lastY = e.clientY;
        stage.classList.add('dz-grabbing');
      }
      stage.setPointerCapture(e.pointerId);
    });
    stage.addEventListener('pointermove', function (e) {
      if (pointers[e.pointerId]) pointers[e.pointerId] = e;
      var ids = Object.keys(pointers);
      if (ids.length === 2) {           // pinch zoom
        var a = pointers[ids[0]], b = pointers[ids[1]];
        var d = Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY);
        if (pinchDist) {
          var mid = { clientX: (a.clientX + b.clientX) / 2, clientY: (a.clientY + b.clientY) / 2 };
          var p = stagePoint(mid);
          zoomAt(p.x, p.y, d / pinchDist);
        }
        pinchDist = d;
      } else if (dragging) {
        tx += e.clientX - lastX; ty += e.clientY - lastY;
        lastX = e.clientX; lastY = e.clientY;
        apply();
      }
    });
    function release(e) {
      delete pointers[e.pointerId];
      pinchDist = 0;
      if (!Object.keys(pointers).length) {
        dragging = false;
        stage.classList.remove('dz-grabbing');
      }
    }
    stage.addEventListener('pointerup', release);
    stage.addEventListener('pointercancel', release);

    stage.addEventListener('dblclick', reset);
    overlay.addEventListener('click', function (e) {
      var b = e.target.closest('[data-dz]');
      if (b) {
        var k = b.getAttribute('data-dz');
        if (k === 'in') zoomAt(0, 0, 1.4);
        else if (k === 'out') zoomAt(0, 0, 1 / 1.4);
        else if (k === 'reset') reset();
        else if (k === 'close') close();
      } else if (e.target === overlay || e.target === stage) {
        close();
      }
    });
    document.addEventListener('keydown', function (e) {
      if (!overlay.classList.contains('dz-open')) return;
      if (e.key === 'Escape') close();
      else if (e.key === '+' || e.key === '=') zoomAt(0, 0, 1.4);
      else if (e.key === '-') zoomAt(0, 0, 1 / 1.4);
      else if (e.key === '0') reset();
    });
  }

  function init() {
    document.querySelectorAll('.md-typeset img, .ava-landing img').forEach(function (el) {
      var src = el.getAttribute('src') || '';
      if (!/\.svg$/i.test(src) || /^https?:/i.test(src)) return; // local diagrams only
      el.classList.add('dz-zoomable');
      var link = el.closest('a');
      (link || el).addEventListener('click', function (e) {
        e.preventDefault();
        open(el.currentSrc || el.src, el.alt);
      });
    });
  }

  if (document.readyState !== 'loading') init();
  else document.addEventListener('DOMContentLoaded', init);
})();
