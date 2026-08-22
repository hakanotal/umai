"""user cuisines, enrichment attempts, unique media sha

Revision ID: a3f1c7e94b20
Revises: 88b444f0d07e
Create Date: 2026-08-22 18:40:00.000000

Three changes, all supporting the background enrichment job:

  * users.cuisines — perception context, fed to the vision model as a hint and
    to the enrichment model as the tradition a dish belongs to.
  * enrichment_attempts — the job's memory, so a dish whose composition the
    model cannot produce sanely is not re-researched on every tick at the price
    of the largest model in the roster.
  * a unique index on media.sha256 — re-sending the same photo was writing a
    second media row; the file itself was already deduplicated on disk.

The media index is created CONCURRENTLY-free deliberately: the table holds a
handful of rows on a single-user deployment, and a plain index build is
instantaneous there while CONCURRENTLY cannot run inside a migration's
transaction.
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a3f1c7e94b20"
down_revision: Union[str, Sequence[str], None] = "88b444f0d07e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "cuisines",
            postgresql.ARRAY(sa.Text()),
            server_default="{}",
            nullable=False,
        ),
    )

    op.create_table(
        "enrichment_attempts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("detected_name", sa.String(length=200), nullable=False),
        sa.Column(
            "state",
            postgresql.ENUM(name="food_state", create_type=False),
            nullable=False,
        ),
        sa.Column("food_id", sa.UUID(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "cuisines", postgresql.ARRAY(sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column("model", sa.String(length=120), nullable=True),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["food_id"], ["foods.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("detected_name", "state", name="uq_enrichment_name_state"),
    )

    # Duplicates from before the constraint existed would block the index, and
    # the oldest row is the one perception_runs may already reference.
    op.execute(
        """
        DELETE FROM media m
        USING media keep
        WHERE m.sha256 = keep.sha256
          AND m.created_at > keep.created_at
        """
    )
    op.drop_index("ix_media_sha256", table_name="media")
    op.create_index("ix_media_sha256", "media", ["sha256"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_media_sha256", table_name="media")
    op.create_index("ix_media_sha256", "media", ["sha256"], unique=False)
    op.drop_table("enrichment_attempts")
    op.drop_column("users", "cuisines")
