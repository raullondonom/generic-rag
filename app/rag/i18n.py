"""User-facing texts in every supported language (LANGUAGE=en|es).

Code, logs and comments are in English; only what the end user reads is localized.
"""

from __future__ import annotations

LANGUAGES = ("en", "es")

TEXTS: dict[str, dict[str, str]] = {
    "en": {
        # Answers that never reach a model
        "no_context": (
            "I couldn't find information about this in the indexed documents.\n\n"
            "Can you give me more details so I can help? For example:\n"
            "- the exact system, project or term you mean,\n"
            "- or the document / Confluence space where that information should be."
        ),
        "greeting": (
            "Hi! I answer only with the information in the indexed documents "
            "What would you like to look up?"
        ),
        # Notes appended to a model answer
        "refusal": "\n\n_The request was declined by the model's safety policies._",
        "unavailable": "The assistant is not available right now. Please try again in a few minutes.",
        "cancelled": "Answer stopped.",
        "busy": "Many people are asking right now. Please try again in a moment.",
        "truncated": "\n\n_(Answer truncated: ANTHROPIC_MAX_TOKENS was reached)_",
        "dry_run": "_(DRY_RUN: simulated answer, Claude was not called.)_\n\n{docs} document(s) were retrieved for this question",
        "over_budget": (
            "Monthly Claude budget reached: {purpose} could cost up to USD {estimated:.4f} and USD {available:.4f} "
            "of USD {cap:.2f} remain. Use Ollama or raise MONTHLY_BUDGET_USD."
        ),
        "purpose_answer": "the answer",
        "purpose_rewrite": "preparing the search",
        "purpose_context": "the search notes of the chunks",
        "purpose_rerank": "ranking the documents found",
        # Source labels and locations inside files
        "local_folder": "Local folder",
        "page": "page {n}",
        "sheet": "sheet {name}",
        "slide": "slide {n}",
        "chapter": "chapter {n}",
        "notes": "Notes",
        "email_header": "Subject: {subject}\nFrom: {sender}\nTo: {to}\nDate: {date}\n",
        "space": "Space: {name} ({key})",
        # Sync status shown in the UI
        "folder_running": "The folder is already being indexed",
        "folder_missing": "Folder {path} does not exist",
        "folder_done": "{changed} processed, {removed} removed, {seen} files in folder",
        "confluence_off": "Confluence is not configured",
        "confluence_running": "Confluence is already syncing",
        "confluence_done": ("{pages} pages, {attachments} attachments, {changed} processed, "
                            "{removed} removed, {errors} errors"),
        "confluence_missing_spaces": " · spaces not found: {spaces}",
        "embeddings_waiting": "Waiting for the embeddings service",
        "cluster_waiting": "Waiting for the Qdrant nodes",
        # Document states
        "no_text": "no extractable text",
        "too_big": "larger than MAX_FILE_MB ({mb} MB)",
        "unsupported_ext": "format {ext} not supported",
        "binary": "binary file with no available reader",
        "image_no_ocr": "image: enable OCR_ENABLED to extract its text",
        "doc_needs_antiword": ".doc requires antiword",
        "redaction_marker": "[REDACTED]",
        # Context added to every chunk (folder path and source registry metadata)
        "ctx_folder": "Folder",
        "ctx_jurisdiction": "Jurisdiction",
        "ctx_issuer": "Issuer",
        "ctx_type": "Type",
        "ctx_status": "Status",
        "ctx_valid_until": "Valid until",
        "ctx_ocr": "Text obtained by OCR from a scanned PDF: it may contain recognition errors",
        "corpus_notes": ("Notes from the administrator of this document collection. Follow them in every "
                         "answer; they take precedence over what the documents say:"),
    },
    "es": {
        "no_context": (
            "No encontré información sobre esto en los documentos indexados.\n\n"
            "Para poder ayudarte, ¿puedes darme más detalles? Por ejemplo:\n"
            "- el sistema, proyecto o término exacto al que te refieres,\n"
            "- o el documento / espacio de Confluence donde debería estar esa información."
        ),
        "greeting": (
            "¡Hola! Soy un asistente que responde únicamente con la información de los documentos indexados "
            "¿Qué necesitas consultar?"
        ),
        "refusal": "\n\n_La solicitud fue rechazada por las políticas de seguridad del modelo._",
        "unavailable": "El asistente no está disponible en este momento. Inténtalo de nuevo en unos minutos.",
        "cancelled": "Respuesta detenida.",
        "busy": "Hay muchas consultas en este momento. Inténtalo de nuevo en unos segundos.",
        "truncated": "\n\n_(Respuesta truncada: se alcanzó ANTHROPIC_MAX_TOKENS)_",
        "dry_run": "_(DRY_RUN: respuesta simulada, no se llamó a Claude.)_\n\nSe recuperaron {docs} documento(s) para esta pregunta",
        "over_budget": (
            "Tope mensual de Claude alcanzado: {purpose} costaría hasta USD {estimated:.4f} y quedan "
            "USD {available:.4f} de USD {cap:.2f}. Usa Ollama o sube MONTHLY_BUDGET_USD."
        ),
        "purpose_answer": "la respuesta",
        "purpose_rewrite": "preparar la búsqueda",
        "purpose_context": "las notas de búsqueda de los fragmentos",
        "purpose_rerank": "ordenar los documentos encontrados",
        "local_folder": "Carpeta local",
        "page": "página {n}",
        "sheet": "hoja {name}",
        "slide": "diapositiva {n}",
        "chapter": "capítulo {n}",
        "notes": "Notas",
        "email_header": "Asunto: {subject}\nDe: {sender}\nPara: {to}\nFecha: {date}\n",
        "space": "Espacio: {name} ({key})",
        "folder_running": "La carpeta ya se está indexando",
        "folder_missing": "No existe la carpeta {path}",
        "folder_done": "{changed} procesados, {removed} eliminados, {seen} archivos en carpeta",
        "confluence_off": "Confluence no está configurado",
        "confluence_running": "Confluence ya se está sincronizando",
        "confluence_done": ("{pages} páginas, {attachments} adjuntos, {changed} procesados, "
                            "{removed} eliminados, {errors} errores"),
        "confluence_missing_spaces": " · espacios no encontrados: {spaces}",
        "embeddings_waiting": "Esperando el servicio de embeddings",
        "cluster_waiting": "Esperando los nodos de Qdrant",
        "no_text": "sin texto extraíble",
        "too_big": "supera MAX_FILE_MB ({mb} MB)",
        "unsupported_ext": "formato {ext} no soportado",
        "binary": "archivo binario sin lector disponible",
        "image_no_ocr": "imagen: activa OCR_ENABLED para extraer su texto",
        "doc_needs_antiword": ".doc requiere antiword",
        "redaction_marker": "[REDACTADO]",
        "ctx_folder": "Carpeta",
        "ctx_jurisdiction": "Jurisdicción",
        "ctx_issuer": "Emisor",
        "ctx_type": "Tipo",
        "ctx_status": "Estado",
        "ctx_valid_until": "Vigente hasta",
        "ctx_ocr": "Texto obtenido por OCR de un PDF escaneado: puede tener errores de reconocimiento",
        "corpus_notes": ("Notas del administrador de esta colección de documentos. Síguelas en todas las "
                         "respuestas; prevalecen sobre lo que digan los documentos:"),
    },
}


def normalize(language: str) -> str:
    language = (language or "en").lower()[:2]
    return language if language in LANGUAGES else "en"


def t(language: str, key: str, /, **kwargs) -> str:
    text = TEXTS[normalize(language)].get(key) or TEXTS["en"][key]
    return text.format(**kwargs) if kwargs else text
