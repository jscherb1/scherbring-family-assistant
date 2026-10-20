// Wellness charts: hover tooltips, a crosshair shared by every date chart on the page, and tap-to-pin
// readouts on touch screens. Every chart works without this file (each mark has a <title>); this only
// reads the data-tip JSON that dashboard/charts.py puts on days, points and cells. No dependencies.
(function () {
  "use strict";
  var tip = document.createElement("div");
  tip.className = "wtip";
  tip.setAttribute("role", "tooltip");
  document.body.appendChild(tip);
  var current = null;

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }

  function render(data) {
    var html = "<b>" + esc(data.d) + "</b>";
    if (data.rows && data.rows.length) {
      html += "<table>" + data.rows.map(function (r) {
        return "<tr><td>" + (r[2] ? '<i class="sw ' + esc(r[2]) + '"></i> ' : "") + esc(r[0]) + "</td><td>" + esc(r[1]) + "</td></tr>";
      }).join("") + "</table>";
    }
    if (data.note) html += '<div class="note">' + esc(data.note) + "</div>";
    return html;
  }

  function parse(el) {
    try { return JSON.parse(el.getAttribute("data-tip")); } catch (e) { return null; }
  }

  // Crosshair: every chart with the same data-sync group highlights the same day index.
  function crosshair(el, on) {
    var svg = el.closest("svg[data-sync]");
    if (!svg) return;
    var group = svg.getAttribute("data-sync"), i = el.getAttribute("data-i");
    document.querySelectorAll('svg[data-sync="' + group + '"]').forEach(function (s) {
      var line = s.querySelector(".xhair");
      var hit = on && s.querySelector('.hit[data-i="' + i + '"]');
      if (!line) return;
      if (hit) {
        line.setAttribute("x1", hit.getAttribute("data-x"));
        line.setAttribute("x2", hit.getAttribute("data-x"));
        line.classList.add("on");
      } else {
        line.classList.remove("on");
      }
    });
  }

  function place(x, y) {
    var pad = 14, r = tip.getBoundingClientRect();
    var left = x + pad, top = y - r.height - pad;
    if (left + r.width > window.innerWidth - 8) left = x - r.width - pad;
    if (top < 8) top = y + pad;
    tip.style.left = Math.max(8, left) + "px";
    tip.style.top = top + "px";
  }

  function show(el, x, y) {
    var data = parse(el);
    if (!data) return;
    if (current && current !== el) hide();
    current = el;
    el.classList.add("on");
    tip.innerHTML = render(data);
    tip.classList.add("on");
    place(x, y);
    crosshair(el, true);
  }

  function hide() {
    if (current) {
      current.classList.remove("on");
      crosshair(current, false);
    }
    current = null;
    tip.classList.remove("on");
  }

  // Mouse/pen: follow the pointer.
  document.addEventListener("pointermove", function (e) {
    if (e.pointerType === "touch") return;
    var el = e.target.closest && e.target.closest("[data-tip]");
    if (!el || !el.closest(".wellness")) { if (current) hide(); return; }
    show(el, e.clientX, e.clientY);
  });
  document.addEventListener("pointerleave", hide);
  window.addEventListener("scroll", function () { if (current && !current.dataset.pinned) hide(); }, { passive: true });

  // Touch: tap a day to pin its numbers in the chart's readout bar (tooltips are hidden on small screens).
  document.addEventListener("click", function (e) {
    var el = e.target.closest && e.target.closest("[data-tip]");
    var fig = el && el.closest(".wfig, .card");
    document.querySelectorAll(".wreadout").forEach(function (r) { if (!fig || !fig.contains(r)) r.innerHTML = ""; });
    if (!el || !el.closest(".wellness")) { hide(); return; }
    var data = parse(el);
    var out = fig && fig.querySelector(".wreadout");
    if (!data) return;
    if (out && window.matchMedia("(max-width: 760px), (hover: none)").matches) {
      out.innerHTML = render(data);
      if (current) { current.classList.remove("on"); crosshair(current, false); }
      current = el;
      el.classList.add("on");
      crosshair(el, true);
    } else {
      show(el, e.clientX, e.clientY);
    }
  });

  // Check-in: "Mark the rest no" fills every yes/no question still blank.
  document.addEventListener("click", function (e) {
    var b = e.target.closest && e.target.closest("[data-rest]");
    if (!b) return;
    b.closest("form").querySelectorAll(".wck-qs li").forEach(function (li) {
      if (li.querySelector("input:checked")) return;
      var pick = li.querySelector('input[value="' + b.getAttribute("data-rest") + '"]');
      if (pick) pick.checked = true;
    });
  });

  document.addEventListener("keydown", function (e) { if (e.key === "Escape") hide(); });
  document.addEventListener("htmx:beforeSwap", hide);
})();
