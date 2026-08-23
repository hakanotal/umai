"""The ingest endpoint decides whose health series a payload becomes.

That is the whole reason this file exists. The endpoint used to take one
deployment-wide bearer token and attribute every reading to
`select(User.id).limit(1)` — whichever row Postgres returned first. With one
user that is indistinguishable from correct; with two it writes one person's
steps and weight into the other's series, and the single token grants that
against either of them.

The tests go through the resolution function rather than an HTTP client,
because what is under test is the mapping from token to user and the shape of
the refusal, and `session_scope` inside the route would need the process-wide
engine that the test fixtures deliberately do not install.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from umai.core import tokens
from umai.db.models import UserStatus
from umai.web.api import _user_for_token


async def test_a_token_resolves_to_its_own_user(session, user, other_user):
    user.health_token = tokens.new_token()
    other_user.health_token = tokens.new_token()
    await session.flush()

    assert await _user_for_token(session, user.health_token) == user.id
    assert await _user_for_token(session, other_user.health_token) == other_user.id


async def test_two_users_tokens_do_not_cross(session, user, other_user):
    """The bug. One person's phone must not be able to write into another
    person's health series, and before this the only thing deciding that was
    row order."""
    user.health_token = tokens.new_token()
    other_user.health_token = tokens.new_token()
    await session.flush()

    resolved = await _user_for_token(session, other_user.health_token)
    assert resolved != user.id


@pytest.mark.parametrize("token", ["", "not-a-real-token", "x" * 43])
async def test_an_unknown_token_is_refused(session, user, token):
    user.health_token = tokens.new_token()
    await session.flush()
    with pytest.raises(HTTPException) as caught:
        await _user_for_token(session, token)
    assert caught.value.status_code == 401


async def test_a_blocked_users_token_stops_working(session, user):
    """Revoking access has to revoke the phone too. Blocking someone in chat
    while their Health Auto Export automation kept writing would be a strange
    kind of revocation."""
    user.health_token = tokens.new_token()
    user.status = UserStatus.blocked
    await session.flush()

    with pytest.raises(HTTPException):
        await _user_for_token(session, user.health_token)


async def test_an_onboarding_user_cannot_ingest_yet(session, onboarding_user):
    """No zone, so there is no local day to bucket the samples into."""
    onboarding_user.health_token = tokens.new_token()
    await session.flush()

    with pytest.raises(HTTPException):
        await _user_for_token(session, onboarding_user.health_token)


async def test_the_refusal_never_says_which_kind_it_is(session, user):
    """An endpoint that distinguishes "no such token" from "that user is not
    active" is an oracle for enumerating tokens."""
    user.health_token = tokens.new_token()
    user.status = UserStatus.blocked
    await session.flush()

    with pytest.raises(HTTPException) as inactive:
        await _user_for_token(session, user.health_token)
    with pytest.raises(HTTPException) as unknown:
        await _user_for_token(session, "no-such-token")

    assert inactive.value.status_code == unknown.value.status_code
    assert inactive.value.detail == unknown.value.detail


async def test_rotating_invalidates_the_previous_token(session, user):
    old = tokens.new_token()
    user.health_token = old
    await session.flush()
    assert await _user_for_token(session, old) == user.id

    user.health_token = tokens.new_token()
    await session.flush()
    with pytest.raises(HTTPException):
        await _user_for_token(session, old)
