"""Move the daily digest to 00:10 local, reporting the day that has just ended.

The digest used to land at 21:30, which is the middle of dinner for most of
the people it reports on — a summary of a day with two hours still to run.
Ten past midnight is the first quiet minute after the day is genuinely over,
so the figures it quotes are final.

Past midnight rather than just before it on purpose. A 23:59 due time has a
sixty-second window, and the tick runs every five minutes on an interval
anchored to process start, so four nights in five no tick would land inside it
and the digest would simply not arrive. A time in the small hours has the
whole night to be caught in.

Two things move together. The column defaults change, so a new user gets the
new time; and the existing rows are updated, but *only* those still sitting on
the old default. Somebody who has deliberately chosen 19:00 has chosen it, and
a migration that overwrites a setting because it also happens to be changing
the default is a migration that reaches past its own scope.

The scheduler side of this is not optional: `jobs.summary_due_day` is what
knows that a due time below `jobs.SMALL_HOURS` reports the previous local day.
Without it a 00:10 digest would summarise a day ten minutes old and find it
empty.

Revision ID: c8d3f5a1e07b
Revises: a91c4e02f5d8
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c8d3f5a1e07b"
down_revision = "a91c4e02f5d8"
branch_labels = None
depends_on = None

OLD = (21, 30)
NEW = (0, 10)


def _move(frm: tuple[int, int], to: tuple[int, int]) -> None:
    op.alter_column("users", "summary_hour", server_default=str(to[0]))
    op.alter_column("users", "summary_minute", server_default=str(to[1]))
    op.execute(
        sa.text(
            "UPDATE users SET summary_hour = :nh, summary_minute = :nm "
            "WHERE summary_hour = :oh AND summary_minute = :om"
        ).bindparams(nh=to[0], nm=to[1], oh=frm[0], om=frm[1])
    )


def upgrade() -> None:
    _move(OLD, NEW)


def downgrade() -> None:
    _move(NEW, OLD)
