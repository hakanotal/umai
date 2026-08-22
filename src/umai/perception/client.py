"""The vision call: schema enforcement, retries, confidence thresholds.

Stores the raw model response alongside the parsed result so old photos can be
re-scored when a better model arrives.

Runs at temperature 0 with a fixed seed. Run-to-run variance is the one error
the calibration engine cannot absorb, so any determinism the provider offers is
worth taking.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from umai.config.models import ModelClient
from umai.perception import prompt as prompt_mod
from umai.perception.images import prepare_for_model
from umai.perception.schema import PerceptionResult, request_schema

log = logging.getLogger(__name__)

# Nutrition keys the model is told not to return. If they appear anyway they are
# dropped here rather than trusted; section 4 of the plan is explicit that
# accepting them is what breaks the design.
_FORBIDDEN = ("kcal", "calories", "energy", "protein", "carbs", "carbohydrate", "fat")


class PerceptionError(RuntimeError):
    pass


@dataclass(slots=True)
class PerceptionOutcome:
    result: PerceptionResult
    model: str
    latency_ms: int
    raw: dict[str, Any]
    prompt_fingerprint: str
    discarded_nutrition: bool = False


class PerceptionClient:
    def __init__(self, models: ModelClient, task: str = "perception") -> None:
        self._models = models
        self._task = task

    async def analyse(
        self,
        photo: Path,
        ctx: prompt_mod.PromptContext,
        *,
        attempts: int = 2,
    ) -> PerceptionOutcome:
        """Read one photo. Async: the vision call takes tens of seconds and the
        caller shares its event loop with polling, the scheduler and the HTTP
        server, so it must not be run inline. Image preparation is CPU work on
        a file and goes to a thread for the same reason."""
        image_b64, media_type = await asyncio.to_thread(prepare_for_model, photo)
        user_prompt = prompt_mod.build(ctx)
        fp = prompt_mod.fingerprint(prompt_mod.SYSTEM, user_prompt)

        last_error: Exception | None = None
        for attempt in range(attempts):
            content, served, latency = await self._models.acall(
                self._task,
                messages=[
                    {"role": "system", "content": prompt_mod.SYSTEM},
                    {"role": "user", "content": user_prompt},
                ],
                schema=request_schema(),
                image_b64=image_b64,
                media_type=media_type,
            )
            try:
                raw = parse_json_object(content)
                had_nutrition = _strip_nutrition(raw)
                result = PerceptionResult.model_validate(raw)
            except (ValueError, ValidationError) as exc:
                # A malformed response is common enough on models without strict
                # structured output that retrying once is cheaper than failing.
                last_error = exc
                log.warning("perception parse failed (attempt %d): %s", attempt + 1, exc)
                continue

            return PerceptionOutcome(
                result=result,
                model=served,
                latency_ms=latency,
                raw=raw,
                prompt_fingerprint=fp,
                discarded_nutrition=had_nutrition,
            )

        raise PerceptionError(f"could not parse a response after {attempts} attempts: {last_error}")


def parse_json_object(content: str | None) -> dict[str, Any]:
    """Parse the model's reply.

    Some models wrap JSON in a fenced block despite being asked for JSON mode,
    so the first balanced object in the text is extracted as a fallback.
    """
    if not content:
        raise ValueError("empty response")
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", content, re.S)
        if not match:
            raise ValueError(f"no JSON object in response: {content[:200]!r}") from None
        parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise ValueError("response was not a JSON object")
    return parsed


def _strip_nutrition(raw: dict[str, Any]) -> bool:
    """Drop any nutrition the model volunteered. Returns whether it did.

    Worth counting rather than ignoring: a model that keeps returning calories
    is a model whose prompt is not landing, and that is a fact you want in the
    logs rather than silently swallowed.
    """
    found = False
    for item in raw.get("items") or []:
        if not isinstance(item, dict):
            continue
        for key in list(item):
            if any(f in key.lower() for f in _FORBIDDEN):
                item.pop(key)
                found = True
    return found
