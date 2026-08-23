"""Every handler router is registered where the access filter can see it.

This is the guard on the one remaining way to bypass the gate. Access is
enforced by a single `IsActive()` filter on the parent router that owns the
feature modules, which works because aiogram checks a router's own filters
before offering an update to any sub-router. A new feature router wired
directly onto the top-level `router` instead would sit *beside* that parent
rather than under it, and would answer strangers.

Nothing in a review reliably catches that: the diff would show one plausible
line in `handlers/__init__.py`, and every test of the new feature would pass.
So the check is structural — enumerate the package, and insist that every
module defining a `Router` is accounted for in one of the two declared tuples.
"""

from __future__ import annotations

import importlib
import pkgutil

from aiogram import Router

from umai.telegram import handlers
from umai.telegram.filters import IsActive, NeedsGate


def _root_filters(observer) -> list:
    """The filters `Router.filter()` registered on an event observer.

    aiogram keeps them on a private handler object rather than exposing them,
    so this is the one place that reaches inside. Worth it: the alternative is
    trusting that a line in handlers/__init__.py is still there.
    """
    return observer._handler.filters or []


def _router_modules() -> dict[str, list[Router]]:
    """Every module in `handlers` that defines at least one Router."""
    found: dict[str, list[Router]] = {}
    for info in pkgutil.iter_modules(handlers.__path__):
        module = importlib.import_module(f"{handlers.__name__}.{info.name}")
        routers = [v for v in vars(module).values() if isinstance(v, Router)]
        if routers:
            found[info.name] = routers
    return found


def test_every_router_is_registered_in_one_of_the_two_tiers():
    declared = {id(r) for r in (*handlers.GATE_ROUTERS, *handlers.APP_ROUTERS)}
    orphans = {
        f"{name}.{r.name}"
        for name, routers in _router_modules().items()
        for r in routers
        if id(r) not in declared
    }
    assert orphans == set(), (
        "these routers are defined but registered nowhere, so they are dead — "
        "or worse, were meant to be added to APP_ROUTERS and were not: "
        f"{sorted(orphans)}"
    )


def test_the_app_tier_is_gated_and_the_gate_tier_is_not():
    """The filters themselves, on the routers that carry them.

    `app_router` holds `IsActive` for both messages and callbacks — a filter on
    only one of the two would leave every inline button open to a stranger.
    """
    for observer in (handlers.app_router.message, handlers.app_router.callback_query):
        assert any(isinstance(f.callback, IsActive) for f in _root_filters(observer)), (
            "the application router must carry IsActive on messages and callbacks"
        )


def test_gate_routers_stand_aside_for_an_active_user():
    """The mirror filter. Without it the gate's catch-all text handler would
    sit in front of the whole application for everybody."""
    for router in handlers.GATE_ROUTERS:
        for observer in (router.message, router.callback_query):
            assert any(isinstance(f.callback, NeedsGate) for f in _root_filters(observer)), (
                f"{router.name} must carry NeedsGate, or it shadows the app"
            )


def test_text_is_the_last_app_router():
    """The catch-all, and anything after it is unreachable."""
    assert handlers.APP_ROUTERS[-1].name == "text"


def test_the_gate_tier_precedes_the_app_tier():
    """The wizard consumes plain messages. Behind `text`, a reply of "180" to
    the height question would be classified as a meal."""
    top = [r.name for r in handlers.router.sub_routers]
    assert top.index("gate") < top.index("umai-app")
    assert top.index("onboarding") < top.index("umai-app")


def test_the_gate_precedes_the_wizard():
    """Both own catch-all text handlers. A pending user's message is a guess at
    the invite phrase, not an answer to a question nobody has asked them."""
    names = [r.name for r in handlers.GATE_ROUTERS]
    assert names.index("gate") < names.index("onboarding")
