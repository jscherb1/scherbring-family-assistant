// Shared page behaviour, loaded on every page by base.html.
//  1. Long tables: <table data-limit="15"> shows the first N rows plus a "Show all" button; <table data-filter="...">
//     adds a filter box (the value is its placeholder). Expanded state survives htmx refreshes (keyed by data-key or
//     the table's position), and a "#q=text" in the URL pre-fills the first filter on the page.
//  2. Jump-to search: press "/" (or tap the search button) to fuzzy-find any page, recipe, habit or person.
(function () {
  "use strict";
  var expanded = {};

  function tableKey(t, i) { return t.getAttribute("data-key") || location.pathname + "#" + i; }

  function apply(t) {
    var rows = Array.prototype.slice.call(t.tBodies[0] ? t.tBodies[0].rows : []);
    var limit = parseInt(t.getAttribute("data-limit") || "0", 10);
    var q = (t._filter && t._filter.value || "").trim().toLowerCase();
    var open = expanded[t._key];
    var shown = 0, matched = 0;
    rows.forEach(function (r) {
      var hit = !q || r.textContent.toLowerCase().indexOf(q) !== -1;
      if (hit) matched++;
      var visible = hit && (q || open || !limit || matched <= limit);
      r.hidden = !visible;
      if (visible) shown++;
    });
    if (t._more) {
      var extra = rows.length - limit;
      t._more.hidden = !limit || q || extra <= 0;
      t._more.textContent = open ? "Show fewer" : "Show all " + rows.length;
    }
    if (t._count) t._count.textContent = q ? matched + " of " + rows.length : "";
  }

  function enhanceTables(root) {
    var tables = (root || document).querySelectorAll("table[data-limit], table[data-filter]");
    Array.prototype.forEach.call(tables, function (t, i) {
      if (t._enhanced) return;
      t._enhanced = true;
      t._key = tableKey(t, i);
      var host = t.closest(".card") || t;
      if (t.hasAttribute("data-filter")) {
        var bar = document.createElement("div");
        bar.className = "tbl-filter";
        bar.innerHTML = '<input type="search" autocomplete="off"><span class="muted"></span>';
        t._filter = bar.firstChild;
        t._count = bar.lastChild;
        t._filter.placeholder = t.getAttribute("data-filter") || "Filter";
        t._filter.setAttribute("aria-label", t._filter.placeholder);
        t._filter.addEventListener("input", function () { apply(t); });
        host.parentNode.insertBefore(bar, host);
        var m = /[#&]q=([^&]+)/.exec(location.hash);
        if (m && !document._prefilled) { t._filter.value = decodeURIComponent(m[1]); document._prefilled = true; }
      }
      if (t.hasAttribute("data-limit")) {
        t._more = document.createElement("button");
        t._more.type = "button";
        t._more.className = "tbl-more";
        t._more.addEventListener("click", function () { expanded[t._key] = !expanded[t._key]; apply(t); });
        host.parentNode.insertBefore(t._more, host.nextSibling);
      }
      apply(t);
    });
  }

  // ---- jump-to search ----
  var index = null, dlg = null, input = null, list = null, sel = 0, hits = [];

  function inOrder(hay, t) {
    for (var i = 0, j = 0; i < hay.length && j < t.length; i++) if (hay.charAt(i) === t.charAt(j)) j++;
    return t.length >= 3 && j === t.length;
  }

  function score(item, terms) {
    var hay = (item.label + " " + (item.group || "") + " " + (item.keywords || "")).toLowerCase();
    var s = 0;
    for (var i = 0; i < terms.length; i++) {
      var t = terms[i], at = hay.indexOf(t);
      if (at === -1 && t.length > 3) at = hay.indexOf(t.replace(/(es|s)$/, ""));  // "tacos" finds "Taco Night"
      if (at === -1) {
        if (!inOrder(item.label.toLowerCase(), t)) return -1;  // last resort: letters in order in the name ("chkn" finds "Chicken")
        s += 0.2;
        continue;
      }
      s += at === 0 ? 3 : (hay.charAt(at - 1) === " " ? 2 : 1);
    }
    return s + (item.kind === "page" ? 1 : 0);
  }

  function render() {
    var terms = input.value.trim().toLowerCase().split(/\s+/).filter(Boolean);
    hits = (index || []).map(function (it) { return { it: it, s: terms.length ? score(it, terms) : (it.kind === "page" ? 1 : -1) }; })
      .filter(function (h) { return h.s >= 0; });
    var direct = hits.some(function (h) { return h.s >= 1; });
    hits = hits.filter(function (h) { return !direct || h.s >= 1; })  // letters-in-order only when nothing matches outright
      .sort(function (a, b) { return b.s - a.s; })
      .slice(0, 12)
      .map(function (h) { return h.it; });
    sel = 0;
    list.innerHTML = "";
    hits.forEach(function (it, i) {
      var li = document.createElement("li");
      li.setAttribute("role", "option");
      var a = document.createElement("a");
      a.href = it.href;
      a.innerHTML = "<span></span><em></em>";
      a.firstChild.textContent = it.label;
      a.lastChild.textContent = it.group || "";
      li.appendChild(a);
      if (i === 0) li.className = "sel";
      list.appendChild(li);
    });
    if (!hits.length) list.innerHTML = '<li class="none">No matches</li>';
  }

  function move(d) {
    var items = list.querySelectorAll("li[role=option]");
    if (!items.length) return;
    items[sel].className = "";
    sel = (sel + d + items.length) % items.length;
    items[sel].className = "sel";
    items[sel].scrollIntoView({ block: "nearest" });
  }

  function openSearch() {
    if (!dlg) {
      dlg = document.getElementById("jump");
      if (!dlg) return;
      input = dlg.querySelector("input");
      list = dlg.querySelector("ul");
      input.addEventListener("input", render);
      input.addEventListener("keydown", function (e) {
        if (e.key === "ArrowDown") { e.preventDefault(); move(1); }
        else if (e.key === "ArrowUp") { e.preventDefault(); move(-1); }
        else if (e.key === "Enter" && hits[sel]) { e.preventDefault(); location.href = hits[sel].href; }
      });
      dlg.addEventListener("click", function (e) { if (e.target === dlg) dlg.close(); });
    }
    input.value = "";
    if (dlg.showModal) dlg.showModal(); else dlg.setAttribute("open", "");
    input.focus();
    if (index) { render(); return; }
    list.innerHTML = '<li class="none">Loading…</li>';
    fetch("/api/nav-index").then(function (r) { return r.json(); }).then(function (d) { index = d.items || []; render(); })
      .catch(function () { list.innerHTML = '<li class="none">Search is unavailable right now.</li>'; });
  }

  document.addEventListener("keydown", function (e) {
    var tag = (e.target.tagName || "").toLowerCase();
    if (e.key === "/" && !e.metaKey && !e.ctrlKey && tag !== "input" && tag !== "textarea" && tag !== "select" && !e.target.isContentEditable) {
      e.preventDefault();
      openSearch();
    }
  });
  document.addEventListener("click", function (e) {
    if (e.target.closest && e.target.closest("[data-jump]")) { e.preventDefault(); openSearch(); }
  });

  enhanceTables();
  document.addEventListener("htmx:afterSwap", function (e) { enhanceTables(e.target); });

  // Home Assistant board: tabs (the last one is remembered; without JS every panel shows).
  (function () {
    var tabs = document.querySelector(".ha-tabs");
    if (!tabs) return;
    var main = tabs.parentNode, panels = main.querySelectorAll(".ha-panel"), KEY = "ha-board-tab";
    var names = Array.prototype.map.call(panels, function (p) { return p.getAttribute("data-panel"); });
    function show(name) {
      if (names.indexOf(name) < 0) name = names[0];
      Array.prototype.forEach.call(panels, function (p) { p.hidden = p.getAttribute("data-panel") !== name; });
      Array.prototype.forEach.call(tabs.querySelectorAll("button"), function (b) {
        b.setAttribute("aria-selected", b.getAttribute("data-tab") === name ? "true" : "false");
      });
      try { localStorage.setItem(KEY, name); } catch (e) {}
      if (location.hash.slice(1) !== name) history.replaceState(null, "", "#" + name);
    }
    var start = location.hash.slice(1);
    if (!start) { try { start = localStorage.getItem(KEY) || ""; } catch (e) {} }
    document.documentElement.classList.add("ha-js");
    tabs.addEventListener("click", function (e) {
      var b = e.target.closest("button[data-tab]");
      if (b) show(b.getAttribute("data-tab"));
    });
    show(start);
  })();
})();
