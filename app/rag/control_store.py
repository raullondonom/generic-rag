"""Postgres storage of the source control review: remote fingerprints and the result of each run."""

from __future__ import annotations

import logging
import threading

from psycopg.types.json import Jsonb

from .control import ControlFolder, RemoteChecker, review
from .db import Database

log = logging.getLogger(__name__)


class ControlStore:
    def __init__(self, db: Database):
        self.db = db
        self.lock = threading.Lock()

    def remote_state(self) -> dict[str, dict]:
        with self.db.conn() as c:
            return {r["url"]: r["state"] for r in c.execute("SELECT url, state FROM control_remote_state")}

    def save_remote_state(self, state: dict[str, dict]) -> None:
        with self.db.transaction() as c:
            for url, value in state.items():
                c.execute(
                    """INSERT INTO control_remote_state (url, state) VALUES (%s, %s)
                       ON CONFLICT (url) DO UPDATE SET state = excluded.state, updated_at = now()""",
                    (url, Jsonb(value)),
                )

    def last_run(self) -> dict | None:
        with self.db.conn() as c:
            row = c.execute("SELECT * FROM control_runs ORDER BY id DESC LIMIT 1").fetchone()
        if row is None:
            return None
        return {**row, "started_at": row["started_at"].timestamp(),
                "finished_at": row["finished_at"].timestamp() if row["finished_at"] else None}

    def run(self, folder: ControlFolder, online: bool, workers: int = 6,
            checker: RemoteChecker | None = None) -> dict:
        """Runs the review and stores it. Only reports: it never downloads documents nor reindexes."""
        if not self.lock.acquire(blocking=False):
            raise RuntimeError("a source control review is already running")
        try:
            with self.db.conn() as c:
                run_id = c.execute("INSERT INTO control_runs (online, status) VALUES (%s, 'running') RETURNING id",
                                   (online,)).fetchone()["id"]
            try:
                result = review(folder, self.remote_state() if online else None, online=online,
                                checker=checker, workers=workers)
                if online:
                    self.save_remote_state(result["remote_state"])
                with self.db.conn() as c:
                    c.execute(
                        """UPDATE control_runs SET finished_at = now(), status = 'ok', registries = %s, records = %s,
                                  counts = %s, findings = %s WHERE id = %s""",
                        (result["registries"], result["records"], Jsonb(result["counts"]),
                         Jsonb(result["findings"]), run_id),
                    )
                log.info("Source control review: %s", result["counts"])
                return result
            except Exception as e:
                with self.db.conn() as c:
                    c.execute("UPDATE control_runs SET finished_at = now(), status = 'error', error = %s WHERE id = %s",
                              (f"{type(e).__name__}: {e}"[:500], run_id))
                raise
        finally:
            self.lock.release()
