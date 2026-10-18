"""Look of the end-user UI for a document collection: name, welcome text, suggested questions, colors.

It lives with the collection, in the control folder (`_control/ui.json`, or the file named by
`ui_file` in control_config.json), so every corpus brings its own identity. Anything missing falls
back to the generic texts of the configured language.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from .control import ControlFolder
from .i18n import normalize

log = logging.getLogger(__name__)

HEX_COLOR = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
ICON_TYPES = {".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
              ".webp": "image/webp"}
COLOR_KEYS = ("accent", "accent_text", "accent_dark", "accent_text_dark")
FIELD_ID = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
MAX_FIELDS = 4
MAX_OPTIONS = 500

DEFAULTS = {
    "en": {
        "name": "Document RAG",
        "short_name": "RAG",
        "tagline": "Answers only from your documents, with their sources.",
        "welcome_title": "What would you like to look up?",
        "welcome_text": "Answers are based only on the indexed documents and cite their sources.",
        "placeholder": "Type your question…",
        "suggestions": [],
        "disclaimer": "",
    },
    "es": {
        "name": "RAG Documental",
        "short_name": "RAG",
        "tagline": "Responde solo con tus documentos, citando sus fuentes.",
        "welcome_title": "¿Qué quieres consultar?",
        "welcome_text": "Las respuestas se basan únicamente en los documentos indexados y citan sus fuentes.",
        "placeholder": "Escribe tu pregunta…",
        "suggestions": [],
        "disclaimer": "",
    },
}


def _ui_file(folder: ControlFolder) -> Path | None:
    name = folder.config.get("ui_file", "ui.json")
    path = (folder.path / name).resolve()
    if not path.is_relative_to(folder.path.resolve()) or not path.is_file():
        return None
    return path


def load(docs_path: str, control_dir: str, language: str) -> dict:
    """Branding for the UI: defaults of the language, overridden by the collection's ui.json."""
    lang = normalize(language)
    out = {**DEFAULTS[lang], "colors": {}, "icon": False, "language": lang, "context_fields": []}
    folder = ControlFolder.load(docs_path, control_dir)
    path = _ui_file(folder) if folder else None
    if path is None:
        return out
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        log.warning("Could not read %s: %s", path, e)
        return out
    for key in DEFAULTS["en"]:
        value = data.get(key)
        if key == "suggestions":
            if isinstance(value, list):
                out[key] = [str(s).strip()[:200] for s in value if str(s).strip()][:8]
        elif isinstance(value, str) and value.strip():
            out[key] = value.strip()[:500]
    colors = data.get("colors") or {}
    out["colors"] = {k: colors[k] for k in COLOR_KEYS if isinstance(colors.get(k), str) and HEX_COLOR.match(colors[k])}
    out["context_fields"] = context_fields(data.get("context_fields"), folder.index_fields)
    out["icon"] = icon_path(docs_path, control_dir) is not None
    return out


def _texts(value, limit: int, max_len: int = 120) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for v in value:
        v = str(v).strip()[:max_len] if isinstance(v, (str, int, float)) else ""
        if v and v not in out:
            out.append(v)
    return out[:limit]


def context_fields(value, index_fields: tuple[str, ...] = ()) -> list[dict]:
    """Selectors the user can fill in before asking (the collection decides which ones), from ui.json.

    Each one: `id`, `label`, optional `placeholder` (the "any" choice), and either fixed `options` or
    `field`: a registry column listed in `index_fields` of control_config.json, whose values in the index
    become the options (minus `exclude_options`). With `field` and `boost: true`, an extra search
    restricted to the chosen value brings its documents up without hiding the rest. The choice always
    reaches the AI as context.
    """
    fields: list[dict] = []
    for f in value if isinstance(value, list) else []:
        if not isinstance(f, dict):
            continue
        fid, label = f.get("id"), f.get("label")
        if not (isinstance(fid, str) and FIELD_ID.match(fid) and isinstance(label, str) and label.strip()):
            continue
        if any(x["id"] == fid for x in fields):
            continue
        field = f.get("field") if f.get("field") in index_fields else None
        options = _texts(f.get("options"), MAX_OPTIONS)
        if not options and not field:
            continue
        placeholder = f.get("placeholder")
        fields.append({
            "id": fid,
            "label": label.strip()[:60],
            "placeholder": placeholder.strip()[:60] if isinstance(placeholder, str) else "",
            "options": options,
            "field": field,
            "boost": bool(field and f.get("boost")),
            "exclude_options": _texts(f.get("exclude_options"), MAX_OPTIONS),
        })
    return fields[:MAX_FIELDS]


def with_options(fields: list[dict], field_values) -> list[dict]:
    """The selectors with their options: fixed, or `field_values(field)` (the indexed values)."""
    out = []
    for f in fields:
        options = f["options"] or field_values(f["field"])
        excluded = {x.casefold() for x in f["exclude_options"]}
        options = [o for o in options if o.casefold() not in excluded]
        if options:
            out.append({**f, "options": options})
    return out


def resolve_context(fields: list[dict], sent: dict[str, str]) -> list[dict]:
    """The user's choices as context for the search and the answer. Only values the selectors offer
    are accepted (the rest is ignored); `field` is set when the value prioritizes the search."""
    out = []
    for f in fields:
        value = (sent.get(f["id"]) or "").strip()
        if value and value in f["options"]:
            out.append({"id": f["id"], "label": f["label"], "value": value,
                        "field": f["field"] if f["boost"] else None})
    return out


def icon_path(docs_path: str, control_dir: str) -> Path | None:
    """The collection's icon file (ui.json "icon", inside the control folder), if any."""
    folder = ControlFolder.load(docs_path, control_dir)
    path = _ui_file(folder) if folder else None
    if path is None:
        return None
    try:
        name = json.loads(path.read_text(encoding="utf-8")).get("icon")
    except (OSError, ValueError):
        return None
    if not isinstance(name, str) or not name:
        return None
    icon = (folder.path / name).resolve()
    if not icon.is_relative_to(folder.path.resolve()) or not icon.is_file() or icon.suffix.lower() not in ICON_TYPES:
        return None
    return icon


def manifest(brand: dict, icon_url: str) -> dict:
    icon_type = ICON_TYPES.get(Path(icon_url).suffix.lower(), "image/svg+xml") if brand["icon"] else "image/svg+xml"
    return {
        "name": brand["name"],
        "short_name": brand["short_name"],
        "description": brand["tagline"],
        "start_url": "/",
        "display": "standalone",
        "background_color": "#f6f7f9",
        "theme_color": brand["colors"].get("accent", "#2f6fde"),
        "lang": brand["language"],
        "icons": [{"src": icon_url, "sizes": "any", "type": icon_type, "purpose": "any maskable"}],
    }
