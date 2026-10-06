const REFRESH_MS = 30000;

function formatDuration(seconds) {
  if (seconds == null) return "unknown";
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (days > 0) return `${days}d ${hours}h`;
  if (hours > 0) return `${hours}h ${minutes}m`;
  return `${minutes}m`;
}

function formatTs(iso) {
  if (!iso) return "never";
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  return d.toLocaleString();
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function renderUptime(uptime) {
  const el = document.querySelector("#card-uptime .card-body");
  if (uptime.error) {
    el.innerHTML = `<div class="error-state">${esc(uptime.error)}</div>`;
    return;
  }
  el.innerHTML = `
    <div class="metric">${formatDuration(uptime.uptime_seconds)}</div>
    <div class="metric-sub">since last restart (${esc(formatTs(uptime.last_restart_at))})</div>
    <div class="metric-sub">${uptime.restarts_24h} restarts / 24h &middot; ${uptime.restarts_7d} / 7d</div>
  `;
}

function renderSuccess(taskRuns) {
  const el = document.querySelector("#card-success .card-body");
  if (taskRuns.error) {
    el.innerHTML = `<div class="error-state">${esc(taskRuns.error)}</div>`;
    return;
  }
  const rate = taskRuns.success_rate_24h;
  const rateText = rate == null ? "n/a" : `${rate.toFixed(0)}%`;
  el.innerHTML = `
    <div class="metric">${rateText}</div>
    <div class="metric-sub">${taskRuns.ok_24h} ok / ${taskRuns.fail_24h} failed (24h)</div>
    <div class="metric-sub">${taskRuns.ok_7d} ok / ${taskRuns.fail_7d} failed (7d)</div>
  `;
}

function renderWatchdogs(watchdogs) {
  const el = document.querySelector("#card-watchdogs .card-body");
  const rows = Object.entries(watchdogs).map(([name, info]) => {
    if (info.error) {
      return `<div class="status-row"><span>${esc(name)}</span><span class="error-state">${esc(info.error)}</span></div>`;
    }
    const pillClass = info.healthy ? "pill-ok" : "pill-warn";
    const pillText = info.healthy ? "healthy" : "unhealthy";
    return `
      <div class="status-row">
        <span>${esc(name)}</span>
        <span class="pill ${pillClass}">${pillText}</span>
      </div>
    `;
  });
  el.innerHTML = rows.join("");
}

function renderFallback(fallback) {
  const el = document.querySelector("#card-fallback .card-body");
  if (!fallback.last_used_at) {
    el.innerHTML = `<div class="empty-state">Never used</div>`;
    return;
  }
  el.innerHTML = `
    <div class="metric-sub">Last used: ${esc(formatTs(fallback.last_used_at))}</div>
    <div class="metric-sub">Reason: ${esc(fallback.reason || "unknown")}</div>
  `;
}

function renderFailures(taskRuns) {
  const el = document.querySelector("#card-failures .card-body");
  if (taskRuns.error) {
    el.innerHTML = `<div class="error-state">${esc(taskRuns.error)}</div>`;
    return;
  }
  const failures = taskRuns.recent_failures || [];
  if (failures.length === 0) {
    el.innerHTML = `<div class="empty-state">No recent failures</div>`;
    return;
  }
  const items = failures.map((f) => `
    <li>
      <div class="failure-task">${esc(f.task_name)} <span class="pill pill-warn">${esc(f.status)}</span></div>
      <div class="failure-meta">${esc(formatTs(f.run_at))}${f.summary ? " — " + esc(f.summary) : ""}</div>
    </li>
  `).join("");
  el.innerHTML = `<ul class="failure-list">${items}</ul>`;
}

async function refresh() {
  try {
    const res = await fetch("/api/status");
    const data = await res.json();
    document.getElementById("generated-at").textContent =
      `Updated ${formatTs(data.generated_at)}`;
    renderUptime(data.uptime);
    renderSuccess(data.task_runs);
    renderWatchdogs(data.watchdogs);
    renderFallback(data.telegram_fallback);
    renderFailures(data.task_runs);
  } catch (err) {
    document.getElementById("generated-at").textContent = "Failed to load status";
    console.error(err);
  }
}

refresh();
setInterval(refresh, REFRESH_MS);
