"""add water target to users

Revision ID: c4d5e6f7a8b9
Revises: b7e2c9a41f36
Create Date: 2026-08-23 12:00:00.000000

Daily water intake target in ml. NULL means "use the system default"
(2500 ml). Editable in chat via the Configure menu.
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "c4d5e6f7a8b9"
down_revision: Union[str, Sequence[str], None] = "b7e2c9a41f36"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("water_target_ml", sa.Float(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "water_target_ml")
