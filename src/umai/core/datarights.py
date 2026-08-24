"""Give a person everything you hold on them, or destroy it.

Umai holds health data. The moment it holds a *second* person's health data,
that stops being a private notebook and becomes special-category data under
GDPR Article 9 — the project plan says so itself and calls the compliance
burden real. Two of the obligations are concrete enough to just implement:
access, and erasure.

Kept out of the handlers because neither is really a chat feature. Export is a
walk over every per-user table, and deletion is a question about foreign keys;
both want testing without Telegram in the way.

**What is not exported.** `foods` and `fndds_foods` are a shared composition
table, not personal data — a person's rows reference them, and the reference is
exported, but the table itself belongs to the deployment. Tier-4 rows the
enrichment job invented while researching somebody's meal stay too: they are
the property the whole trust-tier design rests on, that fixing one row
retroactively corrects everyone.

**What deletion cannot rely on.** Twelve tables cascade from `users`, which
does most of the work. Two do not. `corrections` references `log_entries` and
`food_items` with no ON DELETE clause at all, so it would raise a foreign-key
violation rather than follow; and `perception_runs.entry_id` is ON DELETE SET
NULL, which would leave the raw model response — a description of what somebody
ate — sitting in the table with its owner removed. Both are deleted explicitly,
before the cascade, and `test_data_rights.py` is what stops the next FK added
here from being forgotten.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from umai.db.models import (
    Correction,
    Dinnerware,
    FoodItem,
    FoodLibrary,
    HealthMetric,
    LogEntry,
    Media,
    PerceptionRun,
    PortionPrior,
    Recipe,
    RecipeIngredient,
    User,
)


def _rows(obj: Any) -> dict[str, Any]:
    """One ORM row as plain JSON-able values.

    Reads the mapped columns rather than `__dict__`, so a lazily-unloaded
    attribute cannot silently export as missing, and uuid/date/Decimal become
    strings rather than exploding the encoder.
    """
    out: dict[str, Any] = {}
    for column in obj.__table__.columns:
        value = getattr(obj, column.name)
        if isinstance(value, uuid.UUID):
            value = str(value)
        elif hasattr(value, "isoformat"):
            value = value.isoformat()
        elif isinstance(value, list | tuple):
            value = list(value)
        elif value is not None and not isinstance(value, str | int | float | bool | dict):
            value = str(value)
        out[column.name] = value
    return out


async def export(session: AsyncSession, user_id: uuid.UUID) -> dict[str, Any]:
    """Everything Umai holds on one person, as a JSON-able dict.

    Entries carry their items inline rather than as a parallel table, because
    the question a person actually has is "what did I eat", and a flat list of
    `food_items` keyed by uuid answers it only after a join they should not
    have to do.
    """
    user = await session.get(User, user_id)
    if user is None:  # pragma: no cover - the caller holds a Principal
        raise RuntimeError(f"no user row for {user_id}")
    # `users.updated_at` carries an `onupdate`, so any write to the row in this
    # session leaves the attribute expired — and reading an expired attribute
    # under asyncio raises MissingGreenlet rather than lazily loading. Every
    # other table here is read by a fresh `select`, so this is the only row
    # that needs saying out loud.
    await session.refresh(user)

    profile = _rows(user)
    # The bearer credential for their health endpoint. Exporting it would put a
    # live secret into a file that travels through a chat and onto a laptop,
    # and it is not information about them — it is an access key.
    profile.pop("health_token", None)

    entries = (
        (
            await session.execute(
                select(LogEntry).where(LogEntry.user_id == user_id).order_by(LogEntry.occurred_at)
            )
        )
        .scalars()
        .all()
    )
    items_by_entry: dict[uuid.UUID, list[dict[str, Any]]] = {}
    if entries:
        for item in (
            (
                await session.execute(
                    select(FoodItem)
                    .where(FoodItem.entry_id.in_([e.id for e in entries]))
                    .order_by(FoodItem.position)
                )
            )
            .scalars()
            .all()
        ):
            items_by_entry.setdefault(item.entry_id, []).append(_rows(item))

    recipes = (
        (await session.execute(select(Recipe).where(Recipe.user_id == user_id))).scalars().all()
    )
    ingredients_by_recipe: dict[uuid.UUID, list[dict[str, Any]]] = {}
    if recipes:
        for ing in (
            (
                await session.execute(
                    select(RecipeIngredient).where(
                        RecipeIngredient.recipe_id.in_([r.id for r in recipes])
                    )
                )
            )
            .scalars()
            .all()
        ):
            ingredients_by_recipe.setdefault(ing.recipe_id, []).append(_rows(ing))

    async def _scoped(model: Any) -> list[dict[str, Any]]:
        rows = (
            (await session.execute(select(model).where(model.user_id == user_id))).scalars().all()
        )
        return [_rows(r) for r in rows]

    return {
        "profile": profile,
        "entries": [{**_rows(e), "items": items_by_entry.get(e.id, [])} for e in entries],
        "recipes": [
            {**_rows(r), "ingredients": ingredients_by_recipe.get(r.id, [])} for r in recipes
        ],
        "health_metrics": await _scoped(HealthMetric),
        "food_library": await _scoped(FoodLibrary),
        "portion_priors": await _scoped(PortionPrior),
        "dinnerware": await _scoped(Dinnerware),
        "photos": await _scoped(Media),
    }


def entries_csv(exported: dict[str, Any]) -> str:
    """The entries as a flat spreadsheet.

    The JSON is complete and the CSV is legible, and those are different jobs.
    Somebody who wants to see what they ate opens this in a spreadsheet;
    somebody who wants to move their data elsewhere parses the JSON.
    """
    import csv
    import io

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        ["occurred_at", "kind", "source", "item", "grams", "kcal", "protein_g", "carbs_g", "fat_g"]
    )
    for entry in exported["entries"]:
        if not entry["items"]:
            writer.writerow(
                [entry["occurred_at"], entry["kind"], entry["source"], "", "", "", "", "", ""]
            )
            continue
        for item in entry["items"]:
            writer.writerow(
                [
                    entry["occurred_at"],
                    entry["kind"],
                    entry["source"],
                    item.get("detected_name") or "",
                    item.get("grams") or "",
                    item.get("kcal") or "",
                    item.get("protein_g") or "",
                    item.get("carbs_g") or "",
                    item.get("fat_g") or "",
                ]
            )
    return buf.getvalue()


async def erase(session: AsyncSession, user_id: uuid.UUID) -> None:
    """Remove a person and everything of theirs.

    The order is the point. Twelve tables cascade from `users` and would follow
    on their own, but two would not:

      * `corrections` references `log_entries` and `food_items` with no
        ON DELETE clause, so the cascade would hit a foreign-key violation and
        the whole deletion would fail.
      * `perception_runs.entry_id` is ON DELETE SET NULL, so the raw model
        response — which describes what somebody ate — would survive with its
        owner removed and no way left to tell whose it was.

    So both are removed explicitly first, then the single DELETE does the rest.
    Media files on disk are the caller's job: they are not in the database, and
    the handler knows where MEDIA_DIR is.
    """
    entry_ids = select(LogEntry.id).where(LogEntry.user_id == user_id)
    item_ids = select(FoodItem.id).where(FoodItem.entry_id.in_(entry_ids))

    await session.execute(
        delete(Correction).where(
            Correction.entry_id.in_(entry_ids) | Correction.food_item_id.in_(item_ids)
        )
    )
    await session.execute(
        delete(PerceptionRun).where(
            PerceptionRun.entry_id.in_(entry_ids)
            | PerceptionRun.media_id.in_(select(Media.id).where(Media.user_id == user_id))
        )
    )

    user = await session.get(User, user_id)
    if user is not None:
        await session.delete(user)
    await session.flush()
