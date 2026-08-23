"""The same photograph, sent by two people.

`media.sha256` was globally unique, and `_record_media` inserted with
`ON CONFLICT DO NOTHING` then re-selected by digest. So the second person to
photograph a tin of beans, a menu, or a plate of the same takeaway got back the
*first person's* media row — and `agent.log_photo` then stamped their entry id
onto a row belonging to somebody else's meal.

Two people photographing the same thing is not exotic. A household eats the
same dinner; a group of friends eats at the same restaurant; the same packet of
biscuits appears in two kitchens. The digest is content, and content collides.
"""

from __future__ import annotations

from sqlalchemy import select

from umai.db.models import Media
from umai.telegram.handlers.photo import _record_media


class _SamePhotoEveryTime:
    """Stands in for the file on disk. `_record_media` hashes the path's
    contents, so the test patches the hash rather than writing bytes."""

    digest = "b" * 64


async def _record(session, user_id, monkeypatch, digest=_SamePhotoEveryTime.digest):
    monkeypatch.setattr("umai.perception.images.sha256_of", lambda _p: digest, raising=False)
    monkeypatch.setattr("umai.telegram.handlers.photo.image_tools.sha256_of", lambda _p: digest)
    monkeypatch.setattr("umai.telegram.handlers.photo._taken_at_local", lambda _p, _tz: None)
    from pathlib import Path

    return await _record_media(session, user_id, Path("/tmp/whatever.jpg"), "Europe/Istanbul")


async def test_two_users_photographing_the_same_thing_get_their_own_rows(
    session, user, other_user, monkeypatch
):
    mine = await _record(session, user.id, monkeypatch)
    theirs = await _record(session, other_user.id, monkeypatch)

    assert mine != theirs

    rows = {
        m.user_id: m.id
        for m in (await session.execute(select(Media).where(Media.sha256 == "b" * 64))).scalars()
    }
    assert rows == {user.id: mine, other_user.id: theirs}


async def test_the_same_user_sending_it_twice_still_gets_one_row(session, user, monkeypatch):
    """The deduplication that made the digest unique in the first place is
    intact — it is now per person rather than per deployment."""
    first = await _record(session, user.id, monkeypatch)
    again = await _record(session, user.id, monkeypatch)
    assert first == again


async def test_a_media_row_always_has_an_owner(session, user, monkeypatch):
    """`user_id` is carried directly rather than reached through `entry_id`,
    because the row is written before the entry exists. An orphan with a null
    entry — of which the first live session left two — would otherwise belong
    to nobody, and could not be deleted with its owner."""
    media_id = await _record(session, user.id, monkeypatch)
    row = await session.get(Media, media_id)
    assert row.user_id == user.id
    assert row.entry_id is None


async def test_deleting_a_user_takes_their_media_with_them(session, user, monkeypatch):
    """The CASCADE. Data rights depend on it: `/delete_me` is a single DELETE
    and every per-user table has to follow."""
    media_id = await _record(session, user.id, monkeypatch)
    await session.delete(user)
    await session.flush()
    assert await session.get(Media, media_id) is None


async def test_a_different_photo_is_a_different_row(session, user, monkeypatch):
    a = await _record(session, user.id, monkeypatch, digest="a" * 64)
    b = await _record(session, user.id, monkeypatch, digest="c" * 64)
    assert a != b
