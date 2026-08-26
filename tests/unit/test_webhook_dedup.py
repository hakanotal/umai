"""The webhook acknowledges before it works, and never works twice.

The bug these guard against cost real data. `telegram_webhook` awaited
`feed_update` inline, so the HTTP response was held open for the whole handler
— 17-90 seconds for a photo, against a Telegram webhook timeout far shorter
than that. Telegram hung up (the edge logged 499 at ~20s and ~60s) and
redelivered the same update, and each redelivery ran the pipeline again. On
2026-08-26 one bowl of cacik became three live `log_entries` of 107, 119 and
107 kcal — disagreeing because perception ran three times — all three counted.

Two independent properties, so two sets of tests: the response does not wait
for the handler, and a repeated `update_id` is not work.
"""

from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from umai.web.api import SeenUpdates, create_app

SECRET = "s3cret"


class FakeDispatcher:
    """Records what it was fed, and can be made arbitrarily slow."""

    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay
        self.fed: list[int] = []
        self.started = asyncio.Event()
        self.finished = asyncio.Event()

    async def feed_update(self, bot, update):
        self.started.set()
        if self.delay:
            await asyncio.sleep(self.delay)
        self.fed.append(update.update_id)
        self.finished.set()


def _app(settings, dispatcher):
    return create_app(settings, dispatcher=dispatcher, bot=object())


def _update(update_id: int) -> dict:
    """The smallest thing aiogram will parse as an Update."""
    return {
        "update_id": update_id,
        "message": {
            "message_id": 1,
            "date": 1756000000,
            "chat": {"id": 1, "type": "private"},
            "from": {"id": 1, "is_bot": False, "first_name": "x"},
            "text": "hello",
        },
    }


@pytest.fixture
def settings(monkeypatch):
    from umai.config.settings import Settings

    s = Settings(telegram_bot_token="x", telegram_webhook_secret=SECRET)
    return s


async def _post(client, update, secret=SECRET):
    return await client.post(
        "/webhook/telegram",
        json=update,
        headers={"X-Telegram-Bot-Api-Secret-Token": secret},
    )


async def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# --- the acknowledgement does not wait --------------------------------------


async def test_the_response_does_not_wait_for_the_handler(settings):
    """The fix itself.

    A handler slower than Telegram's timeout must not delay the 200. Half a
    second here stands in for the 17-90s a photo really takes; the assertion is
    that the response arrives while the handler is still running.
    """
    dispatcher = FakeDispatcher(delay=0.5)
    app = _app(settings, dispatcher)

    async with await _client(app) as client:
        response = await asyncio.wait_for(_post(client, _update(1)), timeout=0.2)

        assert response.status_code == 200
        # Not merely unfinished — not yet started. `create_task` schedules;
        # the response is written before the loop ever runs the handler.
        assert dispatcher.fed == [], "the handler had finished before the ack was sent"
        assert not dispatcher.started.is_set()
        # Waited on rather than slept past: a fixed sleep turns any future
        # loop-blocking regression into a flake instead of a failure.
        await asyncio.wait_for(dispatcher.finished.wait(), timeout=5)
        assert dispatcher.fed == [1], "the update was acknowledged but never processed"


async def test_the_update_is_still_processed_after_the_ack(settings):
    dispatcher = FakeDispatcher()
    app = _app(settings, dispatcher)

    async with await _client(app) as client:
        await _post(client, _update(7))
        await asyncio.wait_for(dispatcher.finished.wait(), timeout=5)

    assert dispatcher.fed == [7]


async def test_a_failing_handler_does_not_break_the_endpoint(settings):
    """Nothing awaits the task, so an escaping exception would otherwise appear
    only as asyncio's "task exception was never retrieved", detached from the
    update that caused it."""

    class Exploding(FakeDispatcher):
        async def feed_update(self, bot, update):
            raise RuntimeError("handler blew up")

    app = _app(settings, Exploding())
    async with await _client(app) as client:
        response = await _post(client, _update(1))
    assert response.status_code == 200
    await asyncio.sleep(0)


# --- and it is not done twice -----------------------------------------------


async def test_a_redelivered_update_is_ignored(settings):
    """What Telegram actually did: the same update_id, three times."""
    dispatcher = FakeDispatcher()
    app = _app(settings, dispatcher)

    async with await _client(app) as client:
        for _ in range(3):
            assert (await _post(client, _update(42))).status_code == 200
        await asyncio.wait_for(dispatcher.finished.wait(), timeout=5)

    assert dispatcher.fed == [42], "a redelivery was processed a second time"


async def test_distinct_updates_all_run(settings):
    """The dedup must not swallow ordinary traffic."""
    dispatcher = FakeDispatcher()
    app = _app(settings, dispatcher)

    async with await _client(app) as client:
        for update_id in (1, 2, 3):
            await _post(client, _update(update_id))
        await asyncio.sleep(0.05)

    assert sorted(dispatcher.fed) == [1, 2, 3]


async def test_two_apps_do_not_share_a_dedup_registry(settings):
    """The registry hangs off `app.state`, not the module.

    A module-level one would have the first app's ids suppress the second's,
    which in the tests reads as a handler that mysteriously never runs.
    """
    first, second = FakeDispatcher(), FakeDispatcher()
    async with await _client(_app(settings, first)) as c1:
        await _post(c1, _update(99))
    async with await _client(_app(settings, second)) as c2:
        await _post(c2, _update(99))
    await asyncio.sleep(0.05)
    assert first.fed == [99]
    assert second.fed == [99]


# --- the bad secret still wins ----------------------------------------------


async def test_a_bad_secret_is_rejected_and_nothing_is_spawned(settings):
    """The auth check has to stay in front of the spawn. Behind it, an
    unauthenticated caller would get a 200 and a task."""
    dispatcher = FakeDispatcher()
    app = _app(settings, dispatcher)

    async with await _client(app) as client:
        response = await _post(client, _update(1), secret="wrong")

    assert response.status_code == 401
    await asyncio.sleep(0.05)
    assert dispatcher.fed == []


# --- the registry itself ----------------------------------------------------


def test_seen_updates_reports_first_sighting_then_repeats():
    seen = SeenUpdates()
    assert seen.add(1) is True
    assert seen.add(1) is False
    assert seen.add(2) is True


def test_seen_updates_is_bounded_and_forgets_the_oldest_first():
    """Unbounded, this is a slow leak on a process that runs for months."""
    seen = SeenUpdates(maxlen=3)
    for i in range(3):
        assert seen.add(i) is True
    assert seen.add(1) is False  # still remembered
    assert seen.add(99) is True  # evicts id 0
    assert seen.add(0) is True, "the oldest id should have been forgotten"
    assert seen.add(99) is False, "the newest id should still be remembered"
