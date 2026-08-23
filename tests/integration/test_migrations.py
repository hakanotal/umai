"""The migrations and the models describe the same schema.

`conftest` builds the test schema with `Base.metadata.create_all`, which is the
right choice — it is fast, it needs no alembic, and it cannot half-apply. But it
means the migration chain is never exercised by the test suite, so a column
added to `db/models.py` without a revision passes every test and then fails on
deploy, where `alembic upgrade head` is the only thing that runs. That has
already happened once, to `users.water_target_ml`.

So this module does the two things `create_all` cannot: it runs the chain from
empty on a scratch database, and it asks alembic to autogenerate against the
result. An empty autogenerate diff is the assertion — it means every model
change has a revision behind it, and every revision lands where the model says.

The scratch database is created and dropped here rather than reusing the test
database, because `upgrade head` is a schema-wide operation and would fight the
`create_all` schema every other integration test depends on.

Everything here goes through asyncpg, the project's only Postgres driver. That
is why the synchronous-looking work — CREATE DATABASE, `compare_metadata` — is
wrapped in `asyncio.run` and `run_sync` rather than written against a plain
`create_engine`: adding psycopg2 as a dependency to run three tests would be a
poor trade.
"""

from __future__ import annotations

import asyncio
import uuid
from urllib.parse import urlparse, urlunparse

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy.ext.asyncio import create_async_engine

from umai.db.models import Base

# Differences alembic reports that are not drift. Server-side defaults are the
# usual false positive: SQLAlchemy renders `"false"` where Postgres reports
# back `false`, and comparing them is noise rather than signal. Nothing here
# suppresses a missing table, column, index or constraint.
IGNORED_DIFF_KINDS = {"modify_default", "modify_comment"}


def _with_database(url: str, name: str) -> str:
    parts = urlparse(url)
    return urlunparse(parts._replace(path=f"/{name}"))


def _run_autocommit(url: str, statements: list[tuple[str, dict]]) -> None:
    """Statements that cannot run inside a transaction — CREATE and DROP
    DATABASE are both in that category."""

    async def _go() -> None:
        engine = create_async_engine(url, isolation_level="AUTOCOMMIT")
        try:
            async with engine.connect() as conn:
                for sql, params in statements:
                    await conn.execute(sa.text(sql), params)
        finally:
            await engine.dispose()

    asyncio.run(_go())


@pytest.fixture
def scratch_db(db_url: str):
    """An empty database, dropped afterwards.

    Named per-run so a crashed previous run cannot collide with this one.
    """
    name = f"umai_mig_{uuid.uuid4().hex[:12]}"
    admin_url = _with_database(db_url, "postgres")
    try:
        _run_autocommit(admin_url, [(f'CREATE DATABASE "{name}"', {})])
    except Exception as exc:  # pragma: no cover - no permission, or no server
        pytest.skip(f"cannot create a scratch database: {exc}")

    yield _with_database(db_url, name)

    _run_autocommit(
        admin_url,
        [
            (
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = :n AND pid <> pg_backend_pid()",
                {"n": name},
            ),
            (f'DROP DATABASE IF EXISTS "{name}"', {}),
        ],
    )


def _alembic_config(url: str) -> Config:
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def test_the_chain_applies_to_an_empty_database(scratch_db, monkeypatch):
    """`alembic upgrade head` from nothing. This is what runs on deploy."""
    monkeypatch.setenv("DATABASE_URL", scratch_db)
    command.upgrade(_alembic_config(scratch_db), "head")

    async def _tables() -> set[str]:
        engine = create_async_engine(scratch_db)
        try:
            async with engine.connect() as conn:
                return set(await conn.run_sync(lambda c: sa.inspect(c).get_table_names()))
        finally:
            await engine.dispose()

    tables = asyncio.run(_tables())
    assert {"users", "log_entries", "foods", "job_runs", "media"} <= tables


def test_the_models_and_the_migrations_agree(scratch_db, monkeypatch):
    """The drift check.

    A non-empty diff here means someone edited `db/models.py` without writing a
    revision — the change works in the tests, which use `create_all`, and fails
    on the Pi, which uses alembic.
    """
    monkeypatch.setenv("DATABASE_URL", scratch_db)
    command.upgrade(_alembic_config(scratch_db), "head")

    def _diff(conn) -> list:
        return compare_metadata(MigrationContext.configure(conn), Base.metadata)

    async def _go() -> list:
        engine = create_async_engine(scratch_db)
        try:
            async with engine.connect() as conn:
                return await conn.run_sync(_diff)
        finally:
            await engine.dispose()

    diff = asyncio.run(_go())

    real = [d for d in diff if not (isinstance(d, tuple) and d[0] in IGNORED_DIFF_KINDS)]
    assert real == [], (
        "db/models.py and alembic/versions disagree. Generate a revision with "
        f'`just revision name="..."`. Differences: {real}'
    )


def test_the_head_revision_can_be_undone(scratch_db, monkeypatch):
    """Downgrade is not decoration: it is what a bad deploy is rolled back with,
    and a downgrade that has never been run is a downgrade that does not work."""
    monkeypatch.setenv("DATABASE_URL", scratch_db)
    cfg = _alembic_config(scratch_db)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "-1")
    command.upgrade(cfg, "head")
