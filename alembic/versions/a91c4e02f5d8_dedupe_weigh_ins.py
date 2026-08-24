"""collapse duplicate weigh-ins to one per local day

Revision ID: a91c4e02f5d8
Revises: f2b8c41d67ae
Create Date: 2026-08-23 22:00:00.000000

`log_simple` appended every weigh-in, so a mistyped weight stayed in the series
beside its correction. The live database had three readings on each of two
consecutive days. `log_weight` now supersedes rather than appends; this cleans
up what the old behaviour left behind.

The last reading of each local day wins, which is the same rule the new code
applies: a second weigh-in the same day is a correction, so the correction is
the one to keep. The earlier rows are superseded rather than deleted — entries
are immutable, and a weight the user actually typed is a fact about what they
did even when it is not a fact about their body.

No `corrections` rows are written. This is a repair of data the application
should never have produced, not a correction anybody made, and inventing an
audit record for a decision no user took would be worse than leaving the gap.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "a91c4e02f5d8"
down_revision: str | Sequence[str] | None = "f2b8c41d67ae"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Grouped by the *local* date, via the owner's timezone, because that is
    # the day the rule is about: 23:00 Tuesday and 07:00 Wednesday are two
    # measurements, eight hours apart.
    op.execute(
        """
        WITH ranked AS (
            SELECT e.id,
                   e.user_id,
                   date(timezone(u.tz, e.occurred_at)) AS local_day,
                   row_number() OVER (
                       PARTITION BY e.user_id, date(timezone(u.tz, e.occurred_at))
                       ORDER BY e.occurred_at DESC, e.id DESC
                   ) AS rn,
                   first_value(e.id) OVER (
                       PARTITION BY e.user_id, date(timezone(u.tz, e.occurred_at))
                       ORDER BY e.occurred_at DESC, e.id DESC
                   ) AS keeper
            FROM log_entries e
            JOIN users u ON u.id = e.user_id
            WHERE e.kind = 'weight'
              AND e.superseded_by IS NULL
              AND u.tz IS NOT NULL
        )
        UPDATE log_entries e
        SET superseded_by = r.keeper
        FROM ranked r
        WHERE e.id = r.id AND r.rn > 1
        """
    )


def downgrade() -> None:
    # Un-supersede only the chains this revision made: a weight entry pointing
    # at another weight entry on the same local day, with no corrections row
    # explaining it. A correction the user actually made has one, and must not
    # be undone here.
    op.execute(
        """
        UPDATE log_entries e
        SET superseded_by = NULL
        FROM log_entries k, users u
        WHERE e.superseded_by = k.id
          AND e.kind = 'weight' AND k.kind = 'weight'
          AND u.id = e.user_id AND u.tz IS NOT NULL
          AND date(timezone(u.tz, e.occurred_at)) = date(timezone(u.tz, k.occurred_at))
          AND NOT EXISTS (SELECT 1 FROM corrections c WHERE c.entry_id = e.id)
        """
    )
