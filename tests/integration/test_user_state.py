"""The `users` row's new invariants: access state, and the zone that must exist
by the time it matters.

`tz` is nullable now, because a person who has just typed /start genuinely has
no timezone and the column's own comment bans inventing one for them. That
nullability is only safe while two things hold: the database refuses to let a
row reach `active` without a zone, and the code reads the zone through a
property that fails loudly rather than handing `None` to `ZoneInfo`. Both are
asserted here.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from umai.db.models import UserStatus


async def test_a_new_row_starts_pending(session, build_user):
    """The server default, not something the application remembers to set. A
    row that reached the table by any other route is still locked out."""
    u = build_user(status=None)
    u.status = None
    session.add(u)
    await session.flush()
    await session.refresh(u)
    assert u.status == UserStatus.pending


async def test_an_active_user_cannot_exist_without_a_zone(session, build_user):
    """The CHECK. Without it a half-onboarded row could reach `day_bounds` and
    produce totals for a day that does not exist."""
    session.add(build_user(status=UserStatus.active, tz=None))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_a_pending_user_may_have_no_zone(session, build_user):
    """The other half of the same rule: the state the gate creates is legal."""
    u = build_user(status=UserStatus.pending, tz=None)
    session.add(u)
    await session.flush()
    assert u.tz is None


async def test_reading_the_zone_of_a_user_without_one_fails_loudly(session, build_user):
    """The property exists so this is an exception naming the user, rather than
    ZoneInfo being handed the string "None" somewhere three frames down."""
    u = build_user(status=UserStatus.onboarding, tz=None)
    session.add(u)
    await session.flush()
    with pytest.raises(RuntimeError, match="no timezone"):
        _ = u.zone


async def test_the_zone_property_is_the_column_when_it_is_set(session, user):
    assert user.zone == user.tz == "Europe/Istanbul"


async def test_health_tokens_are_unique_across_users(session, build_user):
    """Two users sharing a token would make the ingest endpoint's lookup
    ambiguous, which is the one thing it cannot be: it decides whose health
    series a payload is written into."""
    session.add(build_user(health_token="the-same-token"))
    await session.flush()
    session.add(build_user(health_token="the-same-token"))
    with pytest.raises(IntegrityError):
        await session.flush()


@pytest.mark.parametrize(("hour", "minute"), [(24, 0), (-1, 0), (21, 60), (21, -1)])
async def test_an_impossible_summary_time_is_refused(session, build_user, hour, minute):
    session.add(build_user(summary_hour=hour, summary_minute=minute))
    with pytest.raises(IntegrityError):
        await session.flush()
