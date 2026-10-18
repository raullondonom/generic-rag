"use strict";
// Administration panel: provider, spend, index, source control, ratings and documents with problems.
// Access: ADMIN_TOKEN in the X-Admin-Token header (kept in this browser), or this machine when no token is set.

let token = store.get("rag.adminToken", "");
let status = null;

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (token) headers["X-Admin-Token"] = token;
  if (options.body) headers["Content-Type"] = "application/json";
  const resp = await fetch(path, { ...options, headers });
  if (resp.status === 401 || resp.status === 403) {
    showGate(resp.status);
    throw new Error("unauthorized");
  }
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.detail || resp.statusText);
  return data;
}

// ------------------------------------------------------------ access

function showGate(code) {
  $("#panel").hidden = true;
  $("#gate").hidden = false;
  $("#logout").hidden = true;
  $("#refresh").hidden = true;
  const forbidden = code === 403;
  $("#gate-form").hidden = forbidden;
  $("#gate-text").textContent = tr(forbidden ? "forbidden" : "tokenText");
  $("#gate-error").textContent = code === 401 && token ? tr("tokenWrong") : "";
  if (!forbidden) $("#token").focus();
}

$("#gate-form").addEventListener("submit", (e) => {
  e.preventDefault();
  token = $("#token").value.trim();
  store.set("rag.adminToken", token);
  start();
});

$("#logout").addEventListener("click", () => {
  token = "";
  store.set("rag.adminToken", "");
  $("#token").value = "";
  showGate(401);
});

// ------------------------------------------------------------ provider and spend

function renderProvider(st) {
  const sel = $("#provider");
  sel.replaceChildren();
  if (!st.providers.length) {
    sel.innerHTML = `<option value="">${escapeHtml(tr("noProviders"))}</option>`;
    sel.disabled = true;
  }
  for (const p of st.providers) {
    const opt = document.createElement("option");
    opt.value = p;
    opt.textContent = tr(`p_${p}`) + (p === st.configured_provider ? ` · ${tr("providerDefault", { name: "LLM_PROVIDER" })}` : "");
    opt.selected = p === st.default_provider;
    sel.appendChild(opt);
  }
  const model = st.models[st.default_provider];
  $("#model").textContent = model ? tr("model", { model }) : "";
  renderSpend(st);
}

$("#provider").addEventListener("change", async (e) => {
  const value = e.target.value;
  try {
    // Back to LLM_PROVIDER when it is picked again (no override stored)
    const provider = value === status.configured_provider ? null : value;
    const data = await api("/api/admin/provider", { method: "PUT", body: JSON.stringify({ provider }) });
    status.default_provider = data.default_provider;
    $("#provider-msg").textContent = tr("providerSaved");
    setTimeout(() => { $("#provider-msg").textContent = ""; }, 1500);
    renderProvider(status);
  } catch (err) {
    if (err.message !== "unauthorized") $("#provider-msg").textContent = err.message;
  }
});

function renderSpend(st) {
  const el = $("#spend");
  if (!st.spend) { el.innerHTML = ""; return; }
  const s = st.spend;
  const pct = s.cap_usd ? Math.min(100, (s.month_usd / s.cap_usd) * 100) : 0;
  const cache = s.cache_hit_ratio != null ? ` · ${tr("cache", { pct: Math.round(s.cache_hit_ratio * 100) })}` : "";
  el.innerHTML = `<h3>${escapeHtml(tr("spend"))}</h3>` +
    (st.dry_run ? `<p class="muted small">${escapeHtml(tr("dryRun"))}</p>` : "") +
    `<p class="small">${escapeHtml(tr("spendMonth", { spent: s.month_usd.toFixed(2), cap: s.cap_usd.toFixed(2), calls: s.calls ?? "—" }))}${escapeHtml(cache)}</p>` +
    `<div class="meter${pct > 80 ? " high" : ""}" role="img" aria-label="${escapeHtml(tr("budgetUsed", { pct: Math.round(pct) }))}"><span style="width:${pct}%"></span></div>`;
}

// ------------------------------------------------------------ index

function renderIndex(st) {
  const parts = [`<p class="muted small">${escapeHtml(tr("chunks", { n: st.chunks_in_index }))}</p>`];
  const confluenceOn = st.confluence && st.confluence.configured;
  document.querySelector('button[data-source="confluence"]').hidden = !confluenceOn;
  for (const key of confluenceOn ? ["folder", "confluence"] : ["folder"]) {
    const s = st.sources[key];
    const run = s && s.last_sync;
    const busy = (key === "folder" ? st.folder.running : st.confluence.running) || (run && run.status === "running");
    const cls = busy || !run || run.status === "waiting" ? "running" : run.status === "ok" ? "ok" : "error";
    const docs = s && Object.keys(s.docs).length
      ? Object.entries(s.docs).map(([k, v]) => `${v} ${tr(`st_${k}`)}`).join(" · ") : tr("noDocs");
    const extra = key === "folder"
      ? (st.folder.auto ? "" : `<div class="muted">${escapeHtml(tr("autoOff"))}</div>`)
      : `<div class="muted">${escapeHtml(st.confluence.spaces.join(", "))} · ` +
        escapeHtml(st.confluence.sync_time ? tr("daily", { time: st.confluence.sync_time }) : tr("autoOff")) + "</div>";
    const redacted = s && s.redactions ? `<div class="muted">${escapeHtml(tr("redacted", { n: s.redactions }))}</div>` : "";
    const when = busy ? tr("syncing")
      : !run ? tr("pending")
      : run.status === "waiting" ? tr("waiting")
      : run.finished_at ? tr("last", { date: fmtDateTime(run.finished_at) }) : tr("pending");
    const message = run && run.message && !busy ? `<div class="muted">${escapeHtml(run.message)}</div>` : "";
    parts.push(`<div class="source-card"><b><span class="dot ${cls}"></span>${escapeHtml(tr(key))}</b>${escapeHtml(docs)}${extra}${redacted}` +
      `<div class="muted">${escapeHtml(when)}</div>${message}</div>`);
  }
  $("#status").innerHTML = parts.join("");
}

document.querySelectorAll("button[data-source]").forEach((btn) =>
  btn.addEventListener("click", async () => {
    const full = btn.dataset.full === "1";
    if (full && !confirm(tr("fullConfirm"))) return;
    try {
      const data = await api("/api/reindex", { method: "POST", body: JSON.stringify({ source: btn.dataset.source, full }) });
      $("#reindex-msg").textContent = tr("started", { list: data.started.join(", ") });
    } catch (err) {
      if (err.message !== "unauthorized") $("#reindex-msg").textContent = err.message;
    }
    setTimeout(loadStatus, 1500);
  })
);

// ------------------------------------------------------------ source control review (no AI, no reindex)

const CONTROL_SECTIONS = ["expired", "expiring", "changed", "review_due", "missing", "unreachable", "no_document", "unregistered"];
const CONTROL_ALERTS = new Set(["expired", "missing", "changed"]);

async function loadControl() {
  const box = $("#control");
  const c = await api("/api/control?findings=100");
  $("#control-check").hidden = !c.configured;
  if (!c.configured) { box.innerHTML = `<p class="muted small">${escapeHtml(tr("ctlNone"))}</p>`; return; }
  const run = c.last_run;
  const counts = (run && run.counts) || {};
  const alert = CONTROL_SECTIONS.some((s) => CONTROL_ALERTS.has(s) && counts[s]);
  const cls = c.running || (run && run.status === "running") ? "running" : !run || run.status === "error" || alert ? "error" : "ok";
  const summary = CONTROL_SECTIONS.filter((s) => counts[s])
    .map((s) => `${counts[s]} ${tr(`sec_${s}`)}`).join(" · ") || (run ? tr("ctlNothing") : "");
  const when = c.running ? tr("ctlRunning")
    : !run ? tr("ctlNever")
    : run.status === "error" ? tr("ctlError", { error: run.error || "" })
    : tr("ctlLast", { date: fmtDateTime(run.finished_at || run.started_at) }) + (run.online ? "" : tr("ctlOffline"));
  const schedule = c.check_time ? tr("daily", { time: c.check_time }) : tr("autoOff");
  const items = (run && run.findings || []).map((f) =>
    `<li><b>${escapeHtml(tr(`sec_${f.section}`))}</b> ${escapeHtml(f.file || f.jurisdiction || "")}` +
    `${f.date ? ` (${escapeHtml(f.date)})` : ""}${f.hint ? `<div class="muted">${escapeHtml(f.hint)}</div>` : ""}</li>`).join("");
  const more = run && run.total_findings > (run.findings || []).length
    ? `<p class="muted">${escapeHtml(tr("ctlMore", { n: run.total_findings - run.findings.length }))}</p>` : "";
  box.innerHTML = `<div class="source-card"><b><span class="dot ${cls}"></span>${escapeHtml(tr("ctlRecords", { records: c.records, n: c.registries.length }))}</b>` +
    `<div class="muted">${escapeHtml(when)} · ${escapeHtml(schedule)}</div>` +
    (summary ? `<div>${escapeHtml(summary)}</div>` : "") +
    (items ? `<details class="findings"><summary>${escapeHtml(tr("ctlDetails"))}</summary><ul>${items}</ul>${more}</details>` : "") +
    "</div>";
}

$("#control-check").addEventListener("click", async () => {
  try {
    const data = await api("/api/control/check", { method: "POST", body: JSON.stringify({ online: true }) });
    $("#control-msg").textContent = data.started ? tr("ctlStarted") : tr("ctlRunning");
  } catch (err) {
    if (err.message !== "unauthorized") $("#control-msg").textContent = err.message;
  }
  setTimeout(loadControl, 1500);
});

// ------------------------------------------------------------ ratings

async function loadRatings() {
  const r = await api("/api/feedback/summary?days=30");
  const box = $("#ratings");
  if (!r.up && !r.down) { box.innerHTML = `<p class="muted small">${escapeHtml(tr("noRatings"))}</p>`; return; }
  const pct = Math.round((r.satisfaction || 0) * 100);
  const models = r.by_model.map((m) =>
    `<li>${escapeHtml(tr(`p_${m.provider}`) === `p_${m.provider}` ? m.provider : tr(`p_${m.provider}`))}` +
    `${m.model ? ` · <span class="muted">${escapeHtml(m.model)}</span>` : ""}: 👍 ${m.up} · 👎 ${m.down}</li>`).join("");
  const reasons = Object.entries(r.by_reason).map(([k, n]) =>
    `<li>${escapeHtml(k === "unspecified" ? tr("unspecified") : tr(`reason_${k}`))}: ${n}</li>`).join("");
  box.innerHTML = `<p>${escapeHtml(tr("satisfaction", { pct, up: r.up, down: r.down }))}</p>` +
    `<div class="meter" role="img" aria-label="${pct}%"><span style="width:${pct}%"></span></div>` +
    (models ? `<h3>${escapeHtml(tr("byModel"))}</h3><ul class="plain">${models}</ul>` : "") +
    (reasons ? `<h3>${escapeHtml(tr("byReason"))}</h3><ul class="plain">${reasons}</ul>` : "");
}

// ------------------------------------------------------------ documents with problems

async function loadProblems() {
  const [errors, empty] = await Promise.all([
    api("/api/documents?status=error&limit=200"),
    api("/api/documents?status=empty&limit=200"),
  ]);
  const docs = [...errors, ...empty];
  const box = $("#problems");
  if (!docs.length) { box.innerHTML = `<p class="muted small">${escapeHtml(tr("noProblems"))}</p>`; return; }
  box.innerHTML = `<ul class="plain doc-list">` + docs.map((d) =>
    `<li><span class="dot ${d.status === "error" ? "error" : "running"}"></span><b>${escapeHtml(d.title)}</b> ` +
    `<span class="muted">${escapeHtml(tr(`st_${d.status}`))}</span>` +
    `<div class="muted small">${escapeHtml(d.location)}</div>` +
    (d.error ? `<div class="error small">${escapeHtml(d.error)}</div>` : "") + "</li>").join("") + "</ul>";
}

// ------------------------------------------------------------ loading

async function loadStatus() {
  status = await api("/api/admin/status");
  renderProvider(status);
  renderIndex(status);
}

async function loadBranding() {
  try {
    const c = await (await fetch("/api/config")).json();
    applyLanguage(c.language);
    applyColors(c.colors);
    $("#brand-name").textContent = c.name;
    document.querySelector(".brand-icon").src = c.icon_url;
    $("#favicon").href = c.icon_url;
    document.title = `${tr("adminTitle")} · ${c.name}`;
  } catch {
    applyLanguage(lang);
  }
}

async function refresh() {
  const results = await Promise.allSettled([loadStatus(), loadControl(), loadRatings(), loadProblems()]);
  const failed = results.find((r) => r.status === "rejected" && r.reason.message !== "unauthorized");
  if (failed) $("#reindex-msg").textContent = `${tr("statusFailed")}: ${failed.reason.message}`;
}

async function start() {
  try {
    const check = await api("/api/admin/check");
    $("#gate").hidden = true;
    $("#panel").hidden = false;
    $("#refresh").hidden = false;
    $("#logout").hidden = !check.token_required;
    await refresh();
  } catch (err) {
    if (err.message !== "unauthorized") $("#gate-error").textContent = err.message;
  }
}

$("#refresh").addEventListener("click", refresh);
setInterval(() => { if (!document.hidden && !$("#panel").hidden) loadStatus().catch(() => {}); }, 15000);

applyLanguage(lang);
loadBranding().then(start);
