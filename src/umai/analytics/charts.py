"""Matplotlib, Agg backend. Headless PNG rendering into Telegram.

Charts are push, not pull: the daily digest and the weight trend arrive
without being asked for. Every chart is derived entirely from data the code
already holds, a chart is never a place where a number gets invented.

Colour comes from umai.theme, which is the landing page's palette, so the
picture that lands at ten in the evening looks like the same product as
docs/index.html rather than like matplotlib.

**The digest chart is one chart, not three.** Calories, water and steps are
three units that cannot share an axis, so each bar is drawn as a percentage
of that metric's own daily goal and the goal itself is the single dashed
line at 100%. The absolute figures are not printed on the bars — they are
already in the caption underneath, and putting them here twice is the
crowding this chart is meant to avoid. What the bars are measured against
lives in the legend, once per series.
"""

from __future__ import annotations

import datetime as dt
import io

import matplotlib

matplotlib.use("Agg")  # headless, before pyplot import
import matplotlib.figure
import matplotlib.pyplot as plt
from matplotlib import font_manager

from umai import theme
from umai.analytics.calibration import REFERENCE_STEPS


def _resolve_font() -> str:
    """The first family in the brand stack that this machine actually has.

    Resolved once, here, rather than handing matplotlib the whole list at every
    label: matplotlib re-walks the stack per text object and logs
    "findfont: Font family 'Inter' not found" each time, which on the Railway
    image is forty lines of noise in the scheduler log every evening.
    """
    installed = {f.name for f in font_manager.fontManager.ttflist}
    return next((name for name in theme.FONT_STACK if name in installed), "DejaVu Sans")


FONT = _resolve_font()

# Bars are clipped here rather than allowed to set the scale. One 400% water
# day would otherwise squash the whole week into the bottom eighth of the
# figure; a clipped bar is annotated with its real percentage instead.
_CEILING = 200.0
_BAR_W = 0.26
# The tallest a series may push the axis before the ceiling takes over, and the
# shortest the axis is ever allowed to be, so a quiet week still shows the goal
# line with air above it.
_HEADROOM = 1.12
_FLOOR_TOP = 135.0
# A genuine zero and a missing reading both draw nothing, and they mean
# opposite things. A zero gets a sliver tall enough to see at Telegram's
# preview size; a missing reading gets the "?" instead.
_ZERO_STUB = 1.6


def _png(fig: matplotlib.figure.Figure) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight", facecolor=theme.PAPER)
    plt.close(fig)
    return buf.getvalue()


def _figure(size: tuple[float, float]) -> tuple[matplotlib.figure.Figure, plt.Axes]:
    """A figure with the house style already applied.

    Set on the objects rather than through rcParams: rcParams is process-global
    and these functions run inside the scheduler alongside anything else that
    might draw.
    """
    fig, ax = plt.subplots(figsize=size)
    fig.patch.set_facecolor(theme.PAPER)
    ax.set_facecolor(theme.PAPER)
    ax.spines[["top", "right", "left"]].set_visible(False)
    ax.spines["bottom"].set_color(theme.LINE)
    ax.tick_params(axis="both", length=0, colors=theme.MUTED, labelsize=9)
    ax.set_axisbelow(True)
    for label in (*ax.get_xticklabels(), *ax.get_yticklabels()):
        label.set_fontfamily(FONT)
    return fig, ax


def _pct(value: float | None, goal: float | None) -> float | None:
    """A metric as a percentage of its goal, or None when either is unknown.

    None propagates rather than becoming zero. A day the phone failed to sync
    is not a day spent sitting down, and the whole of `tools.daily_steps`
    exists to keep those two apart — a chart is not the place to collapse them.
    """
    if value is None or not goal:
        return None
    return 100.0 * value / goal


def week_overview(
    days: list[dt.date],
    kcal: list[float],
    water_ml: list[float],
    steps: list[int | None],
    *,
    kcal_target: float | None,
    water_target_ml: float,
    step_goal: float = REFERENCE_STEPS,
) -> bytes:
    """The evening digest chart: a week, three bars a day, one goal line.

    Every series is scaled to its own goal, so a bar reaching the dashed line
    means "hit it" whichever metric it belongs to. Calories carry a target only
    once there is a weigh-in to compute one from; without it the week's own
    mean logged intake is used as the reference and the legend says so, which
    is a stated normaliser rather than a target the code made up.

    A day with no step reading gets a muted "?" instead of a bar, because an
    absent bar and a zero bar look identical and mean opposite things.
    """
    fig, ax = _figure((7.6, 3.5))

    kcal_ref, kcal_label = kcal_target, "calories"
    if not kcal_ref:
        logged = [k for k in kcal if k > 0]
        kcal_ref = sum(logged) / len(logged) if logged else None
        kcal_label = "calories · vs own average"

    series = (
        (kcal_label, kcal_ref, " kcal", theme.GREEN, [_pct(k, kcal_ref) for k in kcal]),
        (
            "water",
            water_target_ml,
            " ml",
            theme.SAGE,
            [_pct(w, water_target_ml) for w in water_ml],
        ),
        (
            "steps",
            step_goal,
            "",
            theme.GOLD_SOFT,
            [_pct(s, step_goal) for s in steps],
        ),
    )

    xs = list(range(len(days)))

    # The scale is decided before anything is drawn, because a clipped bar has
    # to be drawn *to* the top of the axis and labelled just inside it. Drawing
    # first and scaling after put the label above the axis, on top of the
    # legend.
    ceiling_hit = any(p is not None and p > _CEILING for _, _, _, _, pcts in series for p in pcts)
    under = [p for _, _, _, _, pcts in series for p in pcts if p is not None and p <= _CEILING]
    top = max(_FLOOR_TOP, max(under, default=0.0) * _HEADROOM)
    if ceiling_hit:
        top = max(top, _CEILING)
    ax.set_ylim(0, top)

    for slot, (name, ref, unit, colour, pcts) in enumerate(series):
        offset = (slot - 1) * (_BAR_W + 0.02)
        label = f"{name} · {ref:,.0f}{unit}".replace(",", " ") if ref else name
        drawn = [max(min(p, top), _ZERO_STUB) if p is not None else 0.0 for p in pcts]
        ax.bar(
            [x + offset for x in xs],
            drawn,
            width=_BAR_W,
            color=colour,
            label=label,
            linewidth=0,
            zorder=2,
        )
        for x, p in zip(xs, pcts, strict=True):
            if p is None:
                ax.text(
                    x + offset,
                    top * 0.02,
                    "?",
                    ha="center",
                    va="bottom",
                    fontsize=8,
                    color=theme.MUTED,
                    fontfamily=FONT,
                )
            elif p > top:
                # Inside the bar, in the page colour, so an outlier reads as
                # "off the scale, and here is by how much" without stealing a
                # row of the figure for itself.
                ax.text(
                    x + offset,
                    top * 0.975,
                    f"{p:.0f}",
                    ha="center",
                    va="top",
                    fontsize=7,
                    color=theme.PAPER,
                    zorder=4,
                    fontfamily=FONT,
                )

    ax.axhline(100, color=theme.GOLD, lw=1.2, ls=(0, (5, 4)), zorder=3)
    ax.text(
        len(days) - 0.4,
        101,
        "goal",
        ha="right",
        va="bottom",
        fontsize=8,
        color=theme.GOLD,
        fontfamily=FONT,
    )

    ax.yaxis.grid(True, color=theme.LINE, lw=0.8)
    ax.set_yticks([t for t in (0, 50, 100, 150, 200) if t <= top])
    ax.set_yticklabels([f"{t:.0f}%" for t in ax.get_yticks()])
    ax.set_xticks(xs)
    ax.set_xticklabels([f"{d:%a}\n{d.day}" for d in days])
    ax.set_xlim(-0.6, len(days) - 0.4)
    # Today is the day the digest is about; the other six are context.
    for tick in ax.get_xticklabels():
        tick.set_fontfamily(FONT)
        tick.set_color(theme.MUTED)
    if ax.get_xticklabels():
        ax.get_xticklabels()[-1].set_color(theme.INK)
        ax.get_xticklabels()[-1].set_fontweight("bold")

    legend = ax.legend(
        frameon=False,
        fontsize=8.5,
        ncols=3,
        loc="lower center",
        bbox_to_anchor=(0.5, 1.0),
        handlelength=1.1,
        handleheight=0.9,
        columnspacing=1.6,
    )
    for text in legend.get_texts():
        text.set_fontfamily(FONT)
        text.set_color(theme.INK)
    return _png(fig)


def weight_trend(days: list[dt.date], raw: list[float | None], ewma: list[float]) -> bytes:
    """Raw readings as faint dots, the EWMA as the line. The trend is the
    product; the dots are there to show what it is made of."""
    fig, ax = _figure((7.6, 3.2))
    xs_raw = [d.toordinal() for d, r in zip(days, raw, strict=True) if r is not None]
    ys_raw = [r for r in raw if r is not None]
    ax.yaxis.grid(True, color=theme.LINE, lw=0.8)
    ax.scatter(xs_raw, ys_raw, s=14, color=theme.SAGE, zorder=2, label="weigh-ins")
    ax.plot(
        [d.toordinal() for d in days],
        ewma,
        color=theme.GREEN,
        lw=2.2,
        zorder=3,
        label="trend",
    )
    ax.set_ylabel("kg", color=theme.MUTED, fontsize=9, fontfamily=FONT)
    step = max(1, len(days) // 8)
    ax.set_xticks([d.toordinal() for d in days][::step])
    ax.set_xticklabels([f"{d:%d %b}" for d in days][::step])
    for tick in ax.get_xticklabels():
        tick.set_fontfamily(FONT)
    legend = ax.legend(frameon=False, fontsize=8.5, loc="best")
    for text in legend.get_texts():
        text.set_fontfamily(FONT)
        text.set_color(theme.INK)
    return _png(fig)
