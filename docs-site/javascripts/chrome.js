// Make the "Ava" title in the header act as the home link (the logo icon is
// hidden via CSS). Reads the home URL from the hidden logo anchor so it stays
// correct on every page depth.
(function () {
  function boot() {
    var logo = document.querySelector('.md-header a.md-logo');
    var home = logo ? logo.getAttribute('href') : '.';
    var name = document.querySelector('.md-header__title .md-header__topic .md-ellipsis');
    if (!name || name.dataset.homeWired) return;
    name.dataset.homeWired = '1';
    name.setAttribute('role', 'link');
    name.setAttribute('tabindex', '0');
    name.addEventListener('click', function () { window.location.href = home; });
    name.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); window.location.href = home; }
    });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
