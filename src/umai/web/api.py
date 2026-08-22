"""Mini App backend. Telegram initData HMAC verification for auth.

Phase 1 serves two routes: the Health Auto Export ingest endpoint (token
auth, idempotent via ingest.health) and, in prod, the Telegram webhook that
feeds the dispatcher. The Mini App frontend itself is Phase 4.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
import uuid
from urllib.parse import unquote_plus

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from umai.config.settings import Settings
from umai.ingest.health import ingest

log = logging.getLogger(__name__)


def create_app(settings: Settings, dispatcher=None, bot=None) -> FastAPI:
    app = FastAPI(title="umai", docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.dispatcher = dispatcher
    app.state.bot = bot

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        """Liveness *and* the database, because the container healthcheck reads
        this. Returning ok unconditionally reported a container that could not
        reach Postgres as healthy, which is worse than having no check."""
        from sqlalchemy import text

        from umai.db.session import session_scope

        try:
            async with session_scope() as session:
                await session.execute(text("SELECT 1"))
        except Exception as exc:
            log.warning("healthz: database unreachable: %s", exc)
            return JSONResponse({"ok": False, "db": False}, status_code=503)
        return JSONResponse({"ok": True, "db": True})

    @app.post("/ingest/health")
    async def ingest_health(
        payload: dict,
        authorization: str = Header(default=""),
    ) -> JSONResponse:
        """Health Auto Export lands here. Bearer-token authenticated, and the
        idempotency lives in ingest.health rather than here so tests cover it."""
        expected = f"Bearer {settings.health_ingest_token}"
        if not settings.health_ingest_token or not hmac.compare_digest(authorization, expected):
            raise HTTPException(status_code=401, detail="bad token")

        from umai.db.session import session_scope

        async with session_scope() as session:
            user_id = await _single_user_id(session)
            report = await ingest(session, user_id, payload)
        return JSONResponse(
            {
                "written": report.written,
                "duplicates": report.duplicates,
                "rejected": report.rejected,
                "summary": report.summary,
            }
        )

    @app.post("/webhook/telegram")
    async def telegram_webhook(
        request: Request,
        x_telegram_bot_api_secret_token: str = Header(default=""),
    ) -> dict:
        if dispatcher is None or bot is None:
            raise HTTPException(status_code=404)
        expected = settings.telegram_webhook_secret
        if not expected or not hmac.compare_digest(x_telegram_bot_api_secret_token, expected):
            raise HTTPException(status_code=401, detail="bad secret")
        update = await request.json()
        from aiogram.types import Update

        await dispatcher.feed_update(bot=bot, update=Update(**update))
        return {"ok": True}

    return app


async def _single_user_id(session) -> uuid.UUID:
    """The health endpoint serves the household's one user; multi-user needs
    the Phase 6 auth story before this becomes a route parameter."""
    from sqlalchemy import select

    from umai.db.models import User

    user = (await session.execute(select(User.id).limit(1))).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=409, detail="no user registered yet")
    return user


def verify_init_data(init_data: str, bot_token: str, max_age_s: int = 86400) -> dict:
    """Telegram Mini App auth: HMAC-SHA256 of initData with the secret key
    `WebAppData`, plus a freshness check. Phase 4 uses this; it lives here now
    so the auth scheme is settled before the frontend exists."""
    # Percent-decoded before the check string is built, per Telegram's scheme.
    # The `user` field is always URL-encoded JSON, so without this the HMAC
    # could never match and every real initData would be rejected.
    pairs = {}
    for chunk in init_data.split("&"):
        key, _, value = chunk.partition("=")
        pairs[unquote_plus(key)] = unquote_plus(value)

    received = pairs.pop("hash", "")
    if not received:
        raise HTTPException(status_code=401, detail="no hash")

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    calculated = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(received, calculated):
        raise HTTPException(status_code=401, detail="bad hash")

    auth_date = int(pairs.get("auth_date", "0"))
    if time.time() - auth_date > max_age_s:
        raise HTTPException(status_code=401, detail="initData expired")
    return pairs
