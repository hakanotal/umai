"""SQLAlchemy table definitions.

Implements the data model in docs/umai-project-plan.md section 6.3.

Two deliberate departures from that section, both from
docs/technical-implementation.md section 9:

  * No vector columns yet. Phase 1 resolves names with pg_trgm against
    canonical_name_en and the aliases array, which needs no embedding model on
    the critical path. The pgvector extension is installed from day one so the
    columns can be added without a rebuild.
  * When they do arrive the dimension is 512 (CLIP ViT-B/32), not the 768 the
    plan states.

Entries are immutable. A correction is a new row referencing the original via
superseded_by, which is what makes explain-why and historical re-analysis
possible. Macro columns on food_items are a query-speed cache and are always
recomputable from food_id plus grams.
"""

from __future__ import annotations

import datetime as dt
import enum
import uuid
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


TS = DateTime(timezone=True)  # storage is UTC, always. convert at the boundary.


# ---------------------------------------------------------------------------
# Enumerations. Stored as native postgres enums so a typo is a write error
# rather than a silent category that never matches a query.
# ---------------------------------------------------------------------------


class FoodState(enum.StrEnum):
    raw = "raw"
    boiled = "boiled"
    grilled = "grilled"
    fried = "fried"
    baked = "baked"
    roasted = "roasted"
    steamed = "steamed"
    dried = "dried"
    liquid = "liquid"
    unknown = "unknown"


class EntryKind(enum.StrEnum):
    food = "food"
    drink = "drink"
    water = "water"
    exercise = "exercise"
    supplement = "supplement"
    weight = "weight"
    measurement = "measurement"
    mood = "mood"
    satiety = "satiety"


class EntrySource(enum.StrEnum):
    photo = "photo"
    text = "text"
    voice = "voice"
    button = "button"
    library = "library"
    recipe = "recipe"
    health_sync = "health_sync"
    manual = "manual"


class FoodSource(enum.StrEnum):
    turkomp = "turkomp"
    usda_foundation = "usda_foundation"
    usda_sr = "usda_sr"
    usda_branded = "usda_branded"
    off = "off"
    label_photo = "label_photo"
    user = "user"
    model = "model"


class GramsSource(enum.StrEnum):
    vlm = "vlm"
    user = "user"
    recipe = "recipe"
    label = "label"
    library_prior = "library_prior"


class ResolutionMethod(enum.StrEnum):
    exact = "exact"
    trigram = "trigram"
    embedding = "embedding"
    llm_tiebreak = "llm_tiebreak"
    recipe = "recipe"
    library = "library"
    new = "new"


class UserStatus(enum.StrEnum):
    """Where a person is between "typed /start" and "logging meals".

    Four states rather than a boolean, because three of them need different
    handlers. `pending` has sent something but not the invite phrase and must
    reach nothing but the gate. `onboarding` has been admitted but has no
    timezone or body statistics yet, so every target and every local-day
    boundary would be a guess. `active` is the only state the food handlers
    accept. `blocked` is how access is revoked without destroying the history,
    which matters because the entries are immutable and deleting the user row
    would cascade them away.
    """

    pending = "pending"
    onboarding = "onboarding"
    active = "active"
    blocked = "blocked"


def _pg_enum(e: type[enum.Enum], name: str) -> Enum:
    return Enum(e, name=name, values_callable=lambda x: [m.value for m in x])


# ---------------------------------------------------------------------------
# People
# ---------------------------------------------------------------------------


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        # An active user always has a zone. Nothing else in the schema can
        # express "these columns are required, but only once onboarding is
        # finished", and without it a half-onboarded row could reach
        # `day_bounds` and produce totals for a day that does not exist.
        CheckConstraint(
            "status <> 'active' OR tz IS NOT NULL",
            name="ck_users_active_has_tz",
        ),
        CheckConstraint(
            "summary_hour between 0 and 23 and summary_minute between 0 and 59",
            name="ck_users_summary_time",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    # Access state. Every message is offered to the gate routers first and
    # reaches a feature handler only when this says `active`; see
    # telegram/middleware.py.
    status: Mapped[UserStatus] = mapped_column(
        _pg_enum(UserStatus, "user_status"),
        nullable=False,
        server_default=UserStatus.pending.value,
        index=True,
    )
    # One privilege — seeing and blocking other people — so a boolean rather
    # than a role table nobody would populate.
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    # Wrong invite phrases, since the row was created. At the limit the row
    # flips to `blocked`: without a counter a hidden phrase is an oracle that
    # answers several guesses a second.
    code_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # Bearer credential for this person's health-ingest endpoint. Plaintext, so
    # /token can show it again rather than forcing a rotation and a re-paste
    # into a phone app; the endpoint reads it, nothing else does.
    health_token: Mapped[str | None] = mapped_column(String(64), unique=True)
    # When the daily digest is due, in this user's own zone. Two integers
    # rather than a module constant because the scheduler now ticks over every
    # active user and each of them keeps their own hours. The default is ten
    # past midnight, and a time this early means the digest reports the day
    # that has just ended — see `jobs.SMALL_HOURS`.
    summary_hour: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    summary_minute: Mapped[int] = mapped_column(Integer, nullable=False, server_default="10")
    # Nullable, unlike everything else here that reads as required. This column
    # is the source of truth for every local-day boundary — food totals,
    # summaries, step bucketing — and there is no honest default for it, so a
    # pending row carries None and the CHECK above stops that state reaching
    # `active`. Read it through `zone` rather than directly.
    tz: Mapped[str | None] = mapped_column(String(64))
    sex: Mapped[str | None] = mapped_column(String(16))
    height_cm: Mapped[float | None] = mapped_column(Float)
    birth_date: Mapped[dt.date | None] = mapped_column(Date)
    goal_type: Mapped[str | None] = mapped_column(String(32))
    goal_rate_kg_per_week: Mapped[float | None] = mapped_column(Float)
    # Cuisine slugs from core.cuisines. Perception context, not a preference
    # setting: telling the vision model this person eats Turkish food is the
    # difference between "flatbread with reddish paste" and "lahmacun", and
    # only the second one is a lookup key. Edited in chat with /cuisines.
    cuisines: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, server_default="{}")
    # Daily water target in ml. None means "use the system default" (2500 ml).
    # Editable in chat via the Configure menu; the evening summary and
    # water reminders read it to decide whether to nudge.
    water_target_ml: Mapped[float | None] = mapped_column(Float)
    # The weight the user gave during onboarding, kept because every wizard
    # step has to be a real column: the wizard derives "which question is
    # owed" from the first unset field, so a step whose answer lives anywhere
    # else is a question that can never be answered. It was a plain attribute
    # on the ORM object once, which is not persisted — the answer vanished on
    # commit and the wizard asked again, forever.
    #
    # It is *not* the weight series. `_finish` turns this into the first
    # LogEntry, which is what trend and calibration read; this column only
    # records what was said at onboarding.
    onboarding_weight_kg: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[dt.datetime] = mapped_column(TS, server_default=func.now())
    # When the invite phrase was accepted. Null for the bootstrap admin, who
    # never needed one.
    admitted_at: Mapped[dt.datetime | None] = mapped_column(TS)
    updated_at: Mapped[dt.datetime] = mapped_column(
        TS, server_default=func.now(), onupdate=func.now()
    )

    @property
    def zone(self) -> str:
        """This user's timezone, or a loud failure.

        Every caller that computes a local day wants a `str`, and `tz` is
        `str | None` because a pending row genuinely has no zone. Funnelling
        the reads through here keeps those call sites honest without either
        spreading `assert` through them or — far worse — letting a `None` reach
        `ZoneInfo` as the string "None".
        """
        if self.tz is None:
            raise RuntimeError(
                f"user {self.telegram_id} has no timezone: onboarding is incomplete "
                f"(status={self.status})"
            )
        return self.tz


# ---------------------------------------------------------------------------
# The food composition table: the backbone
# ---------------------------------------------------------------------------


class Food(Base):
    """Per 100g, never per 1g: every source publishes per 100g, so storing in
    the same unit removes a conversion at every import and every comparison."""

    __tablename__ = "foods"
    __table_args__ = (
        UniqueConstraint("canonical_name_en", "state", name="uq_foods_name_state"),
        CheckConstraint("trust_tier between 1 and 4", name="ck_foods_trust_tier"),
        CheckConstraint("kcal_per_100g >= 0", name="ck_foods_kcal_nonneg"),
        # A yield factor outside this range is a data entry error, not a food.
        CheckConstraint(
            "yield_factor is null or (yield_factor > 0.1 and yield_factor < 5)",
            name="ck_foods_yield_sane",
        ),
        Index(
            "ix_foods_name_trgm",
            "canonical_name_en",
            postgresql_using="gin",
            postgresql_ops={"canonical_name_en": "gin_trgm_ops"},
        ),
        Index("ix_foods_aliases", "aliases", postgresql_using="gin"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    canonical_name_en: Mapped[str] = mapped_column(String(200))
    # Turkish and colloquial names live here, so "mercimek corbasi",
    # "lentil soup" and "red lentil soup" all resolve to one row while the
    # chat can still speak either language.
    aliases: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    state: Mapped[FoodState] = mapped_column(_pg_enum(FoodState, "food_state"))

    kcal_per_100g: Mapped[float] = mapped_column(Float)
    protein_g_per_100g: Mapped[float] = mapped_column(Float, default=0.0)
    carbs_g_per_100g: Mapped[float] = mapped_column(Float, default=0.0)
    fat_g_per_100g: Mapped[float] = mapped_column(Float, default=0.0)
    fiber_g_per_100g: Mapped[float | None] = mapped_column(Float)
    sugar_g_per_100g: Mapped[float | None] = mapped_column(Float)
    sodium_mg_per_100g: Mapped[float | None] = mapped_column(Float)

    # null for solids. milk ~1.03, olive oil ~0.92, honey ~1.42.
    density_g_per_ml: Mapped[float | None] = mapped_column(Float)
    # cooked weight / raw weight. rice roughly 2.5-3.0, meat roughly 0.75.
    yield_factor: Mapped[float | None] = mapped_column(Float)
    # oil taken up when fried, as a percentage of the food's weight.
    fat_absorption_pct: Mapped[float | None] = mapped_column(Float)

    source: Mapped[FoodSource] = mapped_column(_pg_enum(FoodSource, "food_source"))
    source_ref: Mapped[str | None] = mapped_column(String(200))
    trust_tier: Mapped[int] = mapped_column(Integer)
    verified_at: Mapped[dt.datetime | None] = mapped_column(TS)
    created_at: Mapped[dt.datetime] = mapped_column(TS, server_default=func.now())

    @property
    def is_provisional(self) -> bool:
        """Tier 4 rows are model-invented. Useful, but never silently fact."""
        return self.trust_tier >= 4


# ---------------------------------------------------------------------------
# What was actually eaten
# ---------------------------------------------------------------------------


class LogEntry(Base):
    __tablename__ = "log_entries"
    __table_args__ = (Index("ix_log_entries_user_time", "user_id", "occurred_at"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    # when you ate it, versus when you told the bot. backfill makes these differ.
    occurred_at: Mapped[dt.datetime] = mapped_column(TS)
    logged_at: Mapped[dt.datetime] = mapped_column(TS, server_default=func.now())
    kind: Mapped[EntryKind] = mapped_column(_pg_enum(EntryKind, "entry_kind"))
    source: Mapped[EntrySource] = mapped_column(_pg_enum(EntrySource, "entry_source"))
    # Immutability: an edit writes a new row and points the old one at it.
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("log_entries.id", ondelete="SET NULL")
    )
    raw_input_ref: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    # non-food entries carry their payload here: ml of water, kg of weight,
    # minutes of exercise. food entries carry rows in food_items instead.
    value: Mapped[float | None] = mapped_column(Float)
    unit: Mapped[str | None] = mapped_column(String(16))

    items: Mapped[list[FoodItem]] = relationship(
        back_populates="entry", cascade="all, delete-orphan"
    )
    media: Mapped[list[Media]] = relationship(back_populates="entry", cascade="all, delete-orphan")


class FoodItem(Base):
    __tablename__ = "food_items"
    __table_args__ = (CheckConstraint("grams >= 0", name="ck_food_items_grams_nonneg"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    entry_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("log_entries.id", ondelete="CASCADE"), index=True
    )
    # Display order within the entry, so "✏️ 2" in a confirmation button
    # addresses the same item the user was shown. UUID primary keys do not
    # preserve insertion order.
    position: Mapped[int | None] = mapped_column(Integer)
    food_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("foods.id", ondelete="SET NULL"))
    recipe_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("recipes.id", ondelete="SET NULL")
    )

    # verbatim from the vision model, kept even after resolution: this is the
    # training signal for the library and the audit trail for explain-why.
    detected_name: Mapped[str | None] = mapped_column(String(200))
    detected_state: Mapped[FoodState | None] = mapped_column(_pg_enum(FoodState, "food_state"))

    grams: Mapped[float] = mapped_column(Float)
    grams_source: Mapped[GramsSource] = mapped_column(_pg_enum(GramsSource, "grams_source"))
    grams_confidence: Mapped[float | None] = mapped_column(Float)
    resolution_method: Mapped[ResolutionMethod] = mapped_column(
        _pg_enum(ResolutionMethod, "resolution_method")
    )
    resolution_confidence: Mapped[float | None] = mapped_column(Float)

    # DERIVED and stored for query speed only. always recomputable from
    # food_id plus grams; resolver.compute is the only thing that writes them.
    kcal: Mapped[float] = mapped_column(Float, default=0.0)
    protein_g: Mapped[float] = mapped_column(Float, default=0.0)
    carbs_g: Mapped[float] = mapped_column(Float, default=0.0)
    fat_g: Mapped[float] = mapped_column(Float, default=0.0)
    fiber_g: Mapped[float | None] = mapped_column(Float)

    entry: Mapped[LogEntry] = relationship(back_populates="items")


class Correction(Base):
    __tablename__ = "corrections"

    id: Mapped[uuid.UUID] = _uuid_pk()
    entry_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("log_entries.id", ondelete="SET NULL")
    )
    food_item_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("food_items.id", ondelete="SET NULL")
    )
    field: Mapped[str] = mapped_column(String(64))
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    corrected_at: Mapped[dt.datetime] = mapped_column(TS, server_default=func.now())
    # a correction is only worth something once it has trained something.
    applied_to_library: Mapped[bool] = mapped_column(Boolean, default=False)
    applied_to_prior: Mapped[bool] = mapped_column(Boolean, default=False)


class Media(Base):
    """A photo on disk, and the entry it produced.

    `user_id` is carried directly rather than reached through `entry_id`,
    because the row is written *before* the entry exists — the perception run
    needs something to hang off — and an orphaned row with a null entry would
    otherwise have no owner at all.

    The digest is unique per user, not globally. Globally unique, the second
    person to photograph the same tin of beans hit `ON CONFLICT DO NOTHING`,
    got back the first person's row, and had their meal attached to a
    stranger's entry."""

    __tablename__ = "media"
    __table_args__ = (UniqueConstraint("user_id", "sha256", name="uq_media_user_sha256"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    entry_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("log_entries.id", ondelete="CASCADE")
    )
    path: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    taken_at: Mapped[dt.datetime | None] = mapped_column(TS)
    created_at: Mapped[dt.datetime] = mapped_column(TS, server_default=func.now())

    entry: Mapped[LogEntry | None] = relationship(back_populates="media")


class PerceptionRun(Base):
    """The raw model response, kept alongside the parsed result.

    Not in the plan's table list, but section 6.2 requires perception to store
    it "so old photos can be re-scored later", and re-deriving past estimates
    after a model change is a listed mitigation for provider risk.
    """

    __tablename__ = "perception_runs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    media_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("media.id", ondelete="CASCADE"))
    entry_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("log_entries.id", ondelete="SET NULL")
    )
    model: Mapped[str] = mapped_column(String(120))
    prompt_fingerprint: Mapped[str] = mapped_column(String(64))
    raw_response: Mapped[dict] = mapped_column(JSONB)
    parsed_ok: Mapped[bool] = mapped_column(Boolean, default=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[dt.datetime] = mapped_column(TS, server_default=func.now())


# ---------------------------------------------------------------------------
# Personal learning layer
# ---------------------------------------------------------------------------


class FoodLibrary(Base):
    __tablename__ = "food_library"
    __table_args__ = (UniqueConstraint("user_id", "food_id", name="uq_library_user_food"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    food_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("foods.id", ondelete="CASCADE"))
    display_name: Mapped[str] = mapped_column(String(200))
    times_logged: Mapped[int] = mapped_column(Integer, default=0)
    last_logged_at: Mapped[dt.datetime | None] = mapped_column(TS)
    user_verified: Mapped[bool] = mapped_column(Boolean, default=False)


class PortionPrior(Base):
    """How much of this food *you* actually serve yourself.

    Injected into the stage 1 prompt as a hint. This is the mechanism by which
    the system's largest error shrinks with use, and it is unavailable to any
    app that does not keep your history.
    """

    __tablename__ = "portion_priors"
    __table_args__ = (UniqueConstraint("user_id", "food_id", name="uq_prior_user_food"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    food_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("foods.id", ondelete="CASCADE"))
    median_grams: Mapped[float] = mapped_column(Float)
    p25_grams: Mapped[float | None] = mapped_column(Float)
    p75_grams: Mapped[float | None] = mapped_column(Float)
    n_observations: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[dt.datetime] = mapped_column(TS, server_default=func.now())


# ---------------------------------------------------------------------------
# Recipes: composition, not entry
# ---------------------------------------------------------------------------


class Recipe(Base):
    __tablename__ = "recipes"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(200))
    # the same dish cooked differently is a version, not a new recipe.
    parent_recipe_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("recipes.id", ondelete="SET NULL")
    )

    raw_input_grams: Mapped[float | None] = mapped_column(Float)
    # weighed once, after cooking. without it every simmered dish is wrong by
    # the evaporation rate, typically 10-30%.
    cooked_output_grams: Mapped[float | None] = mapped_column(Float)
    servings: Mapped[float | None] = mapped_column(Float)
    servings_grams: Mapped[float | None] = mapped_column(Float)

    # DERIVED from the ingredients. recomputed, never typed in.
    kcal_per_100g: Mapped[float | None] = mapped_column(Float)
    protein_g_per_100g: Mapped[float | None] = mapped_column(Float)
    carbs_g_per_100g: Mapped[float | None] = mapped_column(Float)
    fat_g_per_100g: Mapped[float | None] = mapped_column(Float)

    times_logged: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(TS, server_default=func.now())

    ingredients: Mapped[list[RecipeIngredient]] = relationship(
        back_populates="recipe", cascade="all, delete-orphan"
    )

    @property
    def yield_factor(self) -> float | None:
        if not self.raw_input_grams or not self.cooked_output_grams:
            return None
        return self.cooked_output_grams / self.raw_input_grams


class RecipeIngredient(Base):
    """No macros stored. Fix a food row and every recipe using it corrects
    itself, retroactively."""

    __tablename__ = "recipe_ingredients"

    id: Mapped[uuid.UUID] = _uuid_pk()
    recipe_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("recipes.id", ondelete="CASCADE"), index=True
    )
    food_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("foods.id", ondelete="CASCADE"))
    grams: Mapped[float] = mapped_column(Float)

    recipe: Mapped[Recipe] = relationship(back_populates="ingredients")


# ---------------------------------------------------------------------------
# Passive data and derived series
# ---------------------------------------------------------------------------


class HealthMetric(Base):
    __tablename__ = "health_metrics"
    __table_args__ = (
        # The idempotency key. The phone will eventually deliver three days of
        # backlog in one request, and duplicate steps corrupt the calibration fit.
        UniqueConstraint("user_id", "metric", "recorded_at", "source", name="uq_health_natural"),
        Index("ix_health_user_metric_time", "user_id", "metric", "recorded_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    metric: Mapped[str] = mapped_column(String(64))
    value: Mapped[float] = mapped_column(Float)
    unit: Mapped[str | None] = mapped_column(String(32))
    recorded_at: Mapped[dt.datetime] = mapped_column(TS)
    source: Mapped[str] = mapped_column(String(64), default="health_auto_export")
    external_id: Mapped[str | None] = mapped_column(String(200))
    ingested_at: Mapped[dt.datetime] = mapped_column(TS, server_default=func.now())


class DailyRollup(Base):
    __tablename__ = "daily_rollups"
    __table_args__ = (UniqueConstraint("user_id", "date", name="uq_rollup_user_date"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    date: Mapped[dt.date] = mapped_column(Date)
    kcal_reported: Mapped[float] = mapped_column(Float, default=0.0)
    kcal_calibrated: Mapped[float | None] = mapped_column(Float)
    protein_g: Mapped[float] = mapped_column(Float, default=0.0)
    carbs_g: Mapped[float] = mapped_column(Float, default=0.0)
    fat_g: Mapped[float] = mapped_column(Float, default=0.0)
    water_ml: Mapped[float] = mapped_column(Float, default=0.0)
    steps: Mapped[int | None] = mapped_column(Integer)
    sleep_minutes: Mapped[int | None] = mapped_column(Integer)
    entry_count: Mapped[int] = mapped_column(Integer, default=0)
    # what gates the calibration fit. 0..1.
    coverage_score: Mapped[float] = mapped_column(Float, default=0.0)


class TrendWeight(Base):
    __tablename__ = "trend_weight"
    __table_args__ = (UniqueConstraint("user_id", "date", name="uq_trend_user_date"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    date: Mapped[dt.date] = mapped_column(Date)
    raw_kg: Mapped[float | None] = mapped_column(Float)  # null on days you did not weigh
    ewma_kg: Mapped[float] = mapped_column(Float)


class CalibrationState(Base):
    __tablename__ = "calibration_state"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    computed_at: Mapped[dt.datetime] = mapped_column(TS, server_default=func.now())
    window_days: Mapped[int] = mapped_column(Integer)
    k_factor: Mapped[float] = mapped_column(Float)
    k_ci_low: Mapped[float | None] = mapped_column(Float)
    k_ci_high: Mapped[float | None] = mapped_column(Float)
    tdee_estimate: Mapped[float] = mapped_column(Float)
    tdee_ci_low: Mapped[float | None] = mapped_column(Float)
    tdee_ci_high: Mapped[float | None] = mapped_column(Float)
    coverage: Mapped[float] = mapped_column(Float)
    # a fit can be computed and still refused: not enough data, or gated out.
    applied: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str | None] = mapped_column(Text)


class Target(Base):
    __tablename__ = "targets"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    effective_from: Mapped[dt.date] = mapped_column(Date)
    kcal_target: Mapped[float] = mapped_column(Float)
    protein_target_g: Mapped[float] = mapped_column(Float)
    rationale: Mapped[str | None] = mapped_column(Text)
    derived_from_calibration_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("calibration_state.id", ondelete="SET NULL")
    )


class Insight(Base):
    __tablename__ = "insights"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    created_at: Mapped[dt.datetime] = mapped_column(TS, server_default=func.now())
    kind: Mapped[str] = mapped_column(String(64))
    statement: Mapped[str] = mapped_column(Text)
    supporting_query: Mapped[str | None] = mapped_column(Text)
    n: Mapped[int] = mapped_column(Integer)
    effect_size: Mapped[float | None] = mapped_column(Float)
    p_value: Mapped[float | None] = mapped_column(Float)
    shown_at: Mapped[dt.datetime | None] = mapped_column(TS)
    user_reaction: Mapped[str | None] = mapped_column(String(32))


class Commitment(Base):
    __tablename__ = "commitments"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    week_of: Mapped[dt.date] = mapped_column(Date)
    text: Mapped[str] = mapped_column(Text)
    follow_up_result: Mapped[str | None] = mapped_column(Text)


class ApiUsage(Base):
    """Exists so the bot can report its own cost honestly.

    `user_id` is nullable and, for now, unwritten: the usage callback fires
    from whichever worker thread made the model call and has no user in scope,
    so filling it needs a context variable threaded from the access middleware.
    The column lands first because this table is append-only — rows written
    before that wiring simply stay null, meaning "before multi-user", and no
    backfill is ever needed."""

    __tablename__ = "api_usage"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    called_at: Mapped[dt.datetime] = mapped_column(TS, server_default=func.now(), index=True)
    provider: Mapped[str] = mapped_column(String(32), default="openrouter")
    model: Mapped[str] = mapped_column(String(120))
    purpose: Mapped[str] = mapped_column(String(64))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    # money. Numeric, not float: this one gets summed over thousands of rows.
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 6), default=0)
    latency_ms: Mapped[int | None] = mapped_column(Integer)


class Dinnerware(Base):
    """Measured once with a bank card beside it. The cheapest accuracy win
    available: scale context roughly halves portion error (papers A04)."""

    __tablename__ = "dinnerware"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_dinnerware_user_name"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(Text)


class EnrichmentAttempt(Base):
    """One attempt to research a food the table did not hold.

    Exists so the background job has a memory. Without it a dish the model
    cannot produce a sane composition for is retried on every tick, forever, at
    the price of the largest model in the roster; and a rejected answer leaves
    no trace of *why* it was rejected, which is exactly the thing worth reading
    after a week of running.

    Keyed on the normalised detected name plus state, which is the same key the
    resolver failed on, so a hit here means "we have already been asked this".
    """

    __tablename__ = "enrichment_attempts"
    __table_args__ = (UniqueConstraint("detected_name", "state", name="uq_enrichment_name_state"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    detected_name: Mapped[str] = mapped_column(String(200))
    state: Mapped[FoodState] = mapped_column(_pg_enum(FoodState, "food_state"))
    # The row it produced, if it produced one. Null means every attempt so far
    # was rejected by the validator.
    food_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("foods.id", ondelete="SET NULL"))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    cuisines: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, server_default="{}")
    model: Mapped[str | None] = mapped_column(String(120))
    first_seen_at: Mapped[dt.datetime] = mapped_column(TS, server_default=func.now())
    last_attempt_at: Mapped[dt.datetime | None] = mapped_column(TS)


class JobRun(Base):
    """Idempotency for scheduled jobs.

    A restart mid-job or a missed window re-runs it, and sending the evening
    summary twice is exactly the kind of thing that makes a bot feel broken.
    The claim means it is sent once per calendar day, ever.

    Per *user*, since the scheduler ticks over everybody: with the claim keyed
    on (job, day) alone the first person summarised would take the day and
    nobody else would hear anything. `user_id` is nullable so a genuinely
    global job — a backup, a price refresh — can still claim a day for the
    whole deployment.

    Hence two constraints rather than one. A nullable column inside a UNIQUE is
    not restrictive in Postgres, because NULL is never equal to NULL, so two
    global claims for the same day would both insert. The partial index is what
    actually enforces the global case."""

    __tablename__ = "job_runs"
    __table_args__ = (
        UniqueConstraint("job", "day", "user_id", name="uq_job_run_day_user"),
        Index(
            "uq_job_run_day_global",
            "job",
            "day",
            unique=True,
            postgresql_where=text("user_id IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    job: Mapped[str] = mapped_column(String(100))
    day: Mapped[dt.date] = mapped_column(Date)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    ran_at: Mapped[dt.datetime] = mapped_column(TS, server_default=func.now())


class FnddsFood(Base):
    """USDA FNDDS survey foods, as a reference table the enrichment job searches.

    Deliberately *not* rows in `foods`. `foods` is the set of things this user
    actually eats — a few hundred rows the resolver matches against on every
    photo. Pouring 5,431 US survey descriptions into it would put "Beans and
    white rice" and "Baby Toddler cereal, rice with fruit, dry" into the trigram
    space that has to find "pilav", where they can only outrank better matches.

    So this table is a lookup the model consults, not a lookup the resolver
    walks. A row only becomes a `foods` row when the enrichment model picks it,
    and then it is copied with its fdc_id recorded in source_ref.

    One property drives how these rows are used: FNDDS energy is computed with
    food-specific Atwater factors, so 4P+4C+9F does *not* reconstruct
    kcal_per_100g exactly. For 40 of these rows it misses by more than the
    enrichment validator's tolerance — spirits, which are pure ethanol the
    factors cannot see; cocoa powder and the polyol sweeteners, whose
    carbohydrate yields ~2 kcal/g inside carbohydrate-by-difference. That is
    measured, not feared. It is why core/enrichment.py must not apply its
    Atwater gate to a row copied from here — see the note there.

    There is no `state` column. FNDDS embeds preparation in the description
    ("Chicken breast, grilled without sauce"), which the model reads directly;
    the keyword `state_hint` in data/fndds_seed.csv is blank for 92% of rows and
    unreliable where it is not, so it is not worth a column that looks
    authoritative.
    """

    __tablename__ = "fndds_foods"
    __table_args__ = (
        CheckConstraint("kcal_per_100g >= 0", name="ck_fndds_kcal_nonneg"),
        Index(
            "ix_fndds_description_trgm",
            "description",
            postgresql_using="gin",
            postgresql_ops={"description": "gin_trgm_ops"},
        ),
    )

    # The USDA identifier is a stable natural key, so re-importing a later FNDDS
    # release updates rows in place instead of duplicating them.
    fdc_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    food_code: Mapped[str | None] = mapped_column(String(16))
    description: Mapped[str] = mapped_column(String(300))
    wweia_category: Mapped[str | None] = mapped_column(String(200))

    kcal_per_100g: Mapped[float] = mapped_column(Float)
    protein_g_per_100g: Mapped[float] = mapped_column(Float)
    carbs_g_per_100g: Mapped[float] = mapped_column(Float)
    fat_g_per_100g: Mapped[float] = mapped_column(Float)
    fiber_g_per_100g: Mapped[float | None] = mapped_column(Float)
    sugar_g_per_100g: Mapped[float | None] = mapped_column(Float)
    sodium_mg_per_100g: Mapped[float | None] = mapped_column(Float)
