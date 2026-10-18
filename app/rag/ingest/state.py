"""Ingestion state in Postgres: which documents are indexed, their version and errors."""

from __future__ import annotations

from ..db import Database

INDEX_COLLECTION_KEY = "index_collection"


class StateStore:
    def __init__(self, db: Database):
        self.db = db

    def get(self, doc_id: str) -> dict | None:
        with self.db.conn() as c:
            return c.execute("SELECT * FROM documents WHERE doc_id = %s", (doc_id,)).fetchone()

    def ids_for_source(self, source: str, container: str | None = None) -> set[str]:
        sql, args = "SELECT doc_id FROM documents WHERE source = %s", [source]
        if container is not None:
            sql += " AND container = %s"
            args.append(container)
        with self.db.conn() as c:
            return {r["doc_id"] for r in c.execute(sql, args)}

    def upsert(self, doc_id: str, *, source: str, title: str, location: str, version: str, status: str,
               chunks: int = 0, redactions: int = 0, error: str | None = None, container: str | None = None) -> None:
        with self.db.conn() as c:
            c.execute(
                """INSERT INTO documents (doc_id, source, title, location, version, status, chunks, redactions,
                                          error, container, updated_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                   ON CONFLICT (doc_id) DO UPDATE SET
                     source=excluded.source, title=excluded.title, location=excluded.location,
                     version=excluded.version, status=excluded.status, chunks=excluded.chunks,
                     redactions=excluded.redactions, error=excluded.error, container=excluded.container,
                     updated_at=excluded.updated_at""",
                (doc_id, source, title, location, version, status, chunks, redactions, error, container),
            )

    def delete(self, doc_id: str) -> None:
        with self.db.conn() as c:
            c.execute("DELETE FROM documents WHERE doc_id = %s", (doc_id,))

    def reset_for_collection(self, collection: str) -> bool:
        """If the state was indexed into another collection (another model), clear it to reindex everything.

        Returns True when the collection changed (also the first time).
        """
        previous = self.db.get_setting(INDEX_COLLECTION_KEY)
        if previous == collection:
            return False
        with self.db.transaction() as c:
            c.execute("DELETE FROM documents")
            c.execute("DELETE FROM sync_runs")
        self.db.set_setting(INDEX_COLLECTION_KEY, collection)
        return True

    def clear_documents(self) -> int:
        """Forgets what was indexed (the index is gone or empty): the next sync indexes everything again."""
        with self.db.transaction() as c:
            n = c.execute("DELETE FROM documents").rowcount
            c.execute("DELETE FROM sync_runs")
        return n

    def list(self, source: str | None = None, status: str | None = None, limit: int = 500) -> list[dict]:
        sql, args = "SELECT * FROM documents WHERE true", []
        if source:
            sql += " AND source = %s"
            args.append(source)
        if status:
            sql += " AND status = %s"
            args.append(status)
        sql += " ORDER BY updated_at DESC LIMIT %s"
        args.append(limit)
        with self.db.conn() as c:
            return list(c.execute(sql, args))

    def summary(self) -> dict:
        with self.db.conn() as c:
            rows = c.execute(
                "SELECT source, status, COUNT(*) AS docs, COALESCE(SUM(chunks), 0) AS chunks, "
                "COALESCE(SUM(redactions), 0) AS redactions FROM documents GROUP BY source, status"
            ).fetchall()
            runs = c.execute("SELECT * FROM sync_runs").fetchall()
        out: dict = {}
        for r in rows:
            src = out.setdefault(r["source"], {"docs": {}, "chunks": 0, "redactions": 0})
            src["docs"][r["status"]] = r["docs"]
            src["chunks"] += r["chunks"]
            src["redactions"] += r["redactions"]
        for run in runs:
            out.setdefault(run["source"], {"docs": {}, "chunks": 0, "redactions": 0})["last_sync"] = {
                "status": run["status"],
                "message": run["message"],
                "started_at": run["started_at"].timestamp() if run["started_at"] else None,
                "finished_at": run["finished_at"].timestamp() if run["finished_at"] else None,
            }
        return out

    def run_started(self, source: str) -> None:
        with self.db.conn() as c:
            c.execute(
                """INSERT INTO sync_runs (source, started_at, finished_at, status, message)
                   VALUES (%s, now(), NULL, 'running', NULL)
                   ON CONFLICT (source) DO UPDATE SET started_at=excluded.started_at, status='running', message=NULL""",
                (source,),
            )

    def run_finished(self, source: str, status: str, message: str = "") -> None:
        with self.db.conn() as c:
            c.execute("UPDATE sync_runs SET finished_at = now(), status = %s, message = %s WHERE source = %s",
                      (status, message, source))
