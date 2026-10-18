"""Postgres connection and schema. The schema is created (idempotently) at startup."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    doc_id      TEXT PRIMARY KEY,
    source      TEXT NOT NULL,          -- folder | confluence
    title       TEXT NOT NULL,
    location    TEXT NOT NULL,          -- relative path or URL
    version     TEXT NOT NULL,          -- file hash or Confluence version
    status      TEXT NOT NULL,          -- indexed | unsupported | error | empty
    chunks      INTEGER NOT NULL DEFAULT 0,
    redactions  INTEGER NOT NULL DEFAULT 0,
    error       TEXT,
    container   TEXT,                   -- parent folder or Confluence space
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_documents_source ON documents(source);

CREATE TABLE IF NOT EXISTS sync_runs (
    source      TEXT PRIMARY KEY,
    started_at  TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    status      TEXT,
    message     TEXT
);

-- Loose values, e.g. the Qdrant collection the current state was indexed into
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
    id          UUID PRIMARY KEY,
    title       TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_conversations_updated ON conversations(updated_at);

CREATE TABLE IF NOT EXISTS messages (
    id               BIGSERIAL PRIMARY KEY,
    conversation_id  UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role             TEXT NOT NULL,     -- user | assistant
    content          TEXT NOT NULL,
    sources          JSONB NOT NULL DEFAULT '[]',
    provider         TEXT,
    model            TEXT,
    error            TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_messages_conversation ON messages(conversation_id, id);

-- Every call to Claude (simulated DRY_RUN calls too, at zero cost)
CREATE TABLE IF NOT EXISTS spend (
    id             BIGSERIAL PRIMARY KEY,
    ts             TIMESTAMPTZ NOT NULL DEFAULT now(),
    model          TEXT NOT NULL,
    purpose        TEXT NOT NULL,
    input_tokens   INTEGER NOT NULL DEFAULT 0,
    output_tokens  INTEGER NOT NULL DEFAULT 0,
    cache_write    INTEGER NOT NULL DEFAULT 0,
    cache_read     INTEGER NOT NULL DEFAULT 0,
    cost_usd       DOUBLE PRECISION NOT NULL DEFAULT 0,
    simulated      BOOLEAN NOT NULL DEFAULT false
);
CREATE INDEX IF NOT EXISTS ix_spend_ts ON spend(ts);

-- Optional user ratings of answers. Question, answer and sources are copied so the feedback
-- survives when the conversation is deleted or expires.
CREATE TABLE IF NOT EXISTS feedback (
    id          BIGSERIAL PRIMARY KEY,
    message_id  BIGINT UNIQUE REFERENCES messages(id) ON DELETE SET NULL,
    rating      SMALLINT NOT NULL CHECK (rating IN (-1, 1)),
    reason      TEXT,               -- wrong | incomplete | not_found | bad_sources | other
    comment     TEXT,
    question    TEXT NOT NULL,
    answer      TEXT NOT NULL,
    sources     JSONB NOT NULL DEFAULT '[]',
    provider    TEXT,
    model       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_feedback_created ON feedback(created_at);

-- Source control review (the documents folder is read only, so its state lives here)
CREATE TABLE IF NOT EXISTS control_remote_state (
    url         TEXT PRIMARY KEY,
    state       JSONB NOT NULL,         -- fingerprint, content_type, etag, last_modified, checked_at, changed_at
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS control_runs (
    id          BIGSERIAL PRIMARY KEY,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    online      BOOLEAN NOT NULL,
    status      TEXT NOT NULL,          -- running | ok | error
    registries  INTEGER,
    records     INTEGER,
    counts      JSONB,
    findings    JSONB,
    error       TEXT
);
"""


class Database:
    def __init__(self, url: str, min_size: int = 1, max_size: int = 8):
        self.pool = ConnectionPool(url, min_size=min_size, max_size=max_size, open=True,
                                   kwargs={"row_factory": dict_row, "autocommit": True})

    def migrate(self) -> None:
        with self.conn() as c:
            c.execute(SCHEMA)

    @contextmanager
    def conn(self) -> Iterator[psycopg.Connection]:
        with self.pool.connection() as c:
            yield c

    @contextmanager
    def transaction(self) -> Iterator[psycopg.Connection]:
        with self.pool.connection() as c, c.transaction():
            yield c

    def get_setting(self, key: str) -> str | None:
        with self.conn() as c:
            row = c.execute("SELECT value FROM settings WHERE key = %s", (key,)).fetchone()
        return row["value"] if row else None

    def set_setting(self, key: str, value: str) -> None:
        with self.conn() as c:
            c.execute("INSERT INTO settings (key, value) VALUES (%s, %s) "
                      "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, value))

    def ping(self) -> None:
        with self.conn() as c:
            c.execute("SELECT 1")

    def close(self) -> None:
        self.pool.close()
