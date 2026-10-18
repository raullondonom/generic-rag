"""Shared fixtures.

Tests that need Postgres use TEST_DATABASE_URL and a temporary schema dropped at the end; without
that variable they are skipped. With docker compose:

    docker compose run --rm -e TEST_DATABASE_URL=postgresql://rag:<password>@postgres:5432/rag rag-app pytest -q
"""

import os
import uuid

import pytest

from rag.config import Settings


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    """Tests must not depend on the .env of whoever runs them (compose passes it to the container)."""
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)


@pytest.fixture
def db():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set")
    import psycopg

    from rag.db import Database

    schema = f"test_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(url, autocommit=True) as c:
        c.execute(f'CREATE SCHEMA "{schema}"')
    sep = "&" if "?" in url else "?"
    database = Database(f"{url}{sep}options=-csearch_path%3D{schema}", max_size=2)
    database.migrate()
    try:
        yield database
    finally:
        database.close()
        with psycopg.connect(url, autocommit=True) as c:
            c.execute(f'DROP SCHEMA "{schema}" CASCADE')
