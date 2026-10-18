"use strict";
// Shared by the end-user page (app.js) and the administration panel (admin.js).

const $ = (sel) => document.querySelector(sel);

const store = {
  get(key, fallback) {
    try { const v = localStorage.getItem(key); return v === null ? fallback : JSON.parse(v); } catch { return fallback; }
  },
  set(key, value) {
    try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* private mode or blocked storage */ }
  },
};

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ------------------------------------------------------------ texts (LANGUAGE=en|es on the server)

const I18N = {
  en: {
    newChat: "New conversation", conversations: "Conversations", send: "Send", stop: "Stop answer",
    openMenu: "Open menu", closeMenu: "Close menu", panel: "Conversations", question: "Question",
    noConversations: "No saved conversations", loadFailed: "Could not load them",
    deleteLabel: 'Delete "{title}"', deleteConfirm: 'Delete the conversation "{title}"? This cannot be undone.',
    incomplete: "This answer could not be completed.", stopped: "Answer stopped.",
    sources: "Sources ({n})", official: "official source", file: "document", validUntil: "valid until {date}",
    expiredOn: "expired on {date}", copy: "Copy", copied: "Copied", copyFailed: "Could not copy",
    notReady: "The assistant is not configured yet.", any: "Any", suggestionsTitle: "You can ask, for example:",
    aiNotice: "Answers are written by an artificial intelligence and may contain mistakes: check the cited official source.",
    privacyNotice: "Questions are stored to improve the service; do not write personal data.",
    rateUp: "Useful answer", rateDown: "Not useful", reasonTitle: "What went wrong? (optional)",
    reason_wrong: "Incorrect", reason_incomplete: "Incomplete", reason_not_found: "Didn't find the info",
    reason_bad_sources: "Wrong sources", reason_other: "Other", commentPh: "Comment (optional)",
    sendFeedback: "Send", skip: "Skip", thanks: "Thanks for your feedback",
    // admin panel
    adminTitle: "Administration", backToApp: "Back to the assistant", tokenTitle: "Administration access",
    tokenText: "Enter the administration token (ADMIN_TOKEN).", tokenPh: "Token", tokenEnter: "Enter",
    tokenWrong: "Wrong token.", forbidden: "The panel only opens from this machine. Set ADMIN_TOKEN to open it from elsewhere.",
    logout: "Forget token", provider: "AI provider", providerDefault: "default ({name})", model: "Model: {model}",
    providerSaved: "Saved", spend: "Claude spend", dryRun: "Simulated mode (DRY_RUN): nothing is spent.",
    spendMonth: "This month: USD {spent} of {cap} · {calls} calls", cache: "cache {pct}%",
    index: "Index", chunks: "{n} chunks · {model}", folder: "Local folder", confluence: "Confluence",
    st_indexed: "indexed", st_unsupported: "unsupported", st_error: "with errors", st_empty: "no text",
    noDocs: "no documents", redacted: "{n} sensitive values redacted", syncing: "Syncing…",
    waiting: "Waiting for the embeddings service", last: "Last: {date}", pending: "Pending",
    daily: "Every day at {time}", autoOff: "Automatic updates off", reindexFolder: "Update folder",
    reindexFull: "Reindex everything", fullConfirm: "Reindex every document from scratch? It can take a long time.",
    syncConfluence: "Sync Confluence", started: "Started: {list}",
    ctlTitle: "Source control", ctlRecords: "{records} records in {n} registries", ctlNever: "Not reviewed yet",
    ctlLast: "Last review: {date}", ctlOffline: " (offline)", ctlRunning: "Reviewing sources…",
    ctlError: "Review failed: {error}", ctlNothing: "Nothing pending", ctlDetails: "Pending items",
    ctlMore: "{n} more: see GET /api/control", ctlCheck: "Review sources", ctlStarted: "Source review started",
    ctlNone: "There is no control folder in the documents folder.",
    sec_expired: "expired", sec_expiring: "expiring soon", sec_changed: "changed online", sec_review_due: "review due",
    sec_missing: "missing on disk", sec_unreachable: "unreachable URLs", sec_no_document: "no document found",
    sec_unregistered: "without a registry row",
    ratings: "Ratings (30 days)", satisfaction: "Satisfaction: {pct}% · {up} useful, {down} not useful",
    noRatings: "No ratings yet", byModel: "By provider and model", byReason: "Reasons",
    problems: "Documents with problems", noProblems: "No documents with errors or without text",
    p_anthropic: "Claude (Anthropic)", p_ollama: "Ollama (local)", noProviders: "No providers configured",
    budgetUsed: "{pct}% of the cap used", statusFailed: "Could not get the status", refresh: "Refresh",
    providerHelp: "Every answer uses this provider; users cannot change it.", unspecified: "No reason",
  },
  es: {
    newChat: "Nueva conversación", conversations: "Conversaciones", send: "Enviar", stop: "Detener respuesta",
    openMenu: "Abrir menú", closeMenu: "Cerrar menú", panel: "Conversaciones", question: "Pregunta",
    noConversations: "Sin conversaciones guardadas", loadFailed: "No se pudieron cargar",
    deleteLabel: 'Borrar "{title}"', deleteConfirm: '¿Borrar la conversación "{title}"? No se puede deshacer.',
    incomplete: "Esta respuesta no se pudo completar.", stopped: "Respuesta detenida.",
    sources: "Fuentes ({n})", official: "fuente oficial", file: "documento", validUntil: "vigente hasta {date}",
    expiredOn: "vencida el {date}", copy: "Copiar", copied: "Copiado", copyFailed: "No se pudo copiar",
    notReady: "El asistente todavía no está configurado.", any: "Cualquiera", suggestionsTitle: "Puedes preguntar, por ejemplo:",
    aiNotice: "Responde una inteligencia artificial y puede cometer errores: verifica la información en la fuente oficial citada.",
    privacyNotice: "Tus preguntas se guardan para mejorar el servicio; no escribas datos personales.",
    rateUp: "Respuesta útil", rateDown: "No me sirvió", reasonTitle: "¿Qué falló? (opcional)",
    reason_wrong: "Incorrecta", reason_incomplete: "Incompleta", reason_not_found: "No encontró la info",
    reason_bad_sources: "Fuentes equivocadas", reason_other: "Otro", commentPh: "Comentario (opcional)",
    sendFeedback: "Enviar", skip: "Omitir", thanks: "Gracias por tu opinión",
    adminTitle: "Administración", backToApp: "Volver al asistente", tokenTitle: "Acceso de administración",
    tokenText: "Escribe el token de administración (ADMIN_TOKEN).", tokenPh: "Token", tokenEnter: "Entrar",
    tokenWrong: "Token incorrecto.", forbidden: "El panel solo abre desde este equipo. Configura ADMIN_TOKEN para abrirlo desde otro lugar.",
    logout: "Olvidar token", provider: "Proveedor de IA", providerDefault: "predeterminado ({name})", model: "Modelo: {model}",
    providerSaved: "Guardado", spend: "Gasto en Claude", dryRun: "Modo simulado (DRY_RUN): no se gasta.",
    spendMonth: "Este mes: USD {spent} de {cap} · {calls} llamadas", cache: "caché {pct}%",
    index: "Índice", chunks: "{n} fragmentos · {model}", folder: "Carpeta local", confluence: "Confluence",
    st_indexed: "indexados", st_unsupported: "no soportados", st_error: "con error", st_empty: "sin texto",
    noDocs: "sin documentos", redacted: "{n} valores sensibles redactados", syncing: "Sincronizando…",
    waiting: "Esperando el servicio de embeddings", last: "Última: {date}", pending: "Pendiente",
    daily: "Todos los días a las {time}", autoOff: "Actualización automática apagada", reindexFolder: "Actualizar carpeta",
    reindexFull: "Reindexar todo", fullConfirm: "¿Reindexar todos los documentos desde cero? Puede tardar bastante.",
    syncConfluence: "Sincronizar Confluence", started: "Iniciado: {list}",
    ctlTitle: "Control de fuentes", ctlRecords: "{records} registros en {n} archivos de registro", ctlNever: "Sin revisar todavía",
    ctlLast: "Última revisión: {date}", ctlOffline: " (sin conexión)", ctlRunning: "Revisando fuentes…",
    ctlError: "La revisión falló: {error}", ctlNothing: "Nada pendiente", ctlDetails: "Pendientes",
    ctlMore: "{n} más: ver GET /api/control", ctlCheck: "Revisar fuentes", ctlStarted: "Revisión de fuentes iniciada",
    ctlNone: "No hay carpeta de control en la carpeta de documentos.",
    sec_expired: "vencidos", sec_expiring: "por vencer", sec_changed: "cambiaron en línea", sec_review_due: "revisión pendiente",
    sec_missing: "faltan en disco", sec_unreachable: "URLs inaccesibles", sec_no_document: "sin documento encontrado",
    sec_unregistered: "sin fila en el registro",
    ratings: "Calificaciones (30 días)", satisfaction: "Satisfacción: {pct}% · {up} útiles, {down} no útiles",
    noRatings: "Sin calificaciones todavía", byModel: "Por proveedor y modelo", byReason: "Motivos",
    problems: "Documentos con problemas", noProblems: "No hay documentos con error ni sin texto",
    p_anthropic: "Claude (Anthropic)", p_ollama: "Ollama (local)", noProviders: "Sin proveedores configurados",
    budgetUsed: "{pct}% del tope usado", statusFailed: "No se pudo obtener el estado", refresh: "Actualizar",
    providerHelp: "Todas las respuestas usan este proveedor; los usuarios no pueden cambiarlo.", unspecified: "Sin motivo",
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
}

// The collection's colors (ui.json "colors"): hex values validated by the server
function applyColors(colors) {
  const root = document.documentElement.style;
  const map = { accent: "--brand-accent", accent_text: "--brand-accent-text", accent_dark: "--brand-accent-dark",
                accent_text_dark: "--brand-accent-text-dark" };
  for (const [key, prop] of Object.entries(map)) {
    if (colors && colors[key]) root.setProperty(prop, colors[key]);
  }
  if (colors && colors.accent) {
    document.querySelectorAll('meta[name="theme-color"]').forEach((m) => m.setAttribute("content", colors.accent));
  }
}

function fmtDateTime(ts) {
  return ts ? new Date(ts * 1000).toLocaleString(lang) : "—";
}
