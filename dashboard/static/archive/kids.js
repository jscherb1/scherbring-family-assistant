// Kid chore page: celebration sound and confetti. No dependencies; everything degrades silently.
(function () {
  "use strict";
  var muted = false;
  try { muted = localStorage.getItem("kids-muted") === "1"; } catch (e) {}

  var toggle = document.getElementById("sound-toggle");
  function paintToggle() { if (toggle) toggle.textContent = muted ? "🔇" : "🔊"; }
  paintToggle();
  if (toggle) toggle.addEventListener("click", function () {
    muted = !muted;
    try { localStorage.setItem("kids-muted", muted ? "1" : "0"); } catch (e) {}
    paintToggle();
  });

  // ---- chime (Web Audio, no files) ----
  var audio;
  function tone(freq, start, dur) {
    if (muted) return;
    try {
      audio = audio || new (window.AudioContext || window.webkitAudioContext)();
      var o = audio.createOscillator(), g = audio.createGain(), t = audio.currentTime + start;
      o.type = "triangle"; o.frequency.value = freq;
      g.gain.setValueAtTime(0.0001, t);
      g.gain.exponentialRampToValueAtTime(0.25, t + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, t + dur);
      o.connect(g); g.connect(audio.destination); o.start(t); o.stop(t + dur + 0.05);
    } catch (e) {}
  }
  function chime(big) {
    [523, 659, 784].forEach(function (f, i) { tone(f, i * 0.09, 0.25); });
    if (big) [1047, 1319].forEach(function (f, i) { tone(f, 0.3 + i * 0.12, 0.35); });
  }

  // ---- confetti ----
  var canvas = document.getElementById("confetti"), ctx = canvas && canvas.getContext && canvas.getContext("2d");
  var bits = [], running = false;
  var COLORS = ["#ff6b6b", "#ffd43b", "#51cf66", "#4dabf7", "#b197fc", "#ff922b"];
  function size() { if (canvas) { canvas.width = window.innerWidth; canvas.height = window.innerHeight; } }
  window.addEventListener("resize", size); size();
  function burst(n, originY) {
    if (!ctx) return;
    for (var i = 0; i < n; i++) {
      bits.push({ x: window.innerWidth * (0.2 + Math.random() * 0.6), y: window.innerHeight * originY,
        vx: (Math.random() - 0.5) * 9, vy: -6 - Math.random() * 8, r: 4 + Math.random() * 6,
        c: COLORS[i % COLORS.length], rot: Math.random() * 6, life: 120 + Math.random() * 60 });
    }
    if (!running) { running = true; requestAnimationFrame(tick); }
  }
  function tick() {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    bits = bits.filter(function (b) { return b.life > 0 && b.y < canvas.height + 20; });
    bits.forEach(function (b) {
      b.vy += 0.35; b.x += b.vx; b.y += b.vy; b.rot += 0.2; b.life--;
      ctx.save(); ctx.translate(b.x, b.y); ctx.rotate(b.rot); ctx.fillStyle = b.c;
      ctx.fillRect(-b.r / 2, -b.r / 2, b.r, b.r * 1.6); ctx.restore();
    });
    if (bits.length) requestAnimationFrame(tick); else { running = false; ctx.clearRect(0, 0, canvas.width, canvas.height); }
  }

  // ---- toast ----
  var toastEl = document.getElementById("toast"), toastTimer;
  function toast(lines) {
    if (!toastEl || !lines.length) return;
    toastEl.innerHTML = "";
    lines.forEach(function (l) { var d = document.createElement("div"); d.textContent = l; toastEl.appendChild(d); });
    toastEl.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { toastEl.classList.remove("show"); }, 3200);
  }

  // Server tells us what happened via the HX-Trigger header (see routers/kids.py).
  document.body.addEventListener("chore-celebrate", function (e) {
    var d = e.detail || {}, lines = [];
    if (d.earned) lines.push("+" + d.earned + "! 🎉");
    else lines.push("Great job! 🎉");
    (d.badges || []).forEach(function (b) { lines.push(b.icon + " New badge: " + b.label); });
    var big = !!(d.all_done || (d.badges && d.badges.length));
    toast(lines); chime(big); burst(big ? 160 : 60, 0.7);
  });

  // Keep a wall-mounted tablet fresh (new day, edits made by the grown-ups).
  setInterval(function () { if (!document.hidden && !document.querySelector(".chore-main:active")) location.reload(); }, 5 * 60 * 1000);
})();
