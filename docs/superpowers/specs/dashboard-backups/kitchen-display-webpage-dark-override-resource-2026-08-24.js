(function () {
  function isTarget() {
    return location.pathname.indexOf('/kitchen-display-webpage') !== -1;
  }
  var STYLE_ID = 'kitchen-display-dark-override';
  var vars = {
    '--primary-background-color': '#111111',
    '--card-background-color': '#1c1c1c',
    '--secondary-background-color': '#202020',
    '--primary-text-color': '#e1e1e1',
    '--secondary-text-color': '#9b9b9b',
    '--disabled-text-color': '#6f6f6f',
    '--divider-color': 'rgba(225, 225, 225, 0.12)',
    '--ha-card-background': '#1c1c1c',
    '--ha-card-border-color': 'rgba(255, 255, 255, 0.15)',
    '--ha-card-border-width': '1px',
    '--app-header-background-color': '#111111',
    '--app-header-text-color': '#e1e1e1',
    '--mush-title-color': '#e1e1e1',
    '--mush-subtitle-color': '#9b9b9b',
    '--mush-text-color': '#e1e1e1',
    '--mush-icon-color': '#e1e1e1',
    '--mush-card-background-color': '#1c1c1c'
  };
  function apply() {
    if (!isTarget()) return;
    var root = document.documentElement;
    Object.keys(vars).forEach(function (k) {
      root.style.setProperty(k, vars[k]);
    });
    if (!document.getElementById(STYLE_ID)) {
      var style = document.createElement('style');
      style.id = STYLE_ID;
      style.textContent = 'html, body { background-color: #111111 !important; }';
      document.head.appendChild(style);
    }
  }
  apply();
  document.addEventListener('DOMContentLoaded', apply);
  window.addEventListener('location-changed', apply);
  window.addEventListener('popstate', apply);
  setInterval(apply, 3000);
})();
