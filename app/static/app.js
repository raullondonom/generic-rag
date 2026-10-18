"use strict";
// End-user page: conversation, history, sources and ratings. Everything internal lives in /admin.

const messagesEl = $("#messages");
const form = $("#form");
const input = $("#question");
const sendBtn = $("#send");
const stopBtn = $("#stop");
const drawer = $("#drawer");
const backdrop = $("#backdrop");
const menuBtn = $("#menu-btn");

const MAX_SAVED = 100;
const desktop = window.matchMedia("(min-width: 900px)");
const touch = window.matchMedia("(pointer: coarse)");

let currentId = null;   // open conversation (null = new)
let busy = false;
let controller = null;  // AbortController of the answer in progress
let brand = { name: "", tagline: "", welcome_title: "", welcome_text: "", suggestions: [], disclaimer: "" };

// ------------------------------------------------------------ saved conversation ids (the history lives on the server)

const savedIds = () => store.get("rag.conversations", []);
function rememberId(id) {
  store.set("rag.conversations", [id, ...savedIds().filter((x) => x !== id)].slice(0, MAX_SAVED));
}
function forgetId(id) {
  store.set("rag.conversations", savedIds().filter((x) => x !== id));
}

// ------------------------------------------------------------ user context (ui.json context_fields), kept in this browser

let contextFields = [];

function savedContext() {
  const saved = store.get("rag.context", {});
  return saved && typeof saved === "object" ? saved : {};
}

function currentContext() {
  const saved = savedContext();
  const out = {};
  for (const f of contextFields) if (saved[f.id] && f.options.includes(saved[f.id])) out[f.id] = saved[f.id];
  return out;
}

function renderContext(fields) {
  contextFields = fields || [];
  const bar = $("#context-bar");
  bar.hidden = !contextFields.length;
  const chosen = currentContext();
  bar.replaceChildren(...contextFields.map((f) => {
    const label = document.createElement("label");
    label.className = "ctx";
    const name = document.createElement("span");
    name.textContent = f.label;
    const sel = document.createElement("select");
    sel.innerHTML = `<option value="">${escapeHtml(f.placeholder || tr("any"))}</option>` +
      f.options.map((o) => `<option${chosen[f.id] === o ? " selected" : ""}>${escapeHtml(o)}</option>`).join("");
    sel.classList.toggle("set", !!chosen[f.id]);
    sel.addEventListener("change", () => {
      const saved = savedContext();
      if (sel.value) saved[f.id] = sel.value; else delete saved[f.id];
      store.set("rag.context", saved);
      sel.classList.toggle("set", !!sel.value);
    });
    label.append(name, sel);
    return label;
  }));
}

// ------------------------------------------------------------ branding of the collection (_control/ui.json)

async function loadBranding() {
  try {
    const c = await (await fetch("/api/config")).json();
    brand = c;
    applyLanguage(c.language);
    applyColors(c.colors);
    document.querySelectorAll(".brand-icon").forEach((img) => { img.src = c.icon_url; });
    $("#favicon").href = c.icon_url;
    $("#touch-icon").href = c.icon_url;
    $("#brand-name").textContent = c.name;
    $("#brand-tagline").textContent = c.tagline;
    document.querySelector('meta[name="description"]').setAttribute("content", c.tagline);
    input.placeholder = c.placeholder;
    renderContext(c.context_fields);
    const notices = [c.disclaimer, tr("aiNotice"), tr("privacyNotice")].filter(Boolean).join(" ");
    $("#footer-notice").textContent = notices;
    $("#drawer-notice").textContent = notices;
    if (!c.ready) $("#footer-notice").textContent = tr("notReady");
    if (!currentId) setTitle(null);
    if (messagesEl.querySelector(".empty")) showEmpty();
  } catch {
    applyLanguage(lang);
  }
}

// ------------------------------------------------------------ minimal, safe Markdown

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

// Answers come as "Respuesta: … Sustento: …" (see prompts.py): the answer stands out and the basis
// folds. Anything else (old messages, guardrails, deviations) is rendered as plain Markdown.
const ANSWER_RE = /^\s*\**\s*(Respuesta|Answer)\s*:?\s*\**\s*:?\s*/i;
const BASIS_RE = /(?:^|\n)\s*\**\s*(Sustento|Basis)\s*:?\s*\**\s*:?[ \t]*\n?/i;

function renderAnswer(text) {
  const head = text.match(ANSWER_RE);
  if (!head) return renderMarkdown(text);
  const rest = text.slice(head[0].length);
  const split = rest.match(BASIS_RE);
  const answer = split ? rest.slice(0, split.index) : rest;
  let html = `<div class="answer"><span class="label">${escapeHtml(head[1])}</span>${renderMarkdown(answer)}</div>`;
  if (split) {
    const label = `${split[1][0].toUpperCase()}${split[1].slice(1).toLowerCase()}`;
    html += `<details class="basis" open><summary>${escapeHtml(label)}</summary>${renderMarkdown(rest.slice(split.index + split[0].length))}</details>`;
  }
  return html;
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
  const suggestions = (brand.suggestions || []).map((s) =>
    `<button type="button" class="suggestion">${escapeHtml(s)}</button>`).join("");
  messagesEl.innerHTML = `<div class="empty">
      <img class="brand-icon hero" src="${escapeHtml(brand.icon_url || "/icon.svg")}" alt="">
      <h2>${escapeHtml(brand.welcome_title)}</h2>
      <p class="muted">${escapeHtml(brand.welcome_text)}</p>
      ${suggestions ? `<p class="small muted">${escapeHtml(tr("suggestionsTitle"))}</p><div class="suggestions">${suggestions}</div>` : ""}
    </div>`;
  messagesEl.querySelectorAll(".suggestion").forEach((b) =>
    b.addEventListener("click", () => { if (!busy) ask(b.textContent); }));
}

function addMessage(role, text) {
  const empty = messagesEl.querySelector(".empty");
  if (empty) empty.remove();
  const wrap = document.createElement("div");
  wrap.className = `msg ${role}`;
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  if (role === "user") bubble.textContent = text;
  else bubble.innerHTML = renderAnswer(text);
  wrap.appendChild(bubble);
  messagesEl.appendChild(wrap);
  scrollDown();
  return wrap;
}

function scrollDown() {
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

function validity(s) {
  if (!s.valid_until) return "";
  const expired = s.valid_until < new Date().toISOString().slice(0, 10);
  return expired ? ` · <span class="error">${escapeHtml(tr("expiredOn", { date: s.valid_until }))}</span>`
    : ` · ${escapeHtml(tr("validUntil", { date: s.valid_until }))}`;
}

// Numbers cited in the answer as [n] or [n, m]
function citedNumbers(text) {
  const out = new Set();
  for (const m of (text || "").matchAll(/\[(\d+(?:\s*,\s*\d+)*)\]/g)) {
    m[1].split(",").forEach((n) => out.add(Number(n)));
  }
  return out;
}

// Only the documents the answer cites (all of them if it cites none)
function renderSources(wrap, sources, answer) {
  const cited = citedNumbers(answer);
  if (sources && cited.size) sources = sources.filter((s) => cited.has(s.n));
  if (!sources || !sources.length) return;
  const det = document.createElement("details");
  det.className = "sources";
  det.innerHTML = `<summary>${escapeHtml(tr("sources", { n: sources.length }))}</summary><ol>` + sources.map((s) => {
    const local = s.source === "folder" ? `/api/file?path=${encodeURIComponent(s.location)}` : "";
    const main = s.url || local;
    const title = main ? `<a href="${escapeHtml(main)}" target="_blank" rel="noopener">${escapeHtml(s.title)}</a>` : escapeHtml(s.title);
    const extra = s.url && local ? ` · <a href="${escapeHtml(local)}" target="_blank">${escapeHtml(tr("file"))}</a>` : "";
    const where = s.jurisdiction ? ` · ${escapeHtml(s.jurisdiction)}` : "";
    return `<li value="${s.n}">${title}<span class="muted">${extra}${where}${validity(s)}</span></li>`;
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
    try { await sendFeedback(conversationId, messageId, next.rating ? next : null); } catch { state = previous; paint(); }
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

function renderMeta(wrap, text, rating = null) {
  const m = document.createElement("div");
  m.className = "meta";
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
  const name = brand.name || document.title;
  $("#conv-title").textContent = title || name;
  document.title = title ? `${title} · ${name}` : name;
}

// ------------------------------------------------------------ conversations

function fmtDate(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  return d.toDateString() === new Date().toDateString()
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
        if (m.error) wrap.querySelector(".bubble").insertAdjacentHTML("beforeend", `<p class="error">${escapeHtml(tr("incomplete"))}</p>`);
        renderSources(wrap, m.sources, m.content);
        renderMeta(wrap, m.content, m.error ? null : { messageId: m.id, rating: m.rating, reason: m.reason, comment: m.comment });
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
  let messageId = null;

  try {
    const resp = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, conversation_id: currentId, context: currentContext() }),
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
          if (data.conversation_id && data.conversation_id !== currentId) {
            currentId = data.conversation_id;
            rememberId(currentId);
            store.set("rag.current", currentId);
            setTitle(question.slice(0, 80));
          }
        } else if (event === "sources") sources = data;
        else if (event === "token") {
          answer += data;
          bubble.innerHTML = renderAnswer(answer);
          scrollDown();
        } else if (event === "done") messageId = data.message_id || null;
        else if (event === "error") throw new Error(data);
      }
    }
  } catch (err) {
    const msg = err.name === "AbortError" ? tr("stopped") : err.message;
    bubble.insertAdjacentHTML("beforeend", `<p class="error">${escapeHtml(msg)}</p>`);
  } finally {
    bubble.classList.remove("typing");
    renderSources(wrap, sources, answer);
    renderMeta(wrap, answer, messageId ? { messageId } : null);
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

// ------------------------------------------------------------ startup

applyLanguage(lang);
loadBranding().then(() => {
  const lastOpen = store.get("rag.current", null);
  if (lastOpen) openConversation(lastOpen);
  else { showEmpty(); loadConversations(); }
});
if (!touch.matches) input.focus();
