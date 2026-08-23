"""Message, photo and callback handlers, one router per feature.

`app.py` includes the single `router` below; each feature module owns its own,
so a handler lives beside the reasoning that explains it rather than in a
1,100-line file where the photo pipeline and the water button share a scroll
position.

**Two tiers, and the split is the access control.**

`GATE_ROUTERS` handle people who are not through the door: the invite phrase,
and the onboarding wizard. `APP_ROUTERS` are the application, and they sit
behind `app_router`, whose root filter is `IsActive()`. aiogram checks a
router's own filters and returns UNHANDLED *before* offering the update to any
sub-router, so that one filter gates all eleven feature routers at once.

That is the whole reason for the two-tier shape. The alternative — a filter on
each feature router — is eleven chances to forget, and the one thing an access
check may never be is forgettable. `test_router_registration.py` closes the
last hole by asserting that every module in this package which defines a
`Router` appears in one of the two tuples: a new feature router wired straight
onto `router` would otherwise bypass the filter entirely.

**Include order within each tier is load-bearing, not alphabetical.** aiogram
offers an update to routers in registration order and stops at the first match,
so:

* `gate` precedes `onboarding` — both own catch-all text handlers, and a
  pending user's message is a guess at the phrase, not an answer to a question
  they have not been asked yet.
* the gate tier precedes the app tier — the wizard consumes plain messages, and
  behind `text` a reply of "180" would be classified as a meal instead of a
  height. Both gate routers carry `NeedsGate()`, the mirror of `IsActive()`, so
  they cannot shadow anything for a user who is already through.
* `menu` precedes `text` — the reply-keyboard buttons arrive as ordinary text,
  and the invariant is that a button tap never costs a model call.
* `recipes` and `confirm` precede `text` — both own an FSM state that consumes
  a plain message; behind the catch-all, a pending ingredient or gram answer
  would be classified as a new meal instead.
* `text` is last, always. It is the catch-all, and anything registered after it
  is unreachable.

Callback routers are order-independent (their `F.data` prefixes are disjoint)
and are grouped with the command that opens them.
"""

from __future__ import annotations

from aiogram import Router

from umai.telegram.filters import IsActive
from umai.telegram.handlers import (
    commands,
    confirm,
    cuisines,
    dinnerware,
    edit,
    gate,
    library,
    menu,
    onboarding,
    photo,
    recipes,
    text,
    token,
)
from umai.telegram.handlers.menu import MENU_LABELS

# Reachable without being `active`. Anything here must be safe to show a
# stranger.
GATE_ROUTERS: tuple[Router, ...] = (
    gate.router,
    onboarding.router,
)

# The application. Order as documented above; `text` last, always.
APP_ROUTERS: tuple[Router, ...] = (
    commands.router,
    gate.admin_router,
    cuisines.router,
    dinnerware.router,
    recipes.router,
    library.router,
    menu.router,
    edit.router,
    confirm.router,
    token.router,
    photo.router,
    text.router,
)

app_router = Router(name="umai-app")
app_router.message.filter(IsActive())
app_router.callback_query.filter(IsActive())
app_router.include_routers(*APP_ROUTERS)

router = Router(name="umai")
router.include_routers(*GATE_ROUTERS, app_router)

__all__ = ["APP_ROUTERS", "GATE_ROUTERS", "MENU_LABELS", "app_router", "router"]
