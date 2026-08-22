"""Shared fixtures.

Two things matter here.

The cassette fixture: tests never call a model API. RECORD=1 makes the real call
and saves it; every other run replays from tests/cassettes/. Cassettes are
committed and double as a regression suite, since re-recording after a prompt or
model change shows exactly how behaviour moved.

The database fixture: a real Postgres via testcontainers, never SQLite. Trigram
matching, native enums, array columns and ON CONFLICT semantics do not exist in
SQLite, and those are precisely the behaviours worth testing.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from umai.db.models import Base, User

CASSETTES = Path(__file__).parent / "cassettes"


# ---------------------------------------------------------------------------
# Model cassettes
# ---------------------------------------------------------------------------


@pytest.fixture
def cassette(request, monkeypatch):
    """Replay a recorded model response, or record one when RECORD=1.

    Keyed by test name, so a test that makes several calls replays them in
    order. A missing cassette is an explicit failure rather than a silent live
    call: an accidental network call in CI is exactly what this exists to stop.

    Both call shapes are captured into one ordered list, tagged with `kind`, so
    a test that interleaves them replays them in the order it made them. An
    entry with no `kind` is a plain `call`, which is what every cassette
    recorded before tool calling existed.
    """
    path = CASSETTES / f"{request.node.name}.json"

    if os.getenv("RECORD"):
        from umai.config.models import ModelClient

        recorded: list[dict] = []
        original = ModelClient.call
        original_tools = ModelClient.call_tools

        def recording(self, task, messages, schema=None, image_b64=None, **kw):
            content, served, latency = original(
                self, task, messages, schema=schema, image_b64=image_b64, **kw
            )
            recorded.append(
                {"task": task, "content": content, "model": served, "latency_ms": latency}
            )
            return content, served, latency

        def recording_tools(self, task, messages, tools, schema=None, **kw):
            message, served, latency = original_tools(
                self, task, messages, tools, schema=schema, **kw
            )
            recorded.append(
                {
                    "kind": "tools",
                    "task": task,
                    "content": message.content,
                    # Only the fields the loop reads. The SDK object carries
                    # provider-specific extras that are not worth freezing into
                    # a fixture.
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        }
                        for tc in (message.tool_calls or [])
                    ],
                    "model": served,
                    "latency_ms": latency,
                }
            )
            return message, served, latency

        monkeypatch.setattr(ModelClient, "call", recording)
        monkeypatch.setattr(ModelClient, "call_tools", recording_tools)
        yield
        CASSETTES.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(recorded, indent=2))
        return

    if not path.exists():
        pytest.skip(f"no cassette at {path}; record it with RECORD=1")

    calls = iter(json.loads(path.read_text()))

    def _next(expected: str) -> dict:
        try:
            entry = next(calls)
        except StopIteration:
            raise AssertionError(
                f"{request.node.name} made more model calls than the cassette holds"
            ) from None
        kind = entry.get("kind", "plain")
        if kind != expected:
            raise AssertionError(
                f"{request.node.name} made a {expected!r} call where the cassette holds {kind!r}"
            )
        return entry

    def replaying(self, task, messages, schema=None, image_b64=None, **kw):
        entry = _next("plain")
        return entry["content"], entry["model"], entry["latency_ms"]

    def replaying_tools(self, task, messages, tools, schema=None, **kw):
        entry = _next("tools")
        message = SimpleNamespace(
            content=entry["content"],
            tool_calls=[
                SimpleNamespace(
                    id=tc["id"],
                    function=SimpleNamespace(name=tc["name"], arguments=tc["arguments"]),
                )
                for tc in entry.get("tool_calls") or []
            ]
            or None,
        )
        return message, entry["model"], entry["latency_ms"]

    from umai.config.models import ModelClient

    monkeypatch.setattr(ModelClient, "call", replaying)
    monkeypatch.setattr(ModelClient, "call_tools", replaying_tools)
    yield


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


def _database_url() -> str | None:
    """Where to find Postgres for integration tests.

    UMAI_TEST_DATABASE_URL points at an already-running instance, which is what
    the local dev container provides and is far faster than starting a new one
    per session. Otherwise testcontainers starts one, and if Docker is not
    available the integration tests skip rather than fail.
    """
    return os.getenv("UMAI_TEST_DATABASE_URL")


@pytest.fixture(scope="session")
def db_url() -> str:
    """The database URL, starting a testcontainer if none is configured.

    Session-scoped and synchronous: a Docker container is not bound to an event
    loop, only an asyncpg connection is. The URL it produces is just a string
    that function-scoped engines connect to.
    """
    url = _database_url()
    if url is not None:
        return url

    try:
        from testcontainers.postgres import PostgresContainer
    except ImportError:  # pragma: no cover
        pytest.skip("testcontainers is not installed")
    try:
        container = PostgresContainer("pgvector/pgvector:pg17", driver="asyncpg")
        container.start()
    except Exception as exc:  # docker unavailable
        pytest.skip(f"no Postgres available for integration tests: {exc}")
    # Stopped at process exit; testcontainers does not offer a session-teardown
    # hook that is compatible with pytest-asyncio's loop management, and a
    # leaked container is a disk-space annoyance rather than a correctness bug.
    return container.get_connection_url()


@pytest.fixture(scope="session")
def _schema_ready(db_url: str) -> None:
    """Create the schema once, on a one-shot engine that is disposed before
    this fixture returns.

    No asyncpg connection outlives the call, so there is nothing to cross an
    event loop boundary. `create_all` is idempotent, so re-running tests against
    a persistent database is free.
    """

    async def _setup() -> None:
        engine = create_async_engine(db_url)
        async with engine.begin() as conn:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()

    asyncio.run(_setup())


@pytest_asyncio.fixture
async def engine(db_url: str, _schema_ready: None) -> AsyncIterator:
    """A fresh async engine per test.

    Function-scoped on purpose. pytest-asyncio's auto mode gives each test its
    own event loop, and an asyncpg connection is bound to the loop that created
    it. A session-scoped engine would create connections on the first test's
    loop and reuse them on every other test's loop, which asyncpg reports as
    "got result for unknown protocol state 3" or "Future attached to a different
    loop". A function-scoped engine avoids that entirely because its pool is
    created and disposed within the single loop the test runs on.
    """
    engine = create_async_engine(db_url)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def session(engine) -> AsyncIterator[AsyncSession]:
    """A session wrapped in a transaction that is always rolled back.

    Every test therefore starts from the same state without recreating the
    schema, and no test can leak rows into another.

    The session joins the outer transaction through a SAVEPOINT rather than
    beginning its own: asyncpg allows only one transaction on a connection at a
    time, so binding a session to a connection that already holds an open
    transaction fails with "another operation is in progress" and leaves the
    transaction deassociated. `join_transaction_mode="create_savepoint"` is what
    makes the two coexist -- the session's "commit" releases the savepoint while
    the outer transaction is rolled back regardless, so nothing persists.
    """
    factory = async_sessionmaker(
        engine, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    conn = await engine.connect()
    outer = await conn.begin()
    try:
        async with factory(bind=conn) as s:
            yield s
    finally:
        await outer.rollback()
        await conn.close()


@pytest_asyncio.fixture
async def user(session: AsyncSession) -> User:
    u = User(
        id=uuid.uuid4(),
        telegram_id=int(uuid.uuid4().int % 1_000_000_000),
        tz="Europe/Istanbul",
        sex="male",
        height_cm=180.0,
    )
    session.add(u)
    await session.flush()
    return u
