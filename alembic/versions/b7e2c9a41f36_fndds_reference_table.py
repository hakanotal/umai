"""fndds reference table

Revision ID: b7e2c9a41f36
Revises: a3f1c7e94b20
Create Date: 2026-08-22 20:10:00.000000

A reference table of USDA FNDDS survey foods for the enrichment job to search.

It is not `foods`, and the distinction is the point. `foods` is the set of
things this user actually eats, and the resolver runs a trigram match over it on
every logged item; adding 5,431 US survey descriptions to that space means
"Beans and white rice" competing with "pilav". This table is consulted by a
model that has decided the resolver already failed, and a row graduates into
`foods` only when the model picks it, carrying its fdc_id in source_ref.

fdc_id is the primary key rather than a surrogate uuid: it is USDA's own stable
identifier, so importing a later FNDDS release updates rows in place. Every
other table here uses a uuid pk, so this is a deliberate departure and not an
oversight — nothing references these rows by foreign key.

The gin_trgm_ops index is what makes the search a lookup rather than a scan.
pg_trgm is already installed (initial migration); no extension work here.
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "b7e2c9a41f36"
down_revision: Union[str, Sequence[str], None] = "a3f1c7e94b20"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "fndds_foods",
        sa.Column("fdc_id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("food_code", sa.String(length=16), nullable=True),
        sa.Column("description", sa.String(length=300), nullable=False),
        sa.Column("wweia_category", sa.String(length=200), nullable=True),
        sa.Column("kcal_per_100g", sa.Float(), nullable=False),
        sa.Column("protein_g_per_100g", sa.Float(), nullable=False),
        sa.Column("carbs_g_per_100g", sa.Float(), nullable=False),
        sa.Column("fat_g_per_100g", sa.Float(), nullable=False),
        sa.Column("fiber_g_per_100g", sa.Float(), nullable=True),
        sa.Column("sugar_g_per_100g", sa.Float(), nullable=True),
        sa.Column("sodium_mg_per_100g", sa.Float(), nullable=True),
        sa.CheckConstraint("kcal_per_100g >= 0", name="ck_fndds_kcal_nonneg"),
        sa.PrimaryKeyConstraint("fdc_id"),
    )
    op.create_index(
        "ix_fndds_description_trgm",
        "fndds_foods",
        ["description"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"description": "gin_trgm_ops"},
    )


def downgrade() -> None:
    op.drop_index("ix_fndds_description_trgm", table_name="fndds_foods", postgresql_using="gin")
    op.drop_table("fndds_foods")
