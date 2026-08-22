"""Message, photo, voice and callback handlers."""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import uuid
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, PhotoSize
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from umai.analytics import safety
from umai.clock import Clock
from umai.config.models import ModelClient
from umai.config.settings import Settings
from umai.core import agent, tools
from umai.core import cuisines as cuisines_mod
from umai.db.models import (
    EntryKind,
    EntrySource,
    Food,
    FoodItem,
    FoodState,
    LogEntry,
    Media,
)
from umai.db.session import session_scope
from umai.perception import images as image_tools
from umai.perception import prompt as prompt_mod
from umai.perception.client import PerceptionClient
from umai.resolver.compute import FoodLike
from umai.resolver.match import Resolver
from umai.telegram import keyboards

log = logging.getLogger(__name__)

router = Router(name="umai")


class Awaiting(StatesGroup):
    """The chat is waiting for one number: grams, a weigh-in, new ml, or a
    recipe ingredient."""

    number = State()
    recipe_ingredients = State()


def _sender_id(message: Message) -> int:
    """from_user is Optional in aiogram's types; the allowlist middleware has
    already rejected anything without a sender, so this assert is for mypy."""
    assert message.from_user is not None
    return message.from_user.id


def _cb_data(callback: CallbackQuery) -> str:
    assert callback.data is not None  # guarded by the F.data filters
    return callback.data


def _cb_message(callback: CallbackQuery) -> Message | None:
    """The message a callback is attached to, if it is still accessible."""
    from aiogram.types import Message as _M

    return callback.message if isinstance(callback.message, _M) else None


# --- commands ----------------------------------------------------------------


@router.message(CommandStart())
@router.message(Command("help"))
async def start(message: Message) -> None:
    await message.answer(agent.HELP, reply_markup=keyboards.main_menu())


@router.message(Command("summary"))
@router.message(Command("today"))
async def today(message: Message, settings: Settings, clock: Clock) -> None:
    async with session_scope() as session:
        user = await tools.get_or_create_user(session, settings, _sender_id(message), clock=clock)
        text = await agent.summary_line(session, user, clock)
    await message.answer(text, reply_markup=keyboards.summary_actions())


@router.message(Command("week"))
async def week(message: Message, settings: Settings, clock: Clock) -> None:
    async with session_scope() as session:
        user = await tools.get_or_create_user(session, settings, _sender_id(message), clock=clock)
        await message.answer(await agent.week_summary(session, user, clock))


@router.message(Command("edit"))
async def edit_command(message: Message, settings: Settings, clock: Clock) -> None:
    """The typed door into the edit flow. Same view as the ✏️ Edit today button."""
    async with session_scope() as session:
        user = await tools.get_or_create_user(session, settings, _sender_id(message), clock=clock)
        entries = await tools.today_entries(session, user, clock)
    if not entries:
        await message.answer("Nothing logged today yet.")
        return
    await message.answer(
        "Today's entries. Tap one to edit or remove it:",
        reply_markup=keyboards.edit_list(entries),
    )


@router.message(Command("cuisines"))
async def cuisines_command(message: Message, settings: Settings, clock: Clock) -> None:
    """Pick the cuisines you actually eat.

    Not a preference setting. The list is injected into the vision model's
    prompt, and it is the difference between "flatbread with reddish meat and
    pepper paste topping", which matches nothing in a food table and was
    logged at zero calories, and "lahmacun", which is a lookup key and, failing
    that, something the enrichment job can research.
    """
    async with session_scope() as session:
        user = await tools.get_or_create_user(session, settings, _sender_id(message), clock=clock)
        selected = list(user.cuisines or [])
    await message.answer(_CUISINE_BLURB, reply_markup=keyboards.cuisines(selected))


_CUISINE_BLURB = (
    "What do you usually eat? Tap to toggle.\n\n"
    "I hand this to the model that reads your photos, so it recognises the "
    f"dishes by name instead of describing them. Up to {cuisines_mod.MAX_CUISINES}."
)


@router.callback_query(F.data.startswith("cuisine:"))
async def cuisine_toggle(callback: CallbackQuery, settings: Settings, clock: Clock) -> None:
    slug = _cb_data(callback).split(":", 1)[1]
    attached = _cb_message(callback)
    async with session_scope() as session:
        user = await tools.get_or_create_user(session, settings, callback.from_user.id, clock=clock)
        current = list(user.cuisines or [])
        if slug in current:
            current.remove(slug)
            note = f"{cuisines_mod.label(slug)} off"
        elif len(current) >= cuisines_mod.MAX_CUISINES:
            await callback.answer(
                f"{cuisines_mod.MAX_CUISINES} is the limit — a model told it eats "
                "everything has been told nothing. Untick one first.",
                show_alert=True,
            )
            return
        else:
            current.append(slug)
            note = f"{cuisines_mod.label(slug)} on"
        # Normalised on write so the prompt is stable across sessions: an
        # unstable prompt is an unstable fingerprint, and perception_runs
        # exists to tell prompt drift from model drift.
        user.cuisines = cuisines_mod.normalise(current)
        selected = list(user.cuisines)

    if attached is not None:
        with contextlib.suppress(Exception):  # unchanged markup is a 400
            await attached.edit_reply_markup(reply_markup=keyboards.cuisines(selected))
    await callback.answer(note)


@router.callback_query(F.data == "cuisine_done")
async def cuisine_done(callback: CallbackQuery, settings: Settings, clock: Clock) -> None:
    async with session_scope() as session:
        user = await tools.get_or_create_user(session, settings, callback.from_user.id, clock=clock)
        selected = list(user.cuisines or [])
    attached = _cb_message(callback)
    named = ", ".join(cuisines_mod.label(s) for s in selected) or "nothing yet"
    if attached is not None:
        with contextlib.suppress(Exception):
            await attached.edit_text(f"Cooking with: {named}\n\nChange it any time with /cuisines.")
    await callback.answer()


# --- dinnerware calibration ---------------------------------------------------
#
# Measured once with a bank card beside the plate. The vision prompt uses these
# as the primary scale reference, roughly halving portion error (papers A04).


_DINNERWARE_BLURB = (
    "Your dinnerware, measured once so I can estimate portions from photos.\n\n"
    "To add: /dinnerware dinner plate: 26cm diameter\n"
    "To remove: tap the item below.\n\n"
    "How to measure: place a bank card (8.5cm) beside the item and compare, "
    "or use a tape measure. One measurement per item, remembered forever."
)


@router.message(Command("dinnerware"))
async def dinnerware_command(
    message: Message, settings: Settings, clock: Clock
) -> None:
    """Manage dinnerware measurements.

    Free-text add via `/dinnerware name: description`, or view and remove
    existing items via the inline list. No FSM: the format is simple enough
    to parse from a single message.
    """
    text = message.text or ""
    # Strip the command prefix and any bot mention
    raw = text.split(None, 1)[1].strip() if len(text.split(None, 1)) > 1 else ""

    if ":" in raw:
        # Add mode: "dinner plate: 26cm diameter"
        name, desc = raw.split(":", 1)
        name = name.strip()
        desc = desc.strip()
        if not name or not desc:
            await message.answer(
                "Format: /dinnerware name: description\n"
                "Example: /dinnerware dinner plate: 26cm diameter"
            )
            return
        async with session_scope() as session:
            user = await tools.get_or_create_user(
                session, settings, _sender_id(message), clock=clock
            )
            await tools.add_dinnerware(session, user.id, name, desc)
            items = await tools.list_dinnerware(session, user.id)
        await message.answer(
            f"Saved: {name}: {desc}\n\n"
            + _dinnerware_list_text(items)
        )
        return

    # View mode
    async with session_scope() as session:
        user = await tools.get_or_create_user(session, settings, _sender_id(message), clock=clock)
        items = await tools.list_dinnerware(session, user.id)
    if not items:
        await message.answer(_DINNERWARE_BLURB)
        return
    await message.answer(
        _dinnerware_list_text(items),
        reply_markup=keyboards.dinnerware_list(items),
    )


def _dinnerware_list_text(items: dict[str, str]) -> str:
    if not items:
        return "No dinnerware saved yet."
    lines = "\n".join(f"  {k}: {v}" for k, v in items.items())
    return f"Your dinnerware:\n{lines}\n\nAdd more: /dinnerware name: description"


@router.callback_query(F.data.startswith("dw:"))
async def dinnerware_remove(callback: CallbackQuery, settings: Settings, clock: Clock) -> None:
    """Remove a dinnerware item from the inline list."""
    name = _cb_data(callback).split(":", 1)[1]
    async with session_scope() as session:
        user = await tools.get_or_create_user(session, settings, callback.from_user.id, clock=clock)
        removed = await tools.remove_dinnerware(session, user.id, name)
        items = await tools.list_dinnerware(session, user.id)
    attached = _cb_message(callback)
    if attached is not None:
        if items:
            with contextlib.suppress(Exception):
                await attached.edit_text(
                    _dinnerware_list_text(items),
                    reply_markup=keyboards.dinnerware_list(items),
                )
        else:
            with contextlib.suppress(Exception):
                await attached.edit_text("All dinnerware removed.")
    await callback.answer(f"Removed {name}" if removed else "Not found")


# --- recipe creation -----------------------------------------------------------
#
# Schema, resolver and compute all exist; this wires them to chat.
# Flow: /recipe <name> → add ingredients one per message → /done to compute.


@router.message(Command("recipe"))
async def recipe_command(
    message: Message, state: FSMContext, settings: Settings, clock: Clock
) -> None:
    """Start building a recipe. Name is required, ingredients follow."""
    text = message.text or ""
    name = text.split(None, 1)[1].strip() if len(text.split(None, 1)) > 1 else ""
    if not name:
        await message.answer(
            "Give the recipe a name.\n"
            "Example: /recipe my lentil soup"
        )
        return
    await state.set_state(Awaiting.recipe_ingredients)
    await state.update_data(
        recipe_name=name,
        recipe_ingredients=[],
        recipe_cooked_grams=None,
    )
    await message.answer(
        f"Building recipe: {name}\n\n"
        "Add ingredients one at a time, like:\n"
        "  lentils 200g\n"
        "  onion 100g\n"
        "  water 500ml\n\n"
        "When done, send /done.\n"
        "To cancel, send /cancel."
    )


async def _parse_ingredient(text: str) -> tuple[str, float, str] | None:
    """Parse 'food_name 200g' or 'food_name 150ml' into (name, grams, state).

    Returns None if the text doesn't match the expected format.
    """
    import re

    _INGREDIENT_RE = re.compile(
        r"^(.+?)\s+(\d+(?:\.\d+)?)\s*(g|ml|gram|grams|kilogram|kg)\s*$", re.I
    )
    m = _INGREDIENT_RE.match(text.strip())
    if not m:
        return None
    raw_name = m.group(1).strip()
    amount = float(m.group(2))
    unit = m.group(3).lower()

    if unit in ("ml",):
        grams = amount  # density default 1.0
        state = "liquid"
    elif unit in ("kg", "kilogram"):
        grams = amount * 1000
        state = "unknown"
    else:
        grams = amount
        state = "unknown"

    return raw_name, grams, state


@router.message(Awaiting.recipe_ingredients)
async def recipe_ingredient_received(
    message: Message, state: FSMContext, settings: Settings, clock: Clock
) -> None:
    """Handle each ingredient text, or /done, or /cancel."""
    text = (message.text or "").strip()

    if text.startswith("/cancel"):
        await state.clear()
        await message.answer("Recipe discarded.")
        return

    if text.startswith("/done"):
        data = await state.get_data()
        ingredients = data.get("recipe_ingredients", [])
        if not ingredients:
            await message.answer("Add at least one ingredient first.")
            return
        await state.update_data(recipe_cooked_grams="awaiting")
        await message.answer(
            "What does the finished dish weigh in grams?\n"
            "This captures evaporation during cooking. Send just the number, "
            "or /skip to use the raw total."
        )
        return

    data = await state.get_data()
    if data and data.get("recipe_cooked_grams") == "awaiting":
        if text.startswith("/skip"):
            cooked_grams = None
        else:
            try:
                cooked_grams = float(text.replace(",", "."))
                if cooked_grams <= 0:
                    raise ValueError
            except ValueError:
                await message.answer("Send a positive number, or /skip.")
                return
        await state.update_data(recipe_cooked_grams=cooked_grams)
        await _finish_recipe(message, state, settings, clock)
        return

    parsed = await _parse_ingredient(text)
    if parsed is None:
        await message.answer(
            "I need a name and amount, like:\n"
            "  lentils 200g\n  onion 100g\n  water 500ml\n\n"
            "Or /done when finished."
        )
        return

    raw_name, grams, state_val = parsed
    ingredients = list(data.get("recipe_ingredients", []))
    ingredients.append({"name": raw_name, "grams": grams, "state": state_val})
    await state.update_data(recipe_ingredients=ingredients)
    n = len(ingredients)
    await message.answer(
        f"Added: {raw_name} {grams:.0f}g ({n} ingredient{'s' if n != 1 else ''})\n"
        "Next ingredient, or /done."
    )


async def _finish_recipe(
    message: Message,
    state: FSMContext,
    settings: Settings,
    clock: Clock,
) -> None:
    """Resolve all ingredients, create the Recipe row, compute per-100g."""
    data = await state.get_data()
    name = data["recipe_name"]
    raw_ingredients = data["recipe_ingredients"]
    cooked_grams = data.get("recipe_cooked_grams")

    async with session_scope() as session:
        user = await tools.get_or_create_user(
            session, settings, _sender_id(message), clock=clock
        )
        resolver = Resolver(session)

        # Resolve each ingredient to a food row
        resolved: list[tuple[uuid.UUID, float]] = []
        unresolved: list[str] = []
        for ing in raw_ingredients:
            resolution = await resolver.resolve(
                ing["name"], FoodState(ing["state"]), user.id
            )
            if resolution.food_id is not None:
                resolved.append((resolution.food_id, ing["grams"]))
            else:
                unresolved.append(f'{ing["name"]} {ing["grams"]:.0f}g')

        if unresolved:
            await message.answer(
                "I couldn't look up these ingredients in the food table:\n"
                + "\n".join(f"  {u}" for u in unresolved)
                + "\n\nLog them first (photo or text) so the table knows them, "
                "then try again."
            )
            await state.clear()
            return

        if not resolved:
            await message.answer("No valid ingredients found.")
            await state.clear()
            return

        # Create the Recipe row
        from umai.db.models import Recipe, RecipeIngredient

        recipe = Recipe(
            user_id=user.id,
            name=name,
            raw_input_grams=sum(g for _, g in resolved),
            cooked_output_grams=cooked_grams,
        )
        session.add(recipe)
        await session.flush()

        for food_id, grams in resolved:
            session.add(RecipeIngredient(
                recipe_id=recipe.id,
                food_id=food_id,
                grams=grams,
            ))
        await session.flush()

        # Compute per-100g profile
        foods: list[tuple[FoodLike, float]] = []
        for food_id, grams in resolved:
            food = await session.get(Food, food_id)
            if food is not None:
                foods.append((food, grams))

        from umai.resolver.compute import recipe_profile

        profile = recipe_profile(foods, cooked_output_grams=cooked_grams)
        recipe.kcal_per_100g = profile.kcal_per_100g
        recipe.protein_g_per_100g = profile.protein_g_per_100g
        recipe.carbs_g_per_100g = profile.carbs_g_per_100g
        recipe.fat_g_per_100g = profile.fat_g_per_100g

    await state.clear()
    lines = "\n".join(
        f"  {ing['name']} {ing['grams']:.0f}g" for ing in raw_ingredients
    )
    total_g = sum(ing["grams"] for ing in raw_ingredients)
    yield_note = ""
    if cooked_grams:
        yield_note = f"\nCooked: {cooked_grams:.0f}g (yield {cooked_grams / total_g:.0%})"
    await message.answer(
        f"Recipe: {name}\n\n"
        f"{lines}\n\n"
        f"Total: {total_g:.0f}g{yield_note}\n"
        f"Per 100g: {profile.kcal_per_100g:.0f} kcal, "
        f"P {profile.protein_g_per_100g:.0f}g, "
        f"C {profile.carbs_g_per_100g:.0f}g, "
        f"F {profile.fat_g_per_100g:.0f}g\n\n"
        "Now if I see this dish in a photo I'll recognise it by name."
    )


# --- food library one-tap ------------------------------------------------------
#
# The library fills as the user logs. This surfaces the most frequent items
# for one-tap re-logging with the typical portion.


@router.message(Command("library"))
async def library_command(
    message: Message, settings: Settings, clock: Clock
) -> None:
    """Show the user's most frequent foods for one-tap re-logging."""
    async with session_scope() as session:
        user = await tools.get_or_create_user(
            session, settings, _sender_id(message), clock=clock
        )
        items = await tools.user_library(session, user.id)
    if not items:
        await message.answer(
            "Your food library is empty. Log a few meals and I'll remember "
            "the ones you eat often."
        )
        return
    await message.answer(
        "Tap to log again with your usual portion:",
        reply_markup=keyboards.library_items(items),
    )


@router.callback_query(F.data.startswith("lib:"))
async def library_quick_log(
    callback: CallbackQuery, settings: Settings, clock: Clock, models: ModelClient
) -> None:
    """One-tap log from the library. Uses the typical portion."""
    food_id_str = _cb_data(callback).split(":", 1)[1]
    try:
        food_id = uuid.UUID(food_id_str)
    except ValueError:
        await callback.answer("Invalid item.", show_alert=True)
        return

    async with session_scope() as session:
        user = await tools.get_or_create_user(
            session, settings, callback.from_user.id, clock=clock
        )
        food = await session.get(Food, food_id)
        if food is None:
            await callback.answer("That food no longer exists.", show_alert=True)
            return

        # Get typical grams from portion priors, default to 100g
        priors = await tools.portion_priors(session, user.id)
        typical = priors.get(food.canonical_name_en, (100.0, 100.0, 100.0))[0]

        from umai.db.models import GramsSource, ResolutionMethod
        from umai.resolver.match import Resolution

        resolution = Resolution(
            food_id=food_id,
            recipe_id=None,
            display_name=food.canonical_name_en,
            method=ResolutionMethod.library,
            confidence=1.0,
        )
        meal = await tools.log_food_items(
            session,
            user.id,
            [
                tools.ItemToLog(
                    detected_name=food.canonical_name_en,
                    detected_state=FoodState(food.state.value if food.state else "unknown"),
                    grams=typical,
                    grams_source=GramsSource.user,
                    grams_confidence=1.0,
                    resolution=resolution,
                )
            ],
            occurred_at=clock.now(),
            source=EntrySource.button,
        )
    await callback.answer(f"Logged {food.canonical_name_en} ({typical:.0f}g)")
    # Update the message with the logged result
    attached = _cb_message(callback)
    if attached is not None:
        with contextlib.suppress(Exception):
            await attached.edit_text(tools.format_meal(meal))


# --- the persistent menu (reply keyboard) ---------------------------------------
#
# The buttons arrive as ordinary text. They are matched by exact label before
# the intent classifier sees anything, which keeps the invariant that a button
# tap never costs a model call.


_MENU_LABELS = {
    keyboards.WATER_250,
    keyboards.WATER_500,
    keyboards.BTN_TODAY,
    keyboards.BTN_EDIT,
    keyboards.BTN_WEIGH,
}


@router.message(F.text.in_(_MENU_LABELS))
async def menu_button(
    message: Message, state: FSMContext, settings: Settings, clock: Clock
) -> None:
    assert message.text is not None  # the F.text filter guarantees it
    label = message.text
    if label in (keyboards.WATER_250, keyboards.WATER_500):
        ml = 250.0 if label == keyboards.WATER_250 else 500.0
        async with session_scope() as session:
            user = await tools.get_or_create_user(
                session, settings, _sender_id(message), clock=clock
            )
            await tools.log_simple(
                session,
                user.id,
                kind=EntryKind.water,
                value=ml,
                unit="ml",
                occurred_at=clock.now(),
            )
        await message.answer(f"Water logged: {ml:.0f} ml 💧")
        return
    if label == keyboards.BTN_TODAY:
        async with session_scope() as session:
            user = await tools.get_or_create_user(
                session, settings, _sender_id(message), clock=clock
            )
            text = await agent.summary_line(session, user, clock)
        await message.answer(text, reply_markup=keyboards.summary_actions())
        return
    if label == keyboards.BTN_EDIT:
        async with session_scope() as session:
            user = await tools.get_or_create_user(
                session, settings, _sender_id(message), clock=clock
            )
            entries = await tools.today_entries(session, user, clock)
        if not entries:
            await message.answer("Nothing logged today yet.")
            return
        await message.answer(
            "Today's entries. Tap one to edit or remove it:",
            reply_markup=keyboards.edit_list(entries),
        )
        return
    # BTN_WEIGH
    await state.set_state(Awaiting.number)
    await state.update_data(mode="weight")
    await message.answer("Send me the number on the scale ⚖️")


# --- edit and remove today's entries ---------------------------------------------


@router.callback_query(F.data == "editlist")
async def edit_list_open(callback: CallbackQuery, settings: Settings, clock: Clock) -> None:
    attached = _cb_message(callback)
    if attached is None:
        await callback.answer("This list is out of date. Tap ✏️ Edit today again.", show_alert=True)
        return
    async with session_scope() as session:
        user = await tools.get_or_create_user(session, settings, callback.from_user.id, clock=clock)
        entries = await tools.today_entries(session, user, clock)
    if not entries:
        await callback.answer("Nothing logged today yet.", show_alert=True)
        return
    with contextlib.suppress(Exception):  # unchanged markup is a 400
        await attached.edit_text(
            "Today's entries. Tap one to edit or remove it:",
            reply_markup=keyboards.edit_list(entries),
        )
    await callback.answer()


@router.callback_query(F.data == "editclose")
async def edit_list_close(callback: CallbackQuery) -> None:
    attached = _cb_message(callback)
    if attached is not None:
        with contextlib.suppress(Exception):
            await attached.edit_text("Closed. Tap ✏️ Edit today any time.")
    await callback.answer()


@router.callback_query(F.data.startswith("edit:"))
async def edit_entry_open(
    callback: CallbackQuery, state: FSMContext, settings: Settings, clock: Clock
) -> None:
    """The per-entry view: a meal gets per-item gram fixes and removal,
    water gets a new amount and removal."""
    attached = _cb_message(callback)
    if attached is None:
        await callback.answer("This list is out of date. Tap ✏️ Edit today again.", show_alert=True)
        return
    await state.clear()
    prefix = _cb_data(callback).split(":", 1)[1]
    async with session_scope() as session:
        user = await tools.get_or_create_user(session, settings, callback.from_user.id, clock=clock)
        entry = await _live_entry_by_prefix(session, user.id, prefix)
        if entry is None:
            await callback.answer("That entry is gone. Tap ↩ Back for the list.", show_alert=True)
            return
        text = tools.format_entry(user, entry)
        if entry.kind is EntryKind.water:
            markup = keyboards.edit_water(prefix)
        else:
            markup = keyboards.edit_meal(prefix, len(entry.items))
    with contextlib.suppress(Exception):
        await attached.edit_text(text, reply_markup=markup)
    await callback.answer()


@router.callback_query(F.data.startswith("del:"))
async def delete_ask(callback: CallbackQuery, state: FSMContext) -> None:
    attached = _cb_message(callback)
    if attached is None:
        await callback.answer("This list is out of date. Tap ✏️ Edit today again.", show_alert=True)
        return
    await state.clear()
    prefix = _cb_data(callback).split(":", 1)[1]
    with contextlib.suppress(Exception):
        await attached.edit_text(
            "Remove this entry? This cannot be undone.",
            reply_markup=keyboards.delete_confirm(prefix),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("delyes:"))
async def delete_confirm(callback: CallbackQuery, settings: Settings, clock: Clock) -> None:
    attached = _cb_message(callback)
    if attached is None:
        await callback.answer("This list is out of date. Tap ✏️ Edit today again.", show_alert=True)
        return
    prefix = _cb_data(callback).split(":", 1)[1]
    async with session_scope() as session:
        user = await tools.get_or_create_user(session, settings, callback.from_user.id, clock=clock)
        entry = await _live_entry_by_prefix(session, user.id, prefix)
        if entry is not None:
            await tools.hard_delete_entry(session, user.id, entry.id)
    with contextlib.suppress(Exception):
        await attached.edit_text("Removed ✅")
    await callback.answer("Removed")


@router.callback_query(F.data.startswith("waterfix:"))
async def water_fix(callback: CallbackQuery, state: FSMContext) -> None:
    attached = _cb_message(callback)
    if attached is None:
        await callback.answer("This list is out of date. Tap ✏️ Edit today again.", show_alert=True)
        return
    prefix = _cb_data(callback).split(":", 1)[1]
    await state.set_state(Awaiting.number)
    await state.update_data(mode="water_edit", entry_prefix=prefix)
    await attached.answer("Send me the new amount in ml.")
    await callback.answer()


async def _live_entry_by_prefix(
    session: AsyncSession, user_id: uuid.UUID, prefix: str
) -> LogEntry | None:
    """Resolve an 8-character prefix to a live entry of any editable kind.

    Same scoping reasoning as _entry_by_prefix: callback data is short by
    necessity, and an unscoped scan is a cross-user read waiting for the day
    a second user exists. Items are eager-loaded because the meal view reads
    them after the session's work is otherwise done.
    """
    stmt = (
        select(LogEntry)
        .options(selectinload(LogEntry.items))
        .where(
            LogEntry.user_id == user_id,
            LogEntry.kind.in_((EntryKind.food, EntryKind.drink, EntryKind.water)),
            LogEntry.superseded_by.is_(None),
        )
        .order_by(LogEntry.logged_at.desc())
        .limit(50)
    )
    for row in (await session.execute(stmt)).scalars():
        if str(row.id).startswith(prefix):
            return row
    return None


# --- meal confirmation ---------------------------------------------------------


@router.callback_query(F.data.startswith("fix:"))
async def fix_item(callback: CallbackQuery, state: FSMContext) -> None:
    attached = _cb_message(callback)
    if attached is None:
        await callback.answer(
            "Too old to edit. Log it fresh, or try ✏️ Edit today.", show_alert=True
        )
        return
    _, entry_prefix, item_no = _cb_data(callback).split(":")
    await state.set_state(Awaiting.number)
    await state.update_data(mode="grams", entry_prefix=entry_prefix, item_no=int(item_no))
    await attached.answer(f"How many grams was item {item_no}?")
    await callback.answer()


@router.callback_query(F.data.startswith("ok:"))
async def meal_ok(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("Logged 👍")


@router.message(Awaiting.number, F.text)
async def number_received(
    message: Message, state: FSMContext, settings: Settings, clock: Clock
) -> None:
    data = await state.get_data()
    assert message.text is not None  # the F.text filter guarantees it
    try:
        value = float(message.text.strip().replace(",", "."))
    except ValueError:
        await message.answer("Just the number, please. Or tap ✏️ Edit today to pick something else.")
        return

    async with session_scope() as session:
        user = await tools.get_or_create_user(session, settings, _sender_id(message), clock=clock)
        if data.get("mode") == "weight":
            if not safety.is_plausible_weight(value):
                await message.answer(
                    f"{value} doesn't look like a body weight. Try again, or send something else."
                )
                return
            await tools.log_simple(
                session,
                user.id,
                kind=EntryKind.weight,
                value=value,
                unit="kg",
                occurred_at=clock.now(),
                source=EntrySource.button,
            )
            await state.clear()
            await message.answer(f"Logged {value:.1f} kg ⚖️")
            return

        if data.get("mode") == "water_edit":
            if value <= 0:
                await message.answer(
                    "The amount needs to be above zero. Send a number in ml, or tap ↩ Back."
                )
                return
            entry = await _live_entry_by_prefix(session, user.id, data["entry_prefix"])
            if entry is None or entry.kind is not EntryKind.water:
                await state.clear()
                await message.answer("That water entry is gone. Tap ✏️ Edit today for the list.")
                return
            new_entry = await tools.edit_water(session, user.id, entry.id, value)
            if new_entry is None:
                await state.clear()
                await message.answer("That water entry is gone. Tap ✏️ Edit today for the list.")
                return
            await state.clear()
            await message.answer(f"Water updated: {value:.0f} ml 💧")
            return

        entry_id = await _entry_by_prefix(session, user.id, data["entry_prefix"])
        if entry_id is None:
            await state.clear()
            await message.answer("That meal is too old to edit. Log it fresh, or tap ✏️ Edit today.")
            return

        meal = await _fix_item_grams(session, entry_id, data["item_no"], value)
        if meal is None:
            await state.clear()
            await message.answer("I couldn't find that item. Tap ✏️ Edit today for the list.")
            return

        # The correction supersedes the entry, so the buttons under the old
        # message now point at a superseded id and every further fix would be
        # refused as "too old to edit". Re-send the meal with a live keyboard.
        text = tools.format_meal(meal)
        n_items = len(meal.items)
    await state.clear()
    await message.answer(text, reply_markup=keyboards.meal_actions(str(meal.entry.id), n_items))


async def _entry_by_prefix(
    session: AsyncSession, user_id: uuid.UUID, prefix: str
) -> uuid.UUID | None:
    """Resolve an 8-character prefix back to a meal entry.

    Scoped to this user: callback data is short by necessity (Telegram caps it
    at 64 bytes) and an unscoped prefix scan is a cross-user read waiting for
    the day a second user exists.
    """
    entry = await _live_entry_by_prefix(session, user_id, prefix)
    if entry is None or entry.kind not in (EntryKind.food, EntryKind.drink):
        return None
    return entry.id


async def _fix_item_grams(
    session: AsyncSession, entry_id: uuid.UUID, item_no: int, grams: float
) -> tools.LoggedMeal | None:
    """Position order, the same order the confirmation message numbered.

    Returns the replacement meal, or None when the item number does not exist
    or the entry was already superseded — both of which used to be reported to
    the user as "Fixed." with nothing having changed.
    """
    items = (
        (
            await session.execute(
                select(FoodItem).where(FoodItem.entry_id == entry_id).order_by(FoodItem.position)
            )
        )
        .scalars()
        .all()
    )
    if not (1 <= item_no <= len(items)):
        return None
    return await tools.supersede_with_grams(session, entry_id, {items[item_no - 1].id: grams})


# --- the photo path --------------------------------------------------------------


@router.message(F.photo)
async def photo(
    message: Message,
    state: FSMContext,
    settings: Settings,
    clock: Clock,
    models: ModelClient,
    perception: PerceptionClient,
) -> None:
    """The four-stage pipeline. The happy path is one reply, a few seconds.

    Structured so that no database transaction is open across the vision call.
    The first version held one for the whole 17-23 seconds, which on a
    five-connection pool is a connection parked on a network round trip and a
    `logged_at` (Postgres `now()`, i.e. transaction start) that predated the
    `occurred_at` it was supposed to follow.
    """
    # A photo answers whatever question was pending; leaving the state set means
    # the next plain number is eaten by the gram prompt instead of being logged.
    await state.clear()

    status: Message | None = await message.answer("Looking…")
    try:
        sizes = message.photo
        if not sizes:
            await message.answer("That photo looks empty — try again?")
            return
        path = await _download_with_retry(message, sizes[-1], settings)
        if path is None:
            await message.answer("Couldn't fetch the photo from Telegram. Please try again.")
            return

        # 1. read what is needed for the prompt, then close the transaction.
        async with session_scope() as session:
            user = await tools.get_or_create_user(
                session, settings, _sender_id(message), clock=clock
            )
            user_id, tz, cuisines = user.id, user.tz, list(user.cuisines or [])
            ctx = prompt_mod.PromptContext(
                dinnerware=await _dinnerware(session, user_id),
                portion_priors=await tools.portion_priors(session, user_id),
                cuisines=cuisines,
                local_time=clock.now().astimezone(ZoneInfo(tz)).strftime("%A %H:%M"),
                note=message.caption or None,
            )
            media_id = await _record_media(session, path, tz)

        # 2. the slow part, with nothing held.
        try:
            outcome = await perception.analyse(path, ctx)
        except Exception:
            log.exception("perception failed")
            await message.answer("I couldn't read that photo. Another angle might help.")
            return

        # 3. resolve, write, reply.
        async with session_scope() as session:
            user = await tools.get_or_create_user(
                session, settings, _sender_id(message), clock=clock
            )
            logged = await agent.log_photo(
                session=session,
                models=models,
                user=user,
                clock=clock,
                outcome=outcome,
                media_id=media_id,
                caption=message.caption or None,
            )
        text = logged.text
        if outcome.result.clarifying_question:
            text += f"\n❓ {outcome.result.clarifying_question}"
        await message.answer(
            text,
            reply_markup=keyboards.meal_actions(str(logged.entry_id), len(outcome.result.items)),
        )
        if logged.needs_enrichment:
            # Nudge the background researcher rather than making the user wait
            # for it. Fire and forget by design: its failure is not this
            # message's problem, and the scheduled tick will pick the gap up.
            _nudge_enrichment(models, clock)
    finally:
        if status is not None:
            with contextlib.suppress(Exception):
                await status.delete()


async def _record_media(session: AsyncSession, path: Path, tz: str) -> uuid.UUID:
    """Insert the media row, or return the existing one for this file.

    sha256 is unique: the same photo sent twice is one media row, and the
    `entry_id` on it is filled in by `agent.log_photo` once the entry it
    produced exists. Both media rows from the first live session had a null
    entry_id, which made "re-score this old photo" — the reason the file is
    kept at all — impossible.
    """
    digest = await asyncio.to_thread(image_tools.sha256_of, path)
    taken = await asyncio.to_thread(_taken_at_local, path, tz)
    stmt = (
        pg_insert(Media)
        .values(id=uuid.uuid4(), path=str(path), sha256=digest, taken_at=taken)
        .on_conflict_do_nothing(index_elements=["sha256"])
        .returning(Media.id)
    )
    media_id = (await session.execute(stmt)).scalar_one_or_none()
    if media_id is None:
        media_id = (
            await session.execute(select(Media.id).where(Media.sha256 == digest))
        ).scalar_one()
    return media_id


def _nudge_enrichment(models: ModelClient, clock: Clock) -> None:
    """Ask the enrichment job to run now rather than at its next tick.

    Deliberately not awaited and deliberately swallowing everything: the user's
    meal is already logged and their reply already sent, and a provider outage
    in a background researcher must not surface as a failed food log.
    """

    async def _run() -> None:
        from umai.core import enrichment
        from umai.db.session import get_factory

        try:
            report = await enrichment.enrich_once(get_factory(), models, clock)
            if report.did_work:
                log.info("enrichment nudge: %s", enrichment.summarise(report))
        except Exception:
            log.exception("enrichment nudge failed")

    task = asyncio.create_task(_run())
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)


# Strong references to fire-and-forget tasks. Without this the event loop only
# holds a weak reference and the task can be garbage collected mid-flight.
_BACKGROUND: set[asyncio.Task[None]] = set()


async def _download_with_retry(
    message: Message, photo: PhotoSize, settings: Settings
) -> Path | None:
    """Telegram file downloads are two calls (getFile then fetch) and both can
    fail independently, so the pair is retried together (gotcha list §12)."""
    bot = message.bot
    if bot is None:
        return None
    destination = Path(settings.media_dir)
    destination.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240 - local disk, microseconds
    path = destination / f"{photo.file_unique_id}.jpg"
    if path.exists():
        return path
    for attempt in range(2):
        try:
            await bot.download(photo, destination=path)
            return path
        except Exception:
            log.warning("photo download attempt %d failed", attempt + 1)
    return None


def _taken_at_local(path: Path, tz: str) -> dt.datetime | None:
    """EXIF carries naive camera-local time; attach the user's timezone here,
    in one place, then store UTC."""
    when = image_tools.taken_at(path)
    if when is None:
        return None
    return when.replace(tzinfo=ZoneInfo(tz)).astimezone(dt.UTC)


async def _dinnerware(session: AsyncSession, user_id: uuid.UUID) -> dict[str, str]:
    from umai.db.models import Dinnerware

    rows = (
        await session.execute(select(Dinnerware).where(Dinnerware.user_id == user_id))
    ).scalars()
    return {d.name: d.description for d in rows}


# --- free text -----------------------------------------------------------------


@router.message(F.text)
async def text(message: Message, settings: Settings, clock: Clock, models: ModelClient) -> None:
    assert message.text is not None  # the F.text filter guarantees it
    async with session_scope() as session:
        user = await tools.get_or_create_user(session, settings, _sender_id(message), clock=clock)
        try:
            reply = await agent.handle_text(
                session=session, models=models, user=user, clock=clock, text=message.text
            )
        except Exception:
            log.exception("text handling failed")
            reply = agent.Reply("Something went wrong reading that. Please try again.")

    # A typed meal gets the same per-item correction keyboard a photographed
    # one gets. Without it "200g rice and chicken" was uncorrectable while the
    # reply suggested a /fix command that was never registered.
    #
    # Non-meal replies carry no keyboard at all: the persistent reply keyboard
    # already holds the quick actions, and repeating them inline under every
    # message made the chat a wall of duplicate buttons.
    markup = None
    if reply.entry_id is not None:
        async with session_scope() as session:
            count = await _item_count(session, reply.entry_id)
        markup = keyboards.meal_actions(str(reply.entry_id), count)
    await message.answer(reply.text, reply_markup=markup)
    if reply.needs_enrichment:
        _nudge_enrichment(models, clock)


async def _item_count(session: AsyncSession, entry_id: uuid.UUID) -> int:
    return int(
        (
            await session.execute(
                select(func.count()).select_from(FoodItem).where(FoodItem.entry_id == entry_id)
            )
        ).scalar_one()
    )
