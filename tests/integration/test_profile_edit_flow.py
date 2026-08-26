"""Editing a settled profile, against a real database.

The unit tests hold the editor's contract with `core.onboarding`. This file
holds the two things only a database can answer: that an edit survives a commit
and a reload — the exact failure `test_onboarding_flow.py` exists for, where a
wizard step wrote to something that was not a mapped column — and that a
changed field moves the number it is supposed to move.
"""

from __future__ import annotations

import datetime as dt

import pytest

from umai.clock import FakeClock
from umai.core import onboarding as ob
from umai.core import tools
from umai.db.models import EntrySource, User
from umai.telegram.handlers.profile import apply_edit


@pytest.fixture
def clock() -> FakeClock:
    """A fixed instant. Nothing here is time-dependent beyond the age the birth
    date implies, and pinning it keeps that age from drifting on a birthday."""
    return FakeClock(dt.datetime(2026, 8, 26, 9, 0, tzinfo=dt.UTC))


async def _edit(session, user, field: str, raw: str, clock):
    """Parse a typed answer the way the handler does, then write it."""
    step = ob.step_for(field)
    assert step is not None, field
    result = step.parse(raw)
    assert isinstance(result, ob.Ok), f"{field} rejected {raw!r}: {result}"
    # A city resolving to several zones comes back as a list for the caller to
    # offer as buttons; the handler writes whichever one is then tapped.
    value = result.value[0] if isinstance(result.value, list) else result.value
    return await apply_edit(session, user, step, value, clock)


async def _reload(session, user_id) -> User:
    """Commit, detach, and re-read — what the next handler invocation gets."""
    await session.commit()
    session.expunge_all()
    return await session.get(User, user_id)


@pytest.mark.parametrize(
    ("field", "raw", "expected"),
    [
        ("sex", "female", "female"),
        ("height_cm", "172", 172.0),
        ("height_cm", "1.72", 172.0),  # metres, converted on the way in
        ("birth_date", "1985-11-02", dt.date(1985, 11, 2)),
        ("goal_rate_kg_per_week", "-0.25", -0.25),
        ("tz", "Europe/London", "Europe/London"),
    ],
)
async def test_an_edit_survives_a_commit_and_reload(session, user, clock, field, raw, expected):
    """The regression guard, borrowed from the wizard's own.

    An edit that does not survive the round trip is not an edit: the menu would
    redraw from the reloaded row and show the old value, which reads as the tap
    having been ignored.
    """
    await _edit(session, user, field, raw, clock)
    reloaded = await _reload(session, user.id)
    assert getattr(reloaded, field) == expected


async def test_editing_the_goal_rate_moves_goal_type_with_it(session, user, clock):
    """`goal_type` is derived and never asked, so nothing else can repair it.

    A row left saying "lose" at +0.25 kg/wk disagrees with itself, and no
    question the wizard or the editor can ask would fix it.
    """
    assert user.goal_type == "lose"
    await _edit(session, user, "goal_rate_kg_per_week", "0.25", clock)
    reloaded = await _reload(session, user.id)
    assert reloaded.goal_rate_kg_per_week == 0.25
    assert reloaded.goal_type == "gain"


async def test_editing_height_moves_the_target_it_feeds(session, user, clock):
    """The point of the feature. Height is a Mifflin-St Jeor term, so a
    corrected height has to produce a different daily target — and the
    confirmation says so at the moment of the edit rather than tomorrow."""
    await tools.log_weight(
        session, user, kg=80.0, occurred_at=clock.now(), source=EntrySource.manual
    )
    await session.flush()
    before = tools.current_target(user, 80.0, clock).kcal_target

    text = await _edit(session, user, "height_cm", "195", clock)

    after = tools.current_target(user, 80.0, clock).kcal_target
    assert after != before
    assert f"{after:.0f} kcal" in text, text


async def test_the_confirmation_omits_the_target_when_there_is_no_weight(session, user, clock):
    """Every target is computed from a weight, and a fresh user has none until
    they weigh in. Reporting a target anyway would mean inventing the weight."""
    text = await _edit(session, user, "height_cm", "175", clock)
    assert text.strip() == "Height is now 175 cm."


async def test_a_rejected_answer_leaves_the_previous_value_standing(session, user, clock):
    """The editor writes only a parsed `Ok`.

    A cleared column here would be able to violate `ck_users_active_has_tz`,
    and short of that would leave an `active` user whose every summary raises
    out of `current_target`.
    """
    step = ob.step_for("height_cm")
    assert isinstance(step.parse("banana"), ob.Retry)
    reloaded = await _reload(session, user.id)
    assert reloaded.height_cm == 180.0


async def test_an_edit_touches_nobody_else(session, user, other_user, clock):
    """`apply_edit` writes through the row it is handed, and the handler loads
    that row by the principal's own id. The guard is here because every other
    per-user write path in this codebase has one."""
    await _edit(session, user, "height_cm", "165", clock)
    await session.commit()
    session.expunge_all()

    assert (await session.get(User, user.id)).height_cm == 165.0
    untouched = await session.get(User, other_user.id)
    assert untouched.height_cm == 180.0
    assert untouched.tz == "Europe/London"


async def test_every_editable_field_round_trips_through_its_own_parser(session, user, clock):
    """The whole menu at once, so a step added to the wizard cannot arrive with
    a parser and a column that disagree about what it stores."""
    answers = {
        "tz": "London",
        "sex": "male",
        "height_cm": "181",
        "birth_date": "1991-01-31",
        "goal_rate_kg_per_week": "0",
    }
    for step in ob.editable_steps():
        if step.kind == "cuisines":
            continue  # written by the toggle grid, which has its own tests
        await _edit(session, user, step.field, answers[step.field], clock)

    reloaded = await _reload(session, user.id)
    for step in ob.editable_steps():
        if step.kind == "cuisines":
            continue
        assert ob.describe(step, getattr(reloaded, step.field)) != "not set", step.field
