"use strict";

const $ = (sel) => document.querySelector(sel);
const messagesEl = $("#messages");
const form = $("#form");
const input = $("#question");
const sendBtn = $("#send");
const stopBtn = $("#stop");
const providerSel = $("#provider");
const drawer = $("#drawer");
const backdrop = $("#backdrop");
const menuBtn = $("#menu-btn");

const MAX_SAVED = 100;
const desktop = window.matchMedia("(min-width: 900px)");
const touch = window.matchMedia("(pointer: coarse)");

let currentId = null;   // open conversation (null = new)
let busy = false;
let controller = null;  // AbortController of the answer in progress
let models = {};
let lastStatus = null;

// ------------------------------------------------------------ local storage
// The browser only keeps the ids; the history lives on the server.

const store = {
  get(key, fallback) {
    try { const v = localStorage.getItem(key); return v === null ? fallback : JSON.parse(v); } catch { return fallback; }
  },
  set(key, value) {
    try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* private mode or blocked storage */ }
  },
};
const savedIds = () => store.get("rag.conversations", []);
function rememberId(id) {
  store.set("rag.conversations", [id, ...savedIds().filter((x) => x !== id)].slice(0, MAX_SAVED));
}
function forgetId(id) {
  store.set("rag.conversations", savedIds().filter((x) => x !== id));
}

// ------------------------------------------------------------ texts (LANGUAGE=en|es on the server)

const I18N = {
  en: {
    appName: "Document RAG", tagline: "Answers only from your documents.", newChat: "New conversation",
    conversations: "Conversations", provider: "AI provider", index: "Index", loading: "Loading…",
    reindexFolder: "Reindex folder", syncConfluence: "Sync Confluence", placeholder: "Type your question…",
    question: "Question", send: "Send", stop: "Stop answer", openMenu: "Open menu", closeMenu: "Close menu",
    panel: "Conversations and settings",
    emptyTitle: "What would you like to look up?",
    emptyText: "Answers are based only on the indexed documents and cite their sources.",
    noConversations: "No saved conversations", loadFailed: "Could not load them",
    deleteLabel: 'Delete "{title}"', deleteConfirm: 'Delete the conversation "{title}"? This cannot be undone.',
    incomplete: "Incomplete answer: {error}", stopped: "Answer stopped.", error: "Error: {msg}",
    sources: "Sources ({n})", similarity: "similarity", copy: "Copy", copied: "Copied", copyFailed: "Could not copy",
    model: "Model: {model}", noProviders: "No providers configured",
    dryRun: "Simulated mode (DRY_RUN): nothing is spent.", spendMonth: "This month: USD {spent} of {cap}",
    cache: "cache {pct}%", budgetUsed: "{pct}% of the cap used",
    chunks: "{n} chunks", noDocs: "no documents", redacted: "{n} sensitive values redacted", syncing: "Syncing…", last: "Last: {date}", pending: "Pending",
    statusFailed: "Could not get the status", started: "Started: {list}",
    folder: "Local folder", confluence: "Confluence", daily: "Every day at {time}", autoOff: "Automatic updates off: use the button",
    st_indexed: "indexed", st_unsupported: "unsupported", st_error: "with errors", st_empty: "no text",
    p_anthropic: "Claude (Anthropic)", p_ollama: "Ollama (local)",
    rateUp: "Useful answer", rateDown: "Not useful", reasonTitle: "What went wrong? (optional)",
    reason_wrong: "Incorrect", reason_incomplete: "Incomplete", reason_not_found: "Didn't find the info",
    reason_bad_sources: "Wrong sources", reason_other: "Other", commentPh: "Comment (optional)",
    sendFeedback: "Send", skip: "Skip", thanks: "Thanks for your feedback",
  },
  es: {
    appName: "RAG Documental", tagline: "Responde solo con tus documentos.", newChat: "Nueva conversación",
    conversations: "Conversaciones", provider: "Proveedor de IA", index: "Índice", loading: "Cargando…",
    reindexFolder: "Reindexar carpeta", syncConfluence: "Sincronizar Confluence", placeholder: "Escribe tu pregunta…",
    question: "Pregunta", send: "Enviar", stop: "Detener respuesta", openMenu: "Abrir menú", closeMenu: "Cerrar menú",
    panel: "Conversaciones y configuración",
    emptyTitle: "¿Qué quieres consultar?",
    emptyText: "Las respuestas se basan únicamente en los documentos indexados y citan sus fuentes.",
    noConversations: "Sin conversaciones guardadas", loadFailed: "No se pudieron cargar",
    deleteLabel: 'Borrar "{title}"', deleteConfirm: '¿Borrar la conversación "{title}"? No se puede deshacer.',
    incomplete: "Respuesta incompleta: {error}", stopped: "Respuesta detenida.", error: "Error: {msg}",
    sources: "Fuentes ({n})", similarity: "similitud", copy: "Copiar", copied: "Copiado", copyFailed: "No se pudo copiar",
    model: "Modelo: {model}", noProviders: "Sin proveedores configurados",
    dryRun: "Modo simulado (DRY_RUN): no se gasta.", spendMonth: "Este mes: USD {spent} de {cap}",
    cache: "caché {pct}%", budgetUsed: "{pct}% del tope usado",
    chunks: "{n} fragmentos", noDocs: "sin documentos", redacted: "{n} valores sensibles redactados", syncing: "Sincronizando…", last: "Última: {date}", pending: "Pendiente",
    statusFailed: "No se pudo obtener el estado", started: "Iniciado: {list}",
    folder: "Carpeta local", confluence: "Confluence", daily: "Todos los días a las {time}", autoOff: "Actualización automática apagada: usa el botón",
    st_indexed: "indexados", st_unsupported: "no soportados", st_error: "con error", st_empty: "sin texto",
    p_anthropic: "Claude (Anthropic)", p_ollama: "Ollama (local)",
    rateUp: "Respuesta útil", rateDown: "No me sirvió", reasonTitle: "¿Qué falló? (opcional)",
    reason_wrong: "Incorrecta", reason_incomplete: "Incompleta", reason_not_found: "No encontró la info",
    reason_bad_sources: "Fuentes equivocadas", reason_other: "Otro", commentPh: "Comentario (opcional)",
    sendFeedback: "Enviar", skip: "Omitir", thanks: "Gracias por tu opinión",
  },
};

let lang = store.get("rag.language", "en");

function tr(key, vars = {}) {
  const text = (I18N[lang] && I18N[lang][key]) || I18N.en[key] || key;
  return text.replace(/\{(\w+)\}/g, (_, k) => (k in vars ? vars[k] : `{${k}}`));
}

function applyLanguage(next) {
  lang = I18N[next] ? next : "en";
  store.set("rag.language", lang);
  document.documentElement.lang = lang;
  document.querySelectorAll("[data-i18n]").forEach((el) => { el.textContent = tr(el.dataset.i18n); });
  document.querySelectorAll("[data-i18n-label]").forEach((el) => el.setAttribute("aria-label", tr(el.dataset.i18nLabel)));
  document.querySelectorAll("[data-i18n-placeholder]").forEach((el) => { el.placeholder = tr(el.dataset.i18nPlaceholder); });
  const empty = messagesEl.querySelector(".empty");
  if (empty) showEmpty();
  if (!currentId) setTitle(null);
}

// ------------------------------------------------------------ minimal, safe Markdown

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function inline(s) {
  return escapeHtml(s)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>")
    .replace(/(^|[^_\w])_([^_\s][^_]*)_(?!\w)/g, "$1<em>$2</em>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
    .replace(/\[(\d+(?:\s*,\s*\d+)*)\]/g, '<span class="cite">[$1]</span>');
}

function renderMarkdown(md) {
  const lines = md.replace(/\r/g, "").split("\n");
  const out = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (line.startsWith("```")) {
      const code = [];
      i++;
      while (i < lines.length && !lines[i].startsWith("```")) code.push(lines[i++]);
      i++;
      out.push(`<pre><code>${escapeHtml(code.join("\n"))}</code></pre>`);
      continue;
    }
    const h = line.match(/^(#{1,6})\s+(.*)$/);
    if (h) { out.push(`<h${h[1].length}>${inline(h[2])}</h${h[1].length}>`); i++; continue; }
    if (/^\s*\|.*\|\s*$/.test(line)) {
      const rows = [];
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) rows.push(lines[i++]);
      const cells = (r) => r.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
      const body = rows.filter((r) => !/^\s*\|?\s*:?-{2,}/.test(r));
      const [head, ...rest] = body;
      out.push("<table><thead><tr>" + cells(head).map((c) => `<th>${inline(c)}</th>`).join("") + "</tr></thead><tbody>" +
        rest.map((r) => "<tr>" + cells(r).map((c) => `<td>${inline(c)}</td>`).join("") + "</tr>").join("") + "</tbody></table>");
      continue;
    }
    if (/^\s*([-*]|\d+\.)\s+/.test(line)) {
      const ordered = /^\s*\d+\./.test(line);
      const items = [];
      while (i < lines.length && /^\s*([-*]|\d+\.)\s+/.test(lines[i])) {
        items.push(`<li>${inline(lines[i].replace(/^\s*([-*]|\d+\.)\s+/, ""))}</li>`);
        i++;
      }
      out.push(ordered ? `<ol>${items.join("")}</ol>` : `<ul>${items.join("")}</ul>`);
      continue;
    }
    if (!line.trim()) { i++; continue; }
    const para = [];
    while (i < lines.length && lines[i].trim() && !/^(```|#{1,6}\s|\s*\||\s*([-*]|\d+\.)\s)/.test(lines[i])) para.push(lines[i++]);
    out.push(`<p>${inline(para.join("\n")).replace(/\n/g, "<br>")}</p>`);
  }
  return out.join("");
}

// ------------------------------------------------------------ side panel (mobile)

function setDrawer(open) {
  if (desktop.matches) open = false;
  drawer.classList.toggle("open", open);
  backdrop.hidden = !open;
  menuBtn.setAttribute("aria-expanded", String(open));
  if (open) loadConversations();
}
menuBtn.addEventListener("click", () => setDrawer(true));
$("#close-drawer").addEventListener("click", () => setDrawer(false));
backdrop.addEventListener("click", () => setDrawer(false));
document.addEventListener("keydown", (e) => { if (e.key === "Escape") setDrawer(false); });
desktop.addEventListener("change", () => setDrawer(false));

// ------------------------------------------------------------ messages

function showEmpty() {
  messagesEl.innerHTML = `<div class="empty"><h2>${escapeHtml(tr("emptyTitle"))}</h2>` +
    `<p class="muted">${escapeHtml(tr("emptyText"))}</p></div>`;
}

function addMessage(role, text) {
  const empty = messagesEl.querySelector(".empty");
  if (empty) empty.remove();
  const wrap = document.createElement("div");
  wrap.className = `msg ${role}`;
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  if (role === "user") bubble.textContent = text;
  else bubble.innerHTML = renderMarkdown(text);
  wrap.appendChild(bubble);
  messagesEl.appendChild(wrap);
  scrollDown();
  return wrap;
}

function scrollDown() {
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

function sourceLink(s) {
  if (s.url) return `<a href="${escapeHtml(s.url)}" target="_blank" rel="noopener">${escapeHtml(s.title)}</a>`;
  if (s.source === "folder") return `<a href="/api/file?path=${encodeURIComponent(s.location)}" target="_blank">${escapeHtml(s.title)}</a>`;
  return escapeHtml(s.title);
}

function renderSources(wrap, sources) {
  if (!sources || !sources.length) return;
  const det = document.createElement("details");
  det.className = "sources";
  det.innerHTML = `<summary>${escapeHtml(tr("sources", { n: sources.length }))}</summary><ol>` + sources.map((s) => {
    const where = s.source === "folder" ? s.location : s.source_label;
    const secs = s.sections && s.sections.length ? ` · ${escapeHtml(s.sections.slice(0, 4).join(", "))}` : "";
    return `<li value="${s.n}">${sourceLink(s)} <span class="muted">— ${escapeHtml(where)}${secs} · ${tr("similarity")} ${s.score}</span></li>`;
  }).join("") + "</ol>";
  wrap.appendChild(det);
}

// ------------------------------------------------------------ optional answer ratings

const REASONS = ["wrong", "incomplete", "not_found", "bad_sources", "other"];
const THUMB = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 11v9H4v-9zM7 11l4-7a2 2 0 0 1 3 2l-1 4h5a2 2 0 0 1 2 2.3l-1.2 6A2 2 0 0 1 16.8 20H7"/></svg>';

async function sendFeedback(conversationId, messageId, body) {
  const url = `/api/conversations/${conversationId}/messages/${messageId}/feedback`;
  const resp = body
    ? await fetch(url, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
    : await fetch(url, { method: "DELETE" });
  if (!resp.ok) throw new Error(resp.statusText);
}

function renderRating(meta, wrap, conversationId, messageId, saved) {
  let state = { rating: saved.rating || 0, reason: saved.reason || null, comment: saved.comment || "" };
  const box = document.createElement("span");
  box.className = "rate";
  const up = document.createElement("button");
  const down = document.createElement("button");
  up.type = down.type = "button";
  up.className = "rate-btn";
  down.className = "rate-btn down";
  up.innerHTML = down.innerHTML = THUMB;
  up.setAttribute("aria-label", tr("rateUp"));
  down.setAttribute("aria-label", tr("rateDown"));
  box.append(up, down);
  meta.appendChild(box);

  const paint = () => {
    up.setAttribute("aria-pressed", String(state.rating === 1));
    down.setAttribute("aria-pressed", String(state.rating === -1));
  };
  const save = async (next) => {
    const previous = state;
    state = next;
    paint();
    try {
      await sendFeedback(conversationId, messageId, next.rating ? next : null);
    } catch {
      state = previous;
      paint();
    }
  };

  const closeForm = () => { const f = wrap.querySelector(".feedback-form"); if (f) f.remove(); };
  const openForm = () => {
    closeForm();
    const f = document.createElement("div");
    f.className = "feedback-form";
    f.innerHTML = `<p class="small">${escapeHtml(tr("reasonTitle"))}</p><div class="chips"></div>` +
      `<textarea rows="2" maxlength="2000"></textarea>` +
      `<div class="row"><button type="button" class="secondary skip"></button><button type="button" class="primary ok"></button></div>`;
    const chips = f.querySelector(".chips");
    for (const r of REASONS) {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "chip";
      chip.textContent = tr(`reason_${r}`);
      chip.setAttribute("aria-pressed", String(state.reason === r));
      chip.addEventListener("click", () => {
        const on = chip.getAttribute("aria-pressed") !== "true";
        chips.querySelectorAll(".chip").forEach((c) => c.setAttribute("aria-pressed", "false"));
        chip.setAttribute("aria-pressed", String(on));
      });
      chips.appendChild(chip);
    }
    const area = f.querySelector("textarea");
    area.placeholder = tr("commentPh");
    area.setAttribute("aria-label", tr("commentPh"));
    area.value = state.comment;
    f.querySelector(".skip").textContent = tr("skip");
    f.querySelector(".ok").textContent = tr("sendFeedback");
    f.querySelector(".skip").addEventListener("click", closeForm);
    f.querySelector(".ok").addEventListener("click", async () => {
      const chosen = chips.querySelector('.chip[aria-pressed="true"]');
      const reason = chosen ? REASONS[[...chips.children].indexOf(chosen)] : null;
      await save({ rating: -1, reason, comment: area.value.trim() });
      f.innerHTML = `<p class="small muted">${escapeHtml(tr("thanks"))}</p>`;
      setTimeout(closeForm, 1800);
    });
    wrap.appendChild(f);
    if (!touch.matches) area.focus();
  };

  up.addEventListener("click", () => {
    closeForm();
    save(state.rating === 1 ? { rating: 0 } : { rating: 1, reason: null, comment: "" });
  });
  down.addEventListener("click", () => {
    if (state.rating === -1) { closeForm(); save({ rating: 0 }); return; }
    save({ rating: -1, reason: null, comment: "" });
    openForm();
  });
  paint();
}

function renderMeta(wrap, provider, model, text, rating = null) {
  const m = document.createElement("div");
  m.className = "meta";
  if (provider) {
    const span = document.createElement("span");
    span.textContent = `${provider} · ${model}`;
    m.appendChild(span);
  }
  if (rating && rating.messageId && currentId) renderRating(m, wrap, currentId, rating.messageId, rating);
  if (text) {
    const copy = document.createElement("button");
    copy.type = "button";
    copy.textContent = tr("copy");
    copy.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(text); copy.textContent = tr("copied"); } catch { copy.textContent = tr("copyFailed"); }
      setTimeout(() => { copy.textContent = tr("copy"); }, 1500);
    });
    m.appendChild(copy);
  }
  wrap.appendChild(m);
}

function setTitle(title) {
  $("#conv-title").textContent = title || tr("appName");
  document.title = title ? `${title} · ${tr("appName")}` : tr("appName");
}

// ------------------------------------------------------------ conversations

function fmtDate(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  const today = new Date();
  return d.toDateString() === today.toDateString()
    ? d.toLocaleTimeString(lang, { hour: "2-digit", minute: "2-digit" })
    : d.toLocaleDateString(lang, { day: "numeric", month: "short" });
}

function emptyList(key) {
  $("#conv-list").innerHTML = `<li class="muted small">${escapeHtml(tr(key))}</li>`;
}

async function loadConversations() {
  const ids = savedIds();
  if (!ids.length) { emptyList("noConversations"); return; }
  try {
    const resp = await fetch("/api/conversations/lookup", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ids }),
    });
    const convs = await resp.json();
    // The ones that no longer exist on the server (deleted or expired) are forgotten
    const alive = new Set(convs.map((c) => c.id));
    store.set("rag.conversations", ids.filter((id) => alive.has(id)));
    if (!convs.length) { emptyList("noConversations"); return; }
    $("#conv-list").replaceChildren(...convs.map(convItem));
  } catch {
    emptyList("loadFailed");
  }
}

function convItem(c) {
  const li = document.createElement("li");
  li.className = "conv-item" + (c.id === currentId ? " active" : "");
  const open = document.createElement("button");
  open.className = "conv-open";
  open.innerHTML = `<span class="t">${escapeHtml(c.title)}</span><span class="d">${fmtDate(c.updated_at)}</span>`;
  open.addEventListener("click", () => { setDrawer(false); openConversation(c.id); });
  const del = document.createElement("button");
  del.className = "icon-btn conv-del";
  del.setAttribute("aria-label", tr("deleteLabel", { title: c.title }));
  del.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 7h14M10 11v6M14 11v6M6 7l1 12h10l1-12M9 7V4h6v3"/></svg>';
  del.addEventListener("click", () => deleteConversation(c));
  li.append(open, del);
  return li;
}

async function deleteConversation(c) {
  if (!confirm(tr("deleteConfirm", { title: c.title }))) return;
  const resp = await fetch(`/api/conversations/${c.id}`, { method: "DELETE" });
  if (resp.ok || resp.status === 404) {
    forgetId(c.id);
    if (c.id === currentId) newConversation();
    loadConversations();
  }
}

async function openConversation(id) {
  if (busy) return;
  try {
    const resp = await fetch(`/api/conversations/${id}`);
    if (resp.status === 404) { forgetId(id); newConversation(); return; }
    const conv = await resp.json();
    currentId = conv.id;
    store.set("rag.current", currentId);
    setTitle(conv.title);
    messagesEl.replaceChildren();
    for (const m of conv.messages) {
      const wrap = addMessage(m.role, m.content);
      if (m.role === "assistant") {
        if (m.error) wrap.querySelector(".bubble").insertAdjacentHTML("beforeend", `<p class="error">${escapeHtml(tr("incomplete", { error: m.error }))}</p>`);
        renderSources(wrap, m.sources);
        renderMeta(wrap, m.provider, m.model, m.content,
          m.error ? null : { messageId: m.id, rating: m.rating, reason: m.reason, comment: m.comment });
      }
    }
    if (!conv.messages.length) showEmpty();
    loadConversations();
  } catch {
    newConversation();
  }
}

function newConversation() {
  if (busy) return;
  currentId = null;
  store.set("rag.current", null);
  setTitle(null);
  showEmpty();
  setDrawer(false);
  loadConversations();
  if (!touch.matches) input.focus();
}
$("#new-chat").addEventListener("click", newConversation);
$("#new-chat-top").addEventListener("click", newConversation);

// ------------------------------------------------------------ asking (SSE over fetch)

function setBusy(value) {
  busy = value;
  sendBtn.hidden = value;
  stopBtn.hidden = !value;
}

async function ask(question) {
  setBusy(true);
  controller = new AbortController();
  addMessage("user", question);
  const wrap = addMessage("assistant", "");
  const bubble = wrap.querySelector(".bubble");
  bubble.classList.add("typing");
  let answer = "";
  let sources = [];
  let meta = null;
  let messageId = null;

  try {
    const resp = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, conversation_id: currentId, provider: providerSel.value || null }),
      signal: controller.signal,
    });
    if (!resp.ok) throw new Error((await resp.json()).detail || resp.statusText);

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buffer.indexOf("\n\n")) >= 0) {
        const raw = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        const event = (raw.match(/^event: (.*)$/m) || [])[1];
        const dataLine = (raw.match(/^data: (.*)$/m) || [])[1];
        if (!event || dataLine === undefined) continue;
        const data = JSON.parse(dataLine);
        if (event === "meta") {
          meta = data;
          if (data.conversation_id && data.conversation_id !== currentId) {
            currentId = data.conversation_id;
            rememberId(currentId);
            store.set("rag.current", currentId);
            setTitle(question.slice(0, 80));
          }
        } else if (event === "sources") sources = data;
        else if (event === "token") {
          answer += data;
          bubble.innerHTML = renderMarkdown(answer);
          scrollDown();
        } else if (event === "done") messageId = data.message_id || null;
        else if (event === "error") throw new Error(data);
      }
    }
  } catch (err) {
    const msg = err.name === "AbortError" ? tr("stopped") : tr("error", { msg: err.message });
    bubble.insertAdjacentHTML("beforeend", `<p class="error">${escapeHtml(msg)}</p>`);
  } finally {
    bubble.classList.remove("typing");
    renderSources(wrap, sources);
    renderMeta(wrap, meta && meta.provider, meta && meta.model, answer, messageId ? { messageId } : null);
    scrollDown();
    controller = null;
    setBusy(false);
    if (!touch.matches) input.focus();
    if (desktop.matches) loadConversations();
  }
}

form.addEventListener("submit", (e) => {
  e.preventDefault();
  const q = input.value.trim();
  if (!q || busy) return;
  input.value = "";
  input.style.height = "auto";
  ask(q);
});
stopBtn.addEventListener("click", () => controller && controller.abort());

input.addEventListener("keydown", (e) => {
  // On touch screens Enter inserts a new line; the button sends
  if (e.key === "Enter" && !e.shiftKey && !touch.matches && !e.isComposing) {
    e.preventDefault();
    form.requestSubmit();
  }
});
input.addEventListener("input", () => {
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, window.innerHeight * 0.4) + "px";
});

// ------------------------------------------------------------ status, provider and indexing

function renderSpend(st) {
  const el = $("#spend");
  if (!st.spend || providerSel.value !== "anthropic") { el.innerHTML = ""; return; }
  const s = st.spend;
  const pct = s.cap_usd ? Math.min(100, (s.month_usd / s.cap_usd) * 100) : 0;
  const cache = s.cache_hit_ratio != null ? ` · ${tr("cache", { pct: Math.round(s.cache_hit_ratio * 100) })}` : "";
  el.innerHTML = (st.dry_run ? `<span class="muted">${escapeHtml(tr("dryRun"))}</span><br>` : "") +
    escapeHtml(tr("spendMonth", { spent: s.month_usd.toFixed(2), cap: s.cap_usd.toFixed(2) })) + cache +
    `<div class="meter${pct > 80 ? " high" : ""}" role="img" aria-label="${escapeHtml(tr("budgetUsed", { pct: Math.round(pct) }))}"><span style="width:${pct}%"></span></div>`;
}

function fillProviders(st) {
  models = st.models;
  const saved = store.get("rag.provider", null);
  const wanted = st.providers.includes(saved) ? saved : st.default_provider;
  providerSel.replaceChildren();
  for (const p of st.providers) {
    const opt = document.createElement("option");
    opt.value = p;
    opt.textContent = tr(`p_${p}`);
    opt.selected = p === wanted;
    providerSel.appendChild(opt);
  }
  if (!st.providers.length) providerSel.innerHTML = `<option value="">${escapeHtml(tr("noProviders"))}</option>`;
  updateModel();
}

function renderIndexStatus(st) {
  const parts = [`<p class="muted">${escapeHtml(tr("chunks", { n: st.chunks_in_index }))} · ${escapeHtml(st.embed_model)}</p>`];
  const confluenceOn = st.confluence && st.confluence.configured;
  document.querySelector('button[data-source="confluence"]').hidden = !confluenceOn;
  for (const key of confluenceOn ? ["folder", "confluence"] : ["folder"]) {
    const s = st.sources[key];
    const run = s && s.last_sync;
    const cls = run ? (run.status === "running" || run.status === "waiting" ? "running" : run.status === "ok" ? "ok" : "error") : "running";
    const docs = s && Object.keys(s.docs).length
      ? Object.entries(s.docs).map(([k, v]) => `${v} ${tr(`st_${k}`)}`).join(" · ") : tr("noDocs");
    const extra = key === "folder"
      ? (st.folder.auto ? "" : `<div class="muted">${escapeHtml(tr("autoOff"))}</div>`)
      : `<div class="muted">${escapeHtml(st.confluence.spaces.join(", "))} · ` +
        escapeHtml(st.confluence.sync_time ? tr("daily", { time: st.confluence.sync_time }) : tr("autoOff")) + "</div>";
    const redacted = s && s.redactions ? `<div class="muted">${escapeHtml(tr("redacted", { n: s.redactions }))}</div>` : "";
    const when = !run ? tr("pending")
      : run.status === "running" ? tr("syncing")
      : run.finished_at ? tr("last", { date: new Date(run.finished_at * 1000).toLocaleString(lang) }) : tr("pending");
    const message = run && run.message && run.status !== "running" ? `<div class="muted">${escapeHtml(run.message)}</div>` : "";
    parts.push(`<div class="source-card"><b><span class="dot ${cls}"></span>${tr(key)}</b>${escapeHtml(docs)}${extra}${redacted}` +
      `<div class="muted">${escapeHtml(when)}</div>${message}</div>`);
  }
  $("#status").innerHTML = parts.join("");
}

async function loadStatus() {
  if (document.hidden) return;
  try {
    const st = await (await fetch("/api/status")).json();
    const firstLoad = !lastStatus;
    lastStatus = st;
    if (st.language && st.language !== lang) applyLanguage(st.language);
    if (firstLoad) fillProviders(st);
    renderSpend(st);
    renderIndexStatus(st);
  } catch {
    $("#status").textContent = tr("statusFailed");
  }
}

function updateModel() {
  $("#model").textContent = models[providerSel.value] ? tr("model", { model: models[providerSel.value] }) : "";
  if (lastStatus) renderSpend(lastStatus);
}
providerSel.addEventListener("change", () => {
  store.set("rag.provider", providerSel.value);
  updateModel();
});

document.querySelectorAll("button[data-source]").forEach((btn) =>
  btn.addEventListener("click", async () => {
    const resp = await fetch("/api/reindex", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source: btn.dataset.source }),
    });
    const data = await resp.json();
    $("#reindex-msg").textContent = resp.ok ? tr("started", { list: data.started.join(", ") }) : data.detail;
    setTimeout(loadStatus, 1500);
  })
);

// ------------------------------------------------------------ startup

applyLanguage(lang);
loadStatus();
setInterval(loadStatus, 15000);
document.addEventListener("visibilitychange", loadStatus);
const lastOpen = store.get("rag.current", null);
if (lastOpen) openConversation(lastOpen);
else { showEmpty(); loadConversations(); }
if (!touch.matches) input.focus();
