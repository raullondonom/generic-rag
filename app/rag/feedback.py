"""Optional ratings of answers (thumbs up / down, reason and comment), stored in Postgres.

The API only returns aggregated numbers. The detail (questions, answers and comments) is exported
from the command line by whoever operates the server:

    docker compose exec rag-app python -m rag.feedback --negative --days 30
    docker compose exec rag-app python -m rag.feedback --format csv > feedback.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import sys

from psycopg.types.json import Jsonb

from .conversations import parse_id
from .db import Database

REASONS = ("wrong", "incomplete", "not_found", "bad_sources", "other")


class FeedbackError(ValueError):
    pass


class FeedbackStore:
    def __init__(self, db: Database):
        self.db = db

    def set(self, conversation_id: str, message_id: int, rating: int, reason: str | None = None,
            comment: str | None = None) -> dict:
        """Creates or updates the rating of an assistant message of that conversation."""
        if rating not in (-1, 1):
            raise FeedbackError("rating must be 1 or -1")
        if reason is not None and reason not in REASONS:
            raise FeedbackError(f"reason must be one of: {', '.join(REASONS)}")
        comment = (comment or "").strip()[:2000] or None
        cid = parse_id(conversation_id)
        with self.db.transaction() as c:
            msg = c.execute(
                "SELECT id, content, sources, provider, model FROM messages "
                "WHERE id = %s AND conversation_id = %s AND role = 'assistant'",
                (message_id, cid),
            ).fetchone()
            if msg is None:
                raise LookupError("message not found in this conversation")
            question = c.execute(
                "SELECT content FROM messages WHERE conversation_id = %s AND role = 'user' AND id < %s "
                "ORDER BY id DESC LIMIT 1", (cid, message_id),
            ).fetchone()
            c.execute(
                """INSERT INTO feedback (message_id, rating, reason, comment, question, answer, sources,
                                         provider, model)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (message_id) DO UPDATE SET rating = excluded.rating, reason = excluded.reason,
                     comment = excluded.comment, updated_at = now()""",
                (message_id, rating, reason if rating < 0 else None, comment,
                 question["content"] if question else "", msg["content"], Jsonb(msg["sources"]),
                 msg["provider"], msg["model"]),
            )
        return {"message_id": message_id, "rating": rating, "reason": reason if rating < 0 else None,
                "comment": comment}

    def clear(self, conversation_id: str, message_id: int) -> bool:
        with self.db.conn() as c:
            return c.execute(
                """DELETE FROM feedback f USING messages m
                   WHERE f.message_id = m.id AND m.id = %s AND m.conversation_id = %s""",
                (message_id, parse_id(conversation_id)),
            ).rowcount > 0

    def summary(self, days: int = 30) -> dict:
        """Aggregated numbers only (no conversation content)."""
        with self.db.conn() as c:
            total = c.execute(
                """SELECT COUNT(*) FILTER (WHERE rating = 1) AS up, COUNT(*) FILTER (WHERE rating = -1) AS down
                   FROM feedback WHERE created_at >= now() - make_interval(days => %s)""", (days,),
            ).fetchone()
            by_model = c.execute(
                """SELECT COALESCE(provider, 'guardrail') AS provider, COALESCE(model, '') AS model,
                          COUNT(*) FILTER (WHERE rating = 1) AS up, COUNT(*) FILTER (WHERE rating = -1) AS down
                   FROM feedback WHERE created_at >= now() - make_interval(days => %s)
                   GROUP BY 1, 2 ORDER BY 1, 2""", (days,),
            ).fetchall()
            by_reason = c.execute(
                """SELECT COALESCE(reason, 'unspecified') AS reason, COUNT(*) AS n FROM feedback
                   WHERE rating = -1 AND created_at >= now() - make_interval(days => %s)
                   GROUP BY 1 ORDER BY n DESC""", (days,),
            ).fetchall()
        rated = total["up"] + total["down"]
        return {
            "days": days,
            "up": total["up"],
            "down": total["down"],
            "satisfaction": round(total["up"] / rated, 3) if rated else None,
            "by_model": by_model,
            "by_reason": {r["reason"]: r["n"] for r in by_reason},
        }

    def export(self, days: int = 30, negative_only: bool = False) -> list[dict]:
        sql = ("SELECT id, created_at, rating, reason, comment, provider, model, question, answer, sources "
               "FROM feedback WHERE created_at >= now() - make_interval(days => %s)")
        if negative_only:
            sql += " AND rating = -1"
        with self.db.conn() as c:
            rows = c.execute(sql + " ORDER BY created_at DESC", (days,)).fetchall()
        return [{**r, "created_at": r["created_at"].isoformat()} for r in rows]


def main() -> int:
    from .config import get_settings

    ap = argparse.ArgumentParser(description="Export the ratings of the answers")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--negative", action="store_true", help="only thumbs down")
    ap.add_argument("--format", choices=("json", "csv", "summary"), default="json")
    args = ap.parse_args()
    db = Database(get_settings().database_url, max_size=1)
    store = FeedbackStore(db)
    try:
        if args.format == "summary":
            print(json.dumps(store.summary(args.days), ensure_ascii=False, indent=2))
            return 0
        rows = store.export(args.days, args.negative)
        if args.format == "json":
            print(json.dumps(rows, ensure_ascii=False, indent=2))
        else:
            writer = csv.DictWriter(sys.stdout, fieldnames=list(rows[0]) if rows else ["id"])
            writer.writeheader()
            for r in rows:
                writer.writerow({**r, "sources": "; ".join(s.get("title", "") for s in r["sources"])})
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
