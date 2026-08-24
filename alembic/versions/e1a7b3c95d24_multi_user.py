"""multi-user: access state, per-user profile, per-user claims and media

Revision ID: e1a7b3c95d24
Revises: 3e44dad1fba0
Create Date: 2026-08-23 15:00:00.000000

The schema was always multi-user-capable; this is the revision that makes it
multi-user in fact. Three groups of change:

  * `users` gains access state (`status`, `is_admin`, `code_attempts`), the
    per-user health-ingest token, the per-user summary time, and two audit
    timestamps. `tz` becomes nullable, because a person who has just typed
    /start genuinely has no timezone and there is no honest default for one; a
    CHECK stops that state ever reaching `active`.
  * `job_runs` and `media` gain a `user_id`. Both were keyed globally, and both
    were quietly wrong the moment a second person existed: one summary claim
    per day meant only the first user was ever summarised, and a globally
    unique photo digest meant the second person to photograph the same tin of
    beans got the first person's row.
  * `api_usage` gains a nullable `user_id`, unwritten for now.

The data section exists to protect the deployment that is already running.
The operator has real history, a live phone posting to the ingest endpoint, and
a summary that must not fire twice tonight. In order: stamp everybody admitted,
make the oldest row the admin, decide who is genuinely onboarded and who has to
walk the wizard, move the old global ingest token onto the operator's row so
the phone keeps working unchanged, and attribute the existing claims and media
before the new constraints go on.
"""

import os
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e1a7b3c95d24"
down_revision: str | Sequence[str] | None = "3e44dad1fba0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

USER_STATUS = sa.Enum("pending", "onboarding", "active", "blocked", name="user_status")

# The single row every backfill attributes orphaned data to. "Oldest" rather
# than "any", so a re-run picks the same person.
OLDEST_USER = "(SELECT id FROM users ORDER BY created_at, id LIMIT 1)"


def upgrade() -> None:
    bind = op.get_bind()
    USER_STATUS.create(bind, checkfirst=True)

    # --- users: schema ----------------------------------------------------
    op.add_column(
        "users",
        sa.Column(
            "status",
            USER_STATUS,
            nullable=False,
            server_default="pending",
        ),
    )
    op.create_index("ix_users_status", "users", ["status"])
    op.add_column(
        "users",
        sa.Column("is_admin", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "users",
        sa.Column("code_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("users", sa.Column("health_token", sa.String(length=64), nullable=True))
    op.create_unique_constraint("uq_users_health_token", "users", ["health_token"])
    op.add_column(
        "users",
        sa.Column("summary_hour", sa.Integer(), nullable=False, server_default="21"),
    )
    op.add_column(
        "users",
        sa.Column("summary_minute", sa.Integer(), nullable=False, server_default="30"),
    )
    op.add_column("users", sa.Column("admitted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "users",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.alter_column("users", "tz", existing_type=sa.String(length=64), nullable=True)

    # --- users: data ------------------------------------------------------
    # Everyone already in the table got in through the old env allowlist, so
    # they are admitted by definition. Dated from creation rather than now, so
    # the column does not claim they were admitted at deploy time.
    op.execute("UPDATE users SET admitted_at = created_at")
    op.execute(f"UPDATE users SET is_admin = true WHERE id = {OLDEST_USER}")

    # Who is genuinely ready, and who has to walk the wizard once. The
    # distinction is not cosmetic: the person fields were only ever written at
    # row *creation*, seeded from the environment, so a row created before
    # UMAI_SEX was set — or before the operator corrected a typo in it — holds
    # nulls or stale values. Leaving such a user `active` leaves them with a
    # target that can never be computed and no way in chat to fix it.
    op.execute(
        """
        UPDATE users SET status = CASE
            WHEN tz IS NOT NULL
             AND sex IS NOT NULL
             AND height_cm IS NOT NULL
             AND birth_date IS NOT NULL
            THEN 'active'::user_status
            ELSE 'onboarding'::user_status
        END
        """
    )

    # The phone is already posting with HEALTH_INGEST_TOKEN. Moving that value
    # onto the operator's row means the old global token simply *becomes* their
    # per-user token: no legacy branch in the endpoint, and nothing to re-paste.
    # Unset at migration time is fine — they run /token once.
    legacy_token = (os.environ.get("HEALTH_INGEST_TOKEN") or "").strip()
    if legacy_token:
        op.execute(
            sa.text(f"UPDATE users SET health_token = :t WHERE id = {OLDEST_USER}").bindparams(
                t=legacy_token[:64]
            )
        )

    op.create_check_constraint(
        "ck_users_active_has_tz", "users", "status <> 'active' OR tz IS NOT NULL"
    )
    op.create_check_constraint(
        "ck_users_summary_time",
        "users",
        "summary_hour between 0 and 23 and summary_minute between 0 and 59",
    )

    # --- job_runs ---------------------------------------------------------
    op.add_column("job_runs", sa.Column("user_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_job_runs_user", "job_runs", "users", ["user_id"], ["id"], ondelete="CASCADE"
    )
    # Attribute the claims already made, or tonight's tick re-sends a summary
    # for a day that was already sent: the new per-user constraint would see no
    # matching claim and let it through.
    op.execute(
        f"""
        UPDATE job_runs SET user_id = {OLDEST_USER}
        WHERE user_id IS NULL AND EXISTS (SELECT 1 FROM users)
        """
    )
    op.drop_constraint("uq_job_run_day", "job_runs", type_="unique")
    op.create_unique_constraint("uq_job_run_day_user", "job_runs", ["job", "day", "user_id"])
    op.create_index(
        "uq_job_run_day_global",
        "job_runs",
        ["job", "day"],
        unique=True,
        postgresql_where=sa.text("user_id IS NULL"),
    )

    # --- media ------------------------------------------------------------
    op.add_column("media", sa.Column("user_id", sa.Uuid(), nullable=True))
    op.execute(
        "UPDATE media SET user_id = (SELECT user_id FROM log_entries WHERE id = media.entry_id)"
    )
    # Rows written before their entry existed, and rows whose photo never
    # produced one. Both real: the first live session left two of them.
    op.execute(f"UPDATE media SET user_id = {OLDEST_USER} WHERE user_id IS NULL")
    op.execute("DELETE FROM media WHERE user_id IS NULL")  # only if users is empty
    op.alter_column("media", "user_id", existing_type=sa.Uuid(), nullable=False)
    op.create_foreign_key(
        "fk_media_user", "media", "users", ["user_id"], ["id"], ondelete="CASCADE"
    )
    op.drop_index("ix_media_sha256", table_name="media")
    op.create_index("ix_media_sha256", "media", ["sha256"])
    op.create_unique_constraint("uq_media_user_sha256", "media", ["user_id", "sha256"])

    # --- api_usage --------------------------------------------------------
    op.add_column("api_usage", sa.Column("user_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_api_usage_user", "api_usage", "users", ["user_id"], ["id"], ondelete="SET NULL"
    )
    op.create_index("ix_api_usage_user_id", "api_usage", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_api_usage_user_id", table_name="api_usage")
    op.drop_constraint("fk_api_usage_user", "api_usage", type_="foreignkey")
    op.drop_column("api_usage", "user_id")

    # Restoring a globally unique digest needs the duplicates gone first: two
    # users may each hold a row for the same photo by now. Keep the oldest,
    # which is the same rule the earlier revision used when this index was made
    # unique in the first place.
    op.execute(
        """
        DELETE FROM media m USING media keep
        WHERE m.sha256 = keep.sha256 AND m.created_at > keep.created_at
        """
    )
    op.drop_constraint("uq_media_user_sha256", "media", type_="unique")
    op.drop_index("ix_media_sha256", table_name="media")
    op.create_index("ix_media_sha256", "media", ["sha256"], unique=True)
    op.drop_constraint("fk_media_user", "media", type_="foreignkey")
    op.drop_column("media", "user_id")

    op.execute(
        """
        DELETE FROM job_runs j USING job_runs keep
        WHERE j.job = keep.job AND j.day = keep.day AND j.ran_at > keep.ran_at
        """
    )
    op.drop_index("uq_job_run_day_global", table_name="job_runs")
    op.drop_constraint("uq_job_run_day_user", "job_runs", type_="unique")
    op.create_unique_constraint("uq_job_run_day", "job_runs", ["job", "day"])
    op.drop_constraint("fk_job_runs_user", "job_runs", type_="foreignkey")
    op.drop_column("job_runs", "user_id")

    op.drop_constraint("ck_users_summary_time", "users", type_="check")
    op.drop_constraint("ck_users_active_has_tz", "users", type_="check")
    # A null zone cannot survive the column going back to NOT NULL. There is no
    # right answer for a pending user here, so they are removed: they had no
    # data, having never been allowed past the gate.
    op.execute("DELETE FROM users WHERE tz IS NULL")
    op.alter_column("users", "tz", existing_type=sa.String(length=64), nullable=False)
    for column in (
        "updated_at",
        "admitted_at",
        "summary_minute",
        "summary_hour",
        "health_token",
        "code_attempts",
        "is_admin",
    ):
        if column == "health_token":
            op.drop_constraint("uq_users_health_token", "users", type_="unique")
        op.drop_column("users", column)
    op.drop_index("ix_users_status", table_name="users")
    op.drop_column("users", "status")
    USER_STATUS.drop(op.get_bind(), checkfirst=True)
