"""Router-level access filters.

There is one filter here and it is applied once, to the parent router that owns
every feature module. aiogram checks a router's own filters before it offers
the update to any of its sub-routers, so a single `IsActive()` on the parent
gates all ten feature routers at once.

The alternative — a filter on each feature router — was rejected for the reason
the project already gives about the old allowlist: the one thing an access
check may never be is forgettable, and ten repetitions is ten chances to
forget. An FSM state was rejected too: aiogram's default storage is in memory,
so a restart would silently unlock everybody mid-conversation.

`handlers/__init__.py` closes the last hole. A new feature router that is
registered on the top-level router instead of the gated parent would bypass
this entirely, so a test asserts every router module appears in one of the two
declared tuples.
"""

from __future__ import annotations

from typing import Any

from aiogram.filters import BaseFilter
from aiogram.types import TelegramObject

from umai.telegram.middleware import Principal


class IsActive(BaseFilter):
    """True when the sender has finished onboarding.

    `principal` comes from `AccessMiddleware`, which runs on the update
    observer and so has always populated it by the time any filter is
    evaluated. A missing principal is treated as "not active" rather than as an
    error: if the middleware is ever unhooked, the failure should be a bot that
    answers nobody, not a bot that answers everybody.
    """

    async def __call__(self, event: TelegramObject, **data: Any) -> bool:
        principal = data.get("principal")
        return isinstance(principal, Principal) and principal.is_active


class NeedsGate(BaseFilter):
    """The complement: pending, onboarding, or no principal at all.

    Exists so the gate routers cannot shadow a feature handler for someone who
    is already active — without it, the gate's catch-all message handler would
    sit in front of the whole application.
    """

    async def __call__(self, event: TelegramObject, **data: Any) -> bool:
        principal = data.get("principal")
        return not (isinstance(principal, Principal) and principal.is_active)
