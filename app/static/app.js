"use strict";

const $ = (sel) => document.querySelector(sel);
const messagesEl = $("#messages");
const form = $("#form");
const input = $("#question");
const sendBtn = $("#send");
const providerSel = $("#provider");

let history = [];       // [{role, content}]
let busy = false;
let models = {};

// ------------------------------------------------------------ Markdown mínimo y seguro

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function inline(s) {
  return escapeHtml(s)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>")
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

// ------------------------------------------------------------ UI de mensajes

function clearEmpty() {
  const empty = messagesEl.querySelector(".empty");
  if (empty) empty.remove();
}

function addMessage(role, text) {
  clearEmpty();
  const wrap = document.createElement("div");
  wrap.className = `msg ${role}`;
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  if (role === "user") bubble.textContent = text;
  else bubble.innerHTML = renderMarkdown(text);
  wrap.appendChild(bubble);
  messagesEl.appendChild(wrap);
  messagesEl.scrollTop = messagesEl.scrollHeight;
  return wrap;
}

function sourceLink(s) {
  if (s.url) return `<a href="${escapeHtml(s.url)}" target="_blank" rel="noopener">${escapeHtml(s.title)}</a>`;
  if (s.source === "folder") return `<a href="/api/file?path=${encodeURIComponent(s.location)}" target="_blank">${escapeHtml(s.title)}</a>`;
  return escapeHtml(s.title);
}

function renderSources(wrap, sources) {
  if (!sources.length) return;
  const det = document.createElement("details");
  det.className = "sources";
  det.innerHTML = `<summary>Fuentes (${sources.length})</summary><ol>` + sources.map((s) => {
    const where = s.source === "folder" ? s.location : s.source_label;
    const secs = s.sections && s.sections.length ? ` · ${escapeHtml(s.sections.slice(0, 4).join(", "))}` : "";
    return `<li value="${s.n}">${sourceLink(s)} <span class="muted">— ${escapeHtml(where)}${secs} · similitud ${s.score}</span></li>`;
  }).join("") + "</ol>";
  wrap.appendChild(det);
}

// ------------------------------------------------------------ chat (SSE sobre fetch)

async function ask(question) {
  busy = true;
  sendBtn.disabled = true;
  addMessage("user", question);
  const wrap = addMessage("assistant", "");
  const bubble = wrap.querySelector(".bubble");
  bubble.classList.add("typing");
  let answer = "";
  let sources = [];
  let meta = null;

  try {
    const resp = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, history, provider: providerSel.value || null }),
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
        if (event === "meta") meta = data;
        else if (event === "sources") sources = data;
        else if (event === "token") {
          answer += data;
          bubble.innerHTML = renderMarkdown(answer);
          messagesEl.scrollTop = messagesEl.scrollHeight;
        } else if (event === "error") throw new Error(data);
      }
    }
    history.push({ role: "user", content: question }, { role: "assistant", content: answer });
  } catch (err) {
    bubble.innerHTML += `<p class="error">Error: ${escapeHtml(err.message)}</p>`;
  } finally {
    bubble.classList.remove("typing");
    renderSources(wrap, sources);
    if (meta) {
      const m = document.createElement("div");
      m.className = "meta";
      m.textContent = `${meta.provider} · ${meta.model}`;
      wrap.appendChild(m);
    }
    busy = false;
    sendBtn.disabled = false;
    input.focus();
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

input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    form.requestSubmit();
  }
});
input.addEventListener("input", () => {
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 200) + "px";
});

$("#new-chat").addEventListener("click", () => {
  history = [];
  messagesEl.innerHTML = '<div class="empty"><h2>¿Qué quieres consultar?</h2><p class="muted">Las respuestas se basan únicamente en los documentos indexados y citan sus fuentes.</p></div>';
});

// ------------------------------------------------------------ estado e indexación

const NAMES = { folder: "Carpeta local", confluence: "Confluence" };
const STATUS_NAMES = { indexed: "indexados", unsupported: "no soportados", error: "con error", empty: "sin texto" };

function fmtDate(ts) {
  return ts ? new Date(ts * 1000).toLocaleString() : "—";
}

async function loadStatus() {
  try {
    const st = await (await fetch("/api/status")).json();
    if (!providerSel.options.length) {
      models = st.models;
      for (const p of st.providers) {
        const opt = document.createElement("option");
        opt.value = p;
        opt.textContent = p === "anthropic" ? "Claude (Anthropic)" : "Ollama (local)";
        if (p === st.default_provider) opt.selected = true;
        providerSel.appendChild(opt);
      }
      providerSel.dispatchEvent(new Event("change"));
    }
    const parts = [`<p class="muted">${st.chunks_in_index} fragmentos en el índice · embeddings: ${escapeHtml(st.embed_model)}</p>`];
    for (const key of ["folder", "confluence"]) {
      const s = st.sources[key];
      if (key === "confluence" && !st.confluence.configured) {
        parts.push(`<div class="source-card"><b>Confluence</b><span class="muted">No configurado</span></div>`);
        continue;
      }
      const run = s && s.last_sync;
      const cls = run ? (run.status === "running" ? "running" : run.status === "ok" ? "ok" : "error") : "running";
      const docs = s ? Object.entries(s.docs).map(([k, v]) => `${v} ${STATUS_NAMES[k] || k}`).join(" · ") : "sin documentos";
      const extra = key === "confluence" ? `<div class="muted">Espacios: ${escapeHtml(st.confluence.spaces.join(", "))}</div>` : "";
      parts.push(`<div class="source-card"><b><span class="dot ${cls}"></span>${NAMES[key]}</b>${docs}${extra}
        <div class="muted">${run ? (run.status === "running" ? "Sincronizando…" : `Última: ${fmtDate(run.finished_at)}`) : "Pendiente"}</div>
        ${run && run.message && run.status !== "running" ? `<div class="muted">${escapeHtml(run.message)}</div>` : ""}</div>`);
    }
    $("#status").innerHTML = parts.join("");
  } catch {
    $("#status").textContent = "No se pudo obtener el estado";
  }
}

providerSel.addEventListener("change", () => {
  $("#model").textContent = models[providerSel.value] ? `Modelo: ${models[providerSel.value]}` : "";
});

document.querySelectorAll("button[data-source]").forEach((btn) =>
  btn.addEventListener("click", async () => {
    const resp = await fetch("/api/reindex", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source: btn.dataset.source }),
    });
    const data = await resp.json();
    $("#reindex-msg").textContent = resp.ok ? `Iniciado: ${data.started.join(", ")}` : data.detail;
    setTimeout(loadStatus, 1500);
  })
);

loadStatus();
setInterval(loadStatus, 10000);
input.focus();
