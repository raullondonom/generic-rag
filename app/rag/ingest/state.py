"""Estado de la ingesta en SQLite: qué documentos hay indexados, su versión y errores."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    doc_id      TEXT PRIMARY KEY,
    source      TEXT NOT NULL,          -- folder | confluence
    title       TEXT NOT NULL,
    location    TEXT NOT NULL,          -- ruta relativa o URL
    version     TEXT NOT NULL,          -- hash del archivo o versión de Confluence
    status      TEXT NOT NULL,          -- indexed | unsupported | error | empty
    chunks      INTEGER NOT NULL DEFAULT 0,
    error       TEXT,
    container   TEXT,                   -- espacio de Confluence o carpeta
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_documents_source ON documents(source);
CREATE TABLE IF NOT EXISTS sync_runs (
    source      TEXT PRIMARY KEY,
    started_at  REAL,
    finished_at REAL,
    status      TEXT,
    message     TEXT
);
"""


class StateStore:
    def __init__(self, state_dir: str):
        Path(state_dir).mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(Path(state_dir) / "state.db", check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)

    def get(self, doc_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM documents WHERE doc_id = ?", (doc_id,)).fetchone()
        return dict(row) if row else None

    def ids_for_source(self, source: str, container: str | None = None) -> set[str]:
        sql, args = "SELECT doc_id FROM documents WHERE source = ?", [source]
        if container is not None:
            sql += " AND container = ?"
            args.append(container)
        with self._lock:
            return {r[0] for r in self._conn.execute(sql, args)}

    def upsert(self, doc_id: str, *, source: str, title: str, location: str, version: str,
               status: str, chunks: int = 0, error: str | None = None, container: str | None = None) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO documents (doc_id, source, title, location, version, status, chunks, error, container, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(doc_id) DO UPDATE SET
                     source=excluded.source, title=excluded.title, location=excluded.location,
                     version=excluded.version, status=excluded.status, chunks=excluded.chunks,
                     error=excluded.error, container=excluded.container, updated_at=excluded.updated_at""",
                (doc_id, source, title, location, version, status, chunks, error, container, time.time()),
            )

    def delete(self, doc_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM documents WHERE doc_id = ?", (doc_id,))

    def clear_source(self, source: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM documents WHERE source = ?", (source,))

    def list(self, source: str | None = None, status: str | None = None, limit: int = 500) -> list[dict]:
        sql, args = "SELECT * FROM documents WHERE 1=1", []
        if source:
            sql += " AND source = ?"
            args.append(source)
        if status:
            sql += " AND status = ?"
            args.append(status)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, args)]

    def summary(self) -> dict:
        with self._lock:
            rows = self._conn.execute(
                "SELECT source, status, COUNT(*) AS docs, COALESCE(SUM(chunks), 0) AS chunks "
                "FROM documents GROUP BY source, status"
            ).fetchall()
            runs = self._conn.execute("SELECT * FROM sync_runs").fetchall()
        out: dict = {}
        for r in rows:
            src = out.setdefault(r["source"], {"docs": {}, "chunks": 0})
            src["docs"][r["status"]] = r["docs"]
            src["chunks"] += r["chunks"]
        for run in runs:
            out.setdefault(run["source"], {"docs": {}, "chunks": 0})["last_sync"] = dict(run)
        return out

    def run_started(self, source: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO sync_runs (source, started_at, finished_at, status, message) VALUES (?, ?, NULL, 'running', NULL)
                   ON CONFLICT(source) DO UPDATE SET started_at=excluded.started_at, status='running', message=NULL""",
                (source, time.time()),
            )

    def run_finished(self, source: str, status: str, message: str = "") -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE sync_runs SET finished_at = ?, status = ?, message = ? WHERE source = ?",
                (time.time(), status, message, source),
            )
