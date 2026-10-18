"""Conversations stored in Postgres.

There are no users: the identifier (a random UUID) works as the key. The browser keeps the ids of
its conversations and the server only returns the ones it is asked for by id.
"""

from __future__ import annotations

import json
import uuid

from psycopg.types.json import Jsonb

from .db import Database


def parse_id(value: str | None) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value)) if value else None
    except ValueError:
        return None


class ConversationStore:
    def __init__(self, db: Database):
        self.db = db

    def create(self, title: str) -> str:
        cid = uuid.uuid4()
        with self.db.conn() as c:
            c.execute("INSERT INTO conversations (id, title) VALUES (%s, %s)", (cid, title[:120]))
        return str(cid)

    def exists(self, cid: str) -> bool:
        parsed = parse_id(cid)
        if parsed is None:
            return False
        with self.db.conn() as c:
            return c.execute("SELECT 1 FROM conversations WHERE id = %s", (parsed,)).fetchone() is not None

    def history(self, cid: str, limit: int) -> list[dict]:
        """Latest messages without errors, in chronological order, to give the AI context."""
        with self.db.conn() as c:
            rows = c.execute(
                """SELECT role, content FROM (
                       SELECT id, role, content FROM messages
                       WHERE conversation_id = %s AND error IS NULL ORDER BY id DESC LIMIT %s
                   ) t ORDER BY id""",
                (parse_id(cid), limit),
            ).fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in rows]

    def add_message(self, cid: str, role: str, content: str, *, sources: list | None = None,
                    provider: str | None = None, model: str | None = None, error: str | None = None) -> int:
        """Saves a message and returns its id."""
        parsed = parse_id(cid)
        with self.db.transaction() as c:
            row = c.execute(
                """INSERT INTO messages (conversation_id, role, content, sources, provider, model, error)
                   VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                (parsed, role, content, Jsonb(sources or []), provider, model, error),
            ).fetchone()
            c.execute("UPDATE conversations SET updated_at = now() WHERE id = %s", (parsed,))
        return row["id"]

    def get(self, cid: str) -> dict | None:
        parsed = parse_id(cid)
        if parsed is None:
            return None
        with self.db.conn() as c:
            conv = c.execute("SELECT id, title, created_at, updated_at FROM conversations WHERE id = %s",
                             (parsed,)).fetchone()
            if conv is None:
                return None
            msgs = c.execute(
                """SELECT m.id, m.role, m.content, m.sources, m.provider, m.model, m.error, m.created_at,
                          f.rating, f.reason, f.comment
                   FROM messages m LEFT JOIN feedback f ON f.message_id = m.id
                   WHERE m.conversation_id = %s ORDER BY m.id""", (parsed,)
            ).fetchall()
        return {**_summary(conv), "messages": [
            {**m, "sources": m["sources"] if isinstance(m["sources"], list) else json.loads(m["sources"]),
             "created_at": m["created_at"].timestamp()} for m in msgs
        ]}

    def lookup(self, ids: list[str]) -> list[dict]:
        parsed = [p for p in (parse_id(i) for i in ids) if p is not None]
        if not parsed:
            return []
        with self.db.conn() as c:
            rows = c.execute(
                """SELECT c.id, c.title, c.created_at, c.updated_at, COUNT(m.id) AS messages
                   FROM conversations c LEFT JOIN messages m ON m.conversation_id = c.id
                   WHERE c.id = ANY(%s) GROUP BY c.id ORDER BY c.updated_at DESC""",
                (parsed,),
            ).fetchall()
        return [{**_summary(r), "messages": r["messages"]} for r in rows]

    def delete(self, cid: str) -> bool:
        parsed = parse_id(cid)
        if parsed is None:
            return False
        with self.db.conn() as c:
            return c.execute("DELETE FROM conversations WHERE id = %s", (parsed,)).rowcount > 0

    def purge(self, days: int) -> int:
        if days <= 0:
            return 0
        with self.db.conn() as c:
            return c.execute("DELETE FROM conversations WHERE updated_at < now() - make_interval(days => %s)",
                             (days,)).rowcount


def _summary(row: dict) -> dict:
    return {
        "id": str(row["id"]),
        "title": row["title"],
        "created_at": row["created_at"].timestamp(),
        "updated_at": row["updated_at"].timestamp(),
    }
