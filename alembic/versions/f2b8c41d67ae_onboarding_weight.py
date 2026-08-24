"""users.onboarding_weight_kg

Revision ID: f2b8c41d67ae
Revises: e1a7b3c95d24
Create Date: 2026-08-23 21:00:00.000000

The onboarding wizard derives which question is owed from the first unset
column on the row — that is what lets it survive a restart without an FSM. The
weight question had no column: the handler set a plain attribute on the ORM
object, which SQLAlchemy does not persist, so the answer vanished on commit and
the next message asked the same question again. Every new user would have been
trapped on question five.

Nullable, and null for everybody who onboarded before it existed. That is
correct rather than convenient: the wizard only consults it while a user is
`onboarding`, and every existing row is already `active`.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f2b8c41d67ae"
down_revision: str | Sequence[str] | None = "e1a7b3c95d24"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("onboarding_weight_kg", sa.Float(), nullable=True))
    # Anyone already active finished onboarding under the old code, so the
    # question is not owed. Backfilling from their first weight entry keeps
    # `next_step` returning None for them, which is what "done" means.
    op.execute(
        """
        UPDATE users u SET onboarding_weight_kg = (
            SELECT e.value FROM log_entries e
            WHERE e.user_id = u.id AND e.kind = 'weight' AND e.superseded_by IS NULL
            ORDER BY e.occurred_at ASC LIMIT 1
        )
        WHERE u.status = 'active'
        """
    )
    # An active user with no weight reading at all would otherwise be the one
    # row the wizard still considers unfinished. It cannot reach the wizard —
    # that router is behind NeedsGate — but leaving the contradiction in the
    # data is how the next confusing bug starts.
    op.execute(
        "UPDATE users SET onboarding_weight_kg = 0 "
        "WHERE status = 'active' AND onboarding_weight_kg IS NULL"
    )


def downgrade() -> None:
    op.drop_column("users", "onboarding_weight_kg")
