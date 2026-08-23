"""Message, photo and callback handlers, one router per feature.

`app.py` includes the single `router` below; each feature module owns its own,
so a handler lives beside the reasoning that explains it rather than in a
1,100-line file where the photo pipeline and the water button share a scroll
position.

**Include order is load-bearing, not alphabetical.** aiogram offers an update
to routers in registration order and stops at the first match, so:

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

from umai.telegram.handlers import (
    commands,
    confirm,
    cuisines,
    dinnerware,
    edit,
    library,
    menu,
    photo,
    recipes,
    text,
)
from umai.telegram.handlers.menu import MENU_LABELS

router = Router(name="umai")
router.include_routers(
    commands.router,
    cuisines.router,
    dinnerware.router,
    recipes.router,
    library.router,
    menu.router,
    edit.router,
    confirm.router,
    photo.router,
    text.router,
)

__all__ = ["MENU_LABELS", "router"]
