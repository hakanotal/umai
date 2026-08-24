"""The wizard against a real database, one session per answer.

The unit tests in `tests/unit/test_onboarding.py` drive `next_step` against a
`SimpleNamespace`, where any attribute you set stays set. That is exactly what
hid the bug this file exists for: the weight question wrote to an attribute
that was not a mapped column, SQLAlchemy discarded it on commit, and the next
message asked the same question again — every new user trapped on question five,
forever.

So every test here commits and reloads between answers, the way the handler
does: it writes in one `session_scope`, and `_advance` opens another to decide
what to ask next. An answer that does not survive that round trip is not an
answer.
"""

from __future__ import annotations

import datetime as dt

import pytest

from umai.core import onboarding as ob
from umai.db.models import EntryKind, User, UserStatus

ANSWERS: dict[str, str] = {
    "tz": "istanbul",
    "sex": "female",
    "height_cm": "168",
    "birth_date": "1992-03-14",
    "onboarding_weight_kg": "62",
    "goal_rate_kg_per_week": "-0.25",
}


async def _answer(session, user_id, step, raw):
    """Parse, write, commit — then hand back a freshly loaded row."""
    result = step.parse(raw)
    assert isinstance(result, ob.Ok), f"{step.field} rejected {raw!r}: {result}"
    value = result.value[0] if isinstance(result.value, list) else result.value
    user = await session.get(User, user_id)
    setattr(user, step.field, value)
    if step.field == "goal_rate_kg_per_week":
        user.goal_type = ob.goal_type_for(float(value))
    await session.commit()
    # Detach, so the next `get` issues a real SELECT instead of handing back
    # the in-memory object. That is what the handler gets: `_advance` opens
    # its own session and reloads the row.
    session.expunge_all()
    return await session.get(User, user_id)


async def test_every_step_survives_a_commit_and_reload(session, onboarding_user):
    """The regression guard.

    Each answer is written, committed and re-read before the next question is
    chosen. A step whose field is not a real column loops here instead of
    advancing, which is what production did.
    """
    user = onboarding_user
    seen: list[str] = []

    for _ in range(len(ob.STEPS) + 3):  # +3: room to loop before failing
        step = ob.next_step(user)
        if step is None:
            break
        assert step.field not in seen, (
            f"the wizard asked for {step.field!r} twice — its answer did not "
            "survive the commit, so it is not a mapped column"
        )
        seen.append(step.field)
        if step.field == "cuisines":
            user.cuisines = ["turkish"]
            await session.commit()
            session.expunge_all()
            user = await session.get(User, user.id)
            continue
        user = await _answer(session, user.id, step, ANSWERS[step.field])

    assert seen == [s.field for s in ob.STEPS]
    assert ob.next_step(user) is None


async def test_every_step_writes_to_a_real_column():
    """The same guarantee, stated directly. Cheaper to read than the loop above
    and it fails at the point the mistake is made rather than at the symptom."""
    columns = set(User.__table__.columns.keys())
    missing = [s.field for s in ob.STEPS if s.field not in columns]
    assert missing == [], (
        f"{missing} are wizard steps with no column behind them. The wizard "
        "derives its position from the row, so an answer stored anywhere else "
        "is a question that can never be answered."
    )


async def test_a_restart_mid_wizard_resumes_where_it_left_off(session, onboarding_user):
    """The whole reason the wizard holds no FSM state.

    aiogram's default storage is in memory, so a restart would have dropped
    somebody into the free-text catch-all where "168" is a plausible meal.
    """
    user = onboarding_user
    for field in ("tz", "sex"):
        user = await _answer(session, user.id, ob.next_step(user), ANSWERS[field])

    session.expunge_all()  # stands in for the process dying
    reloaded = await session.get(User, user.id)

    assert ob.next_step(reloaded).field == "height_cm"
    assert reloaded.tz == "Europe/Istanbul"


async def test_an_already_active_user_is_owed_nothing(session, user):
    """The bug's other face: for a finished user `next_step` must return None,
    or anything that consults it thinks they are mid-wizard."""
    user.onboarding_weight_kg = 88.0
    await session.flush()
    assert ob.next_step(user) is None


async def test_a_rejected_answer_does_not_advance(session, onboarding_user):
    user = onboarding_user
    step = ob.next_step(user)
    assert step.field == "tz"
    assert isinstance(step.parse("not a real place"), ob.Retry)
    assert ob.next_step(user).field == "tz"


async def test_finishing_writes_the_weight_as_a_log_entry(session, onboarding_user):
    """The column records what they said; the weight *series* is a LogEntry,
    and the series is what trend and calibration read.

    Written once, at the end, so a wizard somebody abandons halfway leaves no
    stray reading in a trend that has no other points yet.
    """
    from sqlalchemy import func, select

    from umai.clock import FakeClock
    from umai.core import tools
    from umai.db.models import LogEntry
    from umai.telegram.handlers.onboarding import _finish

    user = onboarding_user
    while (step := ob.next_step(user)) is not None:
        if step.field == "cuisines":
            user.cuisines = ["turkish"]
            await session.commit()
            session.expunge_all()
            user = await session.get(User, user.id)
            continue
        user = await _answer(session, user.id, step, ANSWERS[step.field])

    async def weight_entries() -> int:
        return int(
            (
                await session.execute(
                    select(func.count(LogEntry.id)).where(
                        LogEntry.user_id == user.id, LogEntry.kind == EntryKind.weight
                    )
                )
            ).scalar_one()
        )

    assert await weight_entries() == 0, "nothing is written until the wizard completes"

    await _finish(session, user, FakeClock(dt.datetime(2026, 8, 23, 12, 0, tzinfo=dt.UTC)))
    await session.commit()

    assert user.status == UserStatus.active
    assert user.health_token, "the health-ingest token is minted at the end"
    assert await weight_entries() == 1
    assert await tools.latest_weight(session, user.id) == pytest.approx(62.0)
