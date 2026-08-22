"""Matplotlib, Agg backend. Headless PNG rendering into Telegram.

Charts are push, not pull: the daily summary and the weight trend arrive
without being asked for. Every chart is derived entirely from data the code
already holds — a chart is never a place where a number gets invented.
"""

from __future__ import annotations

import datetime as dt
import io

import matplotlib

matplotlib.use("Agg")  # headless, before pyplot import
import matplotlib.figure
import matplotlib.pyplot as plt


def _png(fig: matplotlib.figure.Figure) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


def week_kcal(
    days: list[dt.date],
    kcal: list[float],
    target: float | None,
    protein: list[float] | None = None,
    protein_target: float | None = None,
) -> bytes:
    """The daily summary chart: a fortnight of intake against target.

    A dot for each day, a line for the target. Days with nothing logged are
    shown as gaps rather than zeros — an unlogged day is not a zero-calorie
    day, and drawing it as one would flatter the trend."""
    fig, ax = plt.subplots(figsize=(7, 3.2))
    xs = [d.toordinal() for d in days]
    ax.bar(xs, kcal, width=0.7, color="#2563eb", alpha=0.85, label="logged kcal")
    if target:
        ax.axhline(target, color="#dc2626", lw=1.2, ls="--", label=f"target {target:.0f}")
    ax.set_xticks(xs)
    ax.set_xticklabels([d.strftime("%a") if len(days) <= 9 else f"{d.day}" for d in days])
    ax.set_ylabel("kcal")
    ax.spines[["top", "right"]].set_visible(False)
    ax.legend(frameon=False, fontsize=8)
    return _png(fig)


def weight_trend(days: list[dt.date], raw: list[float | None], ewma: list[float]) -> bytes:
    """Raw readings as faint dots, the EWMA as the line. The trend is the
    product; the dots are there to show what it is made of."""
    fig, ax = plt.subplots(figsize=(7, 3.2))
    xs_raw = [d.toordinal() for d, r in zip(days, raw, strict=True) if r is not None]
    ys_raw = [r for r in raw if r is not None]
    ax.scatter(xs_raw, ys_raw, s=12, color="#9ca3af", label="weigh-ins")
    ax.plot(
        [d.toordinal() for d in days],
        ewma,
        color="#059669",
        lw=2,
        label="trend",
    )
    ax.set_ylabel("kg")
    ax.set_xticks([d.toordinal() for d in days][:: max(1, len(days) // 8)])
    ax.set_xticklabels([d.strftime("%d %b") for d in days][:: max(1, len(days) // 8)])
    ax.spines[["top", "right"]].set_visible(False)
    ax.legend(frameon=False, fontsize=8)
    return _png(fig)
