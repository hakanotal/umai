"""
Umai model configuration.

One gateway (OpenRouter), three tiers, chosen per task rather than one model
for everything. Encodes the per-model capability quirks that otherwise cost
an afternoon of debugging.

Verified against the OpenRouter models API on 22 August 2026.
"""

import asyncio
import contextlib
import json
import os
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any

OPENROUTER_BASE = "https://openrouter.ai/api/v1"


# ---------------------------------------------------------------------------
# Tier definitions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelSpec:
    id: str
    price_in: float  # $ per 1M prompt tokens
    price_out: float  # $ per 1M completion tokens
    vision: bool
    structured_outputs: bool  # strict JSON schema, not just json_object
    supports_seed: bool
    supports_logprobs: bool
    context: int
    fallbacks: tuple = ()  # different lineages, tried in order
    # Function/tool calling. Only the enrichment loop needs it; verified against
    # the OpenRouter models endpoint by preflight(), which is the only thing
    # that keeps a flag like this honest.
    supports_tools: bool = False


# --- Tier A: perception (stage 1) ------------------------------------------
# The only call where model quality changes the product. Asked for items,
# state and grams. Never for calories.
PERCEPTION = ModelSpec(
    id="qwen/qwen3.8-27b",
    price_in=0.45,
    price_out=3.20,
    vision=True,
    structured_outputs=True,
    supports_seed=True,
    supports_logprobs=True,
    context=262_144,
    # deliberately different lineages so one provider incident cannot
    # take out the whole chain
    fallbacks=("google/gemini-3.7-flash", "openai/gpt-5.6-luna"),
)

# --- Tier B: utility -------------------------------------------------------
# Intent routing, resolution tiebreaks, pre-screening. Frequent and trivial.
# NOTE: qwen3.7-flash exposes response_format but NOT structured_outputs.
# Use json_object mode and validate in code. Do not hand it a strict schema.
UTILITY = ModelSpec(
    id="qwen/qwen3.7-flash",
    price_in=0.03,
    price_out=0.13,
    vision=True,
    structured_outputs=False,
    supports_seed=True,
    supports_logprobs=True,
    context=1_000_000,
    fallbacks=("inclusionai/ling-3.0-flash", "upstage/solar-pro4"),
)

# --- Tier C: coach ---------------------------------------------------------
# Summaries, weekly review, explain-why. Tone and instruction-following
# matter more here than reasoning depth.
# NOTE: glm-5.2 does not exist on OpenRouter. 5.3 is the current release.
COACH = ModelSpec(
    id="z-ai/glm-5.3",
    price_in=1.40,
    price_out=4.40,
    vision=False,
    structured_outputs=False,  # response_format only
    supports_seed=False,
    supports_logprobs=False,
    context=1_048_576,
    fallbacks=("x-ai/grok-4.6", "deepseek/deepseek-v4-pro-0813"),
    # Verified 22 August 2026: glm-5.3 lists both `tools` and `tool_choice`, as
    # do both fallbacks. The enrichment job's FNDDS lookup depends on it.
    supports_tools=True,
)

TASKS: dict[str, ModelSpec] = {
    "perception": PERCEPTION,  # photo -> items, state, grams
    "label_ocr": PERCEPTION,  # nutrition label -> foods row (transcription)
    "routing": UTILITY,  # is this a log, a question, or a correction
    "resolution": UTILITY,  # tiebreak between close `foods` candidates
    "prescreen": UTILITY,  # is this photo even food
    "coach": COACH,  # daily summary, weekly review, explain-why
    "insight": COACH,  # narrate correlations the stats layer produced
    # Composition recall for a dish the food table does not hold: "what is in
    # 100g of lahmacun". The one task where breadth of world knowledge is the
    # whole product, so it goes to the largest model rather than the cheapest,
    # runs off the critical path in a background job, and is gated hard in code
    # (analytics-free Atwater check in core/enrichment.py) before anything it
    # says reaches the foods table as a tier-4 row.
    "enrichment": COACH,
}


# ---------------------------------------------------------------------------
# Per-task request parameters
# ---------------------------------------------------------------------------

# Perception runs at temperature 0 with a fixed seed. Run-to-run variance is
# the one error the calibration engine cannot absorb, so any determinism the
# provider offers is worth taking.
#
# All three tiers are reasoning models on OpenRouter: they consume completion
# tokens on internal chain-of-thought before producing visible output. max_tokens
# limits the *total* completion (reasoning + output), so without headroom the
# model exhausts its budget thinking and returns an empty string. The values
# below leave room for both. reasoning effort is set to "low" everywhere: the
# tasks are perception and judgement, not theorem-proving, and lower effort
# means fewer reasoning tokens, lower cost, and faster responses.
PARAMS: dict[str, dict[str, Any]] = {
    "perception": {"temperature": 0.0, "seed": 42, "max_tokens": 4000},
    "label_ocr": {"temperature": 0.0, "seed": 42, "max_tokens": 3000},
    "routing": {"temperature": 0.0, "seed": 42, "max_tokens": 2000},
    "resolution": {"temperature": 0.0, "seed": 42, "max_tokens": 2000},
    "prescreen": {"temperature": 0.0, "seed": 42, "max_tokens": 1000},
    "coach": {"temperature": 0.7, "max_tokens": 3000},
    "insight": {"temperature": 0.5, "max_tokens": 2000},
    # Recall, not invention: temperature 0 so the same dish asked twice gives
    # the same composition, which is what makes a tier-4 row auditable.
    "enrichment": {"temperature": 0.0, "max_tokens": 4000},
}

# OpenRouter reasoning config, passed in extra_body. "low" effort keeps
# reasoning tokens minimal while still letting the model organise its thoughts
# before answering — which is the difference between an empty response and a
# correct one on qwen3.8-27b.
REASONING: dict[str, dict[str, Any]] = {
    "perception": {"effort": "low"},
    "label_ocr": {"effort": "low"},
    "routing": {"effort": "low"},
    "resolution": {"effort": "low"},
    "prescreen": {"effort": "low"},
    "coach": {"effort": "low"},
    "insight": {"effort": "low"},
    # The one place medium effort earns its tokens: the model is reconciling a
    # dish against its ingredients and has to make the Atwater arithmetic hang
    # together, or the validator throws the row away.
    "enrichment": {"effort": "medium"},
}


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


@dataclass
class Usage:
    calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    by_task: dict = field(default_factory=dict)


# Per-task wall-clock budget. The SDK default is ten minutes, which on a bot
# that shares one event loop means a single hung OpenRouter request takes the
# whole thing down with no log line. A vision call legitimately takes ~30s; a
# routing call that has not answered in 20 has failed.
TIMEOUTS: dict[str, float] = {
    "perception": 90.0,
    "label_ocr": 90.0,
    "routing": 25.0,
    "resolution": 25.0,
    "prescreen": 20.0,
    "coach": 60.0,
    "insight": 60.0,
    "enrichment": 120.0,
}
DEFAULT_TIMEOUT = 60.0


class ModelClient:
    """Thin OpenRouter wrapper. Handles capability quirks, fallback routing,
    the data policy, and cost accounting.

    `call` is synchronous and blocking. Async callers must use `acall`, which
    runs it on a worker thread: the OpenAI SDK client used here is the sync
    one, and calling it from a coroutine stops the event loop for the length of
    the request. On a bot sharing one loop with polling, the scheduler and the
    HTTP server, a 20-second perception call meant nothing else — not a water
    tap, not /summary — was served for 20 seconds.
    """

    def __init__(self, api_key: str | None = None, on_usage=None):
        from openai import OpenAI

        key = api_key or os.environ["OPENROUTER_API_KEY"]
        self._client = OpenAI(
            base_url=OPENROUTER_BASE,
            api_key=key,
            timeout=DEFAULT_TIMEOUT,
            # The fallback chain already covers a provider incident; SDK-level
            # retries on top of it multiply latency on the critical path.
            max_retries=1,
        )
        self.usage = Usage()
        self._on_usage = on_usage  # callback to persist into api_usage table
        # Guard usage accounting: the client may be shared across threads (the
        # batch tools run calls concurrently) and `+=` is not atomic.
        self._usage_lock = threading.Lock()
        # The loop the usage callback must be scheduled onto. `_account` runs on
        # whichever thread made the call, so a bare create_task would be a
        # no-loop error there and a silently-dropped coroutine here.
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """Tell the client which loop owns the usage callback.

        Called once from the composition root. Without it the callback is
        dropped rather than awaited, which is how api_usage stayed empty
        through a whole session of paid calls.
        """
        self._loop = loop or asyncio.get_running_loop()

    async def acall(self, task: str, **kw):
        """`call` on a worker thread. The only form an async caller may use."""
        return await asyncio.to_thread(lambda: self.call(task, **kw))

    def call(
        self,
        task: str,
        messages: list,
        schema: dict | None = None,
        image_b64: str | None = None,
        media_type: str = "image/jpeg",
    ):
        spec = TASKS[task]
        params = dict(PARAMS[task])

        if not spec.supports_seed:
            params.pop("seed", None)

        if image_b64:
            if not spec.vision:
                raise ValueError(f"{spec.id} has no vision support, task={task}")
            messages = [
                *messages,
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{media_type};base64,{image_b64}"},
                        }
                    ],
                },
            ]

        # Strict schema only where the model actually supports it. Everything
        # else gets json_object mode and is validated in code.
        if schema:
            if spec.structured_outputs:
                params["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"name": "out", "schema": schema, "strict": True},
                }
            else:
                params["response_format"] = {"type": "json_object"}
                messages = [
                    *messages,
                    {
                        "role": "system",
                        "content": "Respond with a single JSON object matching this schema. "
                        "No prose, no code fences.\n" + json.dumps(schema),
                    },
                ]

        extra = self._extra_body(task, spec)

        t0 = time.monotonic()
        resp = self._client.chat.completions.create(
            model=spec.id,
            messages=messages,
            extra_body=extra,
            timeout=TIMEOUTS.get(task, DEFAULT_TIMEOUT),
            **params,
        )
        latency_ms = int((time.monotonic() - t0) * 1000)

        served = getattr(resp, "model", "") or spec.id
        u = resp.usage
        self._account(task, served, u.prompt_tokens, u.completion_tokens, spec, latency_ms)
        return resp.choices[0].message.content, served, latency_ms

    def _extra_body(self, task: str, spec: ModelSpec) -> dict[str, Any]:
        """The OpenRouter-specific request body shared by every call shape."""
        extra: dict[str, Any] = {
            # health data. do not let providers train on it.
            "provider": {"data_collection": "deny"},
        }
        if spec.fallbacks:
            extra["models"] = list(spec.fallbacks)
            extra["route"] = "fallback"
        # All current models are reasoning models. Without this, they exhaust
        # max_tokens on internal chain-of-thought and return an empty string.
        reasoning = REASONING.get(task)
        if reasoning:
            extra["reasoning"] = reasoning
        return extra

    async def acall_tools(self, task: str, **kw):
        """`call_tools` on a worker thread. The only form an async caller may use."""
        return await asyncio.to_thread(lambda: self.call_tools(task, **kw))

    def call_tools(
        self,
        task: str,
        messages: list,
        tools: list,
        schema: dict | None = None,
    ):
        """One turn of a tool-calling conversation.

        Returns `(message, served, latency_ms)` where `message` is the raw
        response message, not its text — the caller needs `.tool_calls` as well
        as `.content`, which is the whole reason this is separate from `call`.
        Driving the loop is the caller's job: this method takes a message list
        and returns one reply, and knows nothing about how many turns are
        reasonable or when to stop.

        `call` is deliberately left alone. Its three-tuple return is depended on
        by perception, routing, resolution and the coach, and a client shared by
        the whole application is the wrong place to widen a contract for the
        benefit of one caller.

        **No `response_format` here, even when `schema` is given.** Providers
        that accept `tools` and `response_format: json_object` together are the
        exception, and a request rejected for that combination fails the whole
        enrichment tick. The schema is appended as a system message instead —
        the same fallback `call` uses for models without strict structured
        outputs — and the final turn is parsed with `parse_json_object`, which
        already tolerates prose around the object.
        """
        spec = TASKS[task]
        if not spec.supports_tools:
            raise ValueError(f"{spec.id} has no tool-calling support, task={task}")
        params = dict(PARAMS[task])
        if not spec.supports_seed:
            params.pop("seed", None)

        if schema:
            messages = [
                *messages,
                {
                    "role": "system",
                    "content": "When you have finished using tools, reply with a single JSON "
                    "object matching this schema. No prose, no code fences.\n" + json.dumps(schema),
                },
            ]

        t0 = time.monotonic()
        resp = self._client.chat.completions.create(
            model=spec.id,
            messages=messages,
            tools=tools,
            extra_body=self._extra_body(task, spec),
            timeout=TIMEOUTS.get(task, DEFAULT_TIMEOUT),
            **params,
        )
        latency_ms = int((time.monotonic() - t0) * 1000)

        served = getattr(resp, "model", "") or spec.id
        u = resp.usage
        self._account(task, served, u.prompt_tokens, u.completion_tokens, spec, latency_ms)
        return resp.choices[0].message, served, latency_ms

    def _account(self, task, served, tin, tout, spec, latency_ms):
        # If a fallback served the request the real price differs. Prefer live
        # pricing when available; otherwise the primary's price is a close
        # enough approximation for a monthly report.
        with self._usage_lock:
            p = PRICES.get(served) or {"in": spec.price_in, "out": spec.price_out}
            cost = (tin * p["in"] + tout * p["out"]) / 1e6
            self.usage.calls += 1
            self.usage.tokens_in += tin
            self.usage.tokens_out += tout
            self.usage.cost_usd += cost
            b = self.usage.by_task.setdefault(task, {"calls": 0, "cost": 0.0})
            b["calls"] += 1
            b["cost"] += cost
            if self._on_usage:
                self._dispatch_usage(
                    task=task,
                    model=served,
                    tokens_in=tin,
                    tokens_out=tout,
                    cost_usd=cost,
                    latency_ms=latency_ms,
                )

    def _dispatch_usage(self, **kw) -> None:
        """Hand the usage row to the async callback, from any thread.

        Best effort by design: a cost row that cannot be written must never
        take a user-facing call down with it.
        """
        assert self._on_usage is not None
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        with contextlib.suppress(RuntimeError):  # loop shutting down mid-call
            asyncio.run_coroutine_threadsafe(self._on_usage(**kw), loop)


# ---------------------------------------------------------------------------
# Live pricing, refreshed on start so the cost report never goes stale
# ---------------------------------------------------------------------------

PRICES: dict[str, dict] = {}


def refresh_prices(timeout: int = 20) -> dict:
    global PRICES
    try:
        with urllib.request.urlopen(f"{OPENROUTER_BASE}/models", timeout=timeout) as r:
            data = json.load(r)
    except Exception:
        return PRICES
    out = {}
    for m in data.get("data", []):
        pr = m.get("pricing") or {}
        try:
            out[m["id"]] = {
                "in": float(pr.get("prompt", 0)) * 1e6,
                "out": float(pr.get("completion", 0)) * 1e6,
            }
        except (TypeError, ValueError):
            continue
    PRICES = out
    return PRICES


def preflight() -> list[str]:
    """Run at startup. Catches a deprecated model or a silently changed
    capability before it becomes a 3am parse failure."""
    problems = []
    try:
        with urllib.request.urlopen(f"{OPENROUTER_BASE}/models", timeout=20) as r:
            live = {m["id"]: m for m in json.load(r).get("data", [])}
    except Exception as e:
        return [f"could not reach OpenRouter models endpoint: {e}"]

    for name, spec in {"perception": PERCEPTION, "utility": UTILITY, "coach": COACH}.items():
        for mid in (spec.id, *spec.fallbacks):
            m = live.get(mid)
            if not m:
                problems.append(f"{name}: {mid} no longer listed on OpenRouter")
                continue
            if mid != spec.id:
                continue
            mods = (m.get("architecture") or {}).get("input_modalities") or []
            params = m.get("supported_parameters") or []
            if spec.vision and "image" not in mods:
                problems.append(f"{name}: {mid} lost image support")
            if spec.structured_outputs and "structured_outputs" not in params:
                problems.append(f"{name}: {mid} lost structured_outputs")
            if spec.supports_tools and "tools" not in params:
                problems.append(f"{name}: {mid} lost tool calling (enrichment FNDDS lookup)")
            if spec.supports_seed and "seed" not in params:
                problems.append(
                    f"{name}: {mid} lost seed support (perception determinism is reduced)"
                )
    return problems


# ---------------------------------------------------------------------------

MONTHLY_ESTIMATE = """
Assumes 3 photos/day, 30 utility calls/day, 5 coach calls/day.

  perception  qwen/qwen3.8-27b    90 x  1800/300 tok   $0.16 /mo
  utility     qwen/qwen3.7-flash 900 x   800/100 tok   $0.03 /mo
  coach       z-ai/glm-5.3       150 x  4000/500 tok   $1.17 /mo
                                                       ------
                                                       $1.36 /mo
  plus OpenRouter's 5.5% credit fee                    $1.44 /mo

A single $20 credit purchase covers roughly 14 months.
Buy in one chunk: the fee is 5.5% with an $0.80 minimum, so a $5 top-up
costs an effective 16%.
"""

if __name__ == "__main__":
    refresh_prices()
    issues = preflight()
    print("Umai model preflight")
    for name, spec in {"perception": PERCEPTION, "utility": UTILITY, "coach": COACH}.items():
        live = PRICES.get(spec.id)
        px = (
            f"${live['in']:.3f}/${live['out']:.3f} per 1M"
            if live
            else f"${spec.price_in}/${spec.price_out} per 1M (cached)"
        )
        print(f"  {name:11s} {spec.id:24s} {px}")
    print()
    print(
        "\n".join(f"  WARNING {p}" for p in issues)
        if issues
        else "  all models present with expected capabilities"
    )
    print(MONTHLY_ESTIMATE)
