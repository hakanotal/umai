"""The brand palette, shared by the charts and the landing page.

A Telegram chat cannot be themed; the colours the product actually shows
are the chart PNGs and `docs/index.html`. Both draw from here, so the
answer to "what colour is Umai?" is one file rather than two that drift.

The tokens below are the landing page's CSS custom properties, name for
name — `--green-soft` is `GREEN_SOFT`. When one changes, change both; a
chart that does not look like the site is the failure this file exists to
prevent.

  ink        #1D3527  text, axis labels
  green      #2F5741  the primary data colour
  green_soft #4A7358  secondary green, hover/second series
  sage       #7C9A5B  tertiary series
  gold       #B8862B  the page's accent; not used by the charts
  gold_soft  #D2A94F  a fourth series, and the gold that survives as a fill
  cream      #EDE6C8  large calm areas
  cream_dim  #F4EFDC  background wash
  paper      #FCFBF6  the page, and every chart's facecolor
  muted      #6E7A6E  subtitles, tick labels, "no reading" marks
  line       #DDD8C2  hairlines, gridlines, the baseline spine

Contrast notes: green, gold and ink all hold up on paper at chart line
weights; cream, cream_dim and line are fill and rule colours and never
carry a label on their own.
"""

from __future__ import annotations

INK = "#1D3527"
GREEN = "#2F5741"
GREEN_SOFT = "#4A7358"
SAGE = "#7C9A5B"
GOLD = "#B8862B"
GOLD_SOFT = "#D2A94F"
CREAM = "#EDE6C8"
CREAM_DIM = "#F4EFDC"
PAPER = "#FCFBF6"
MUTED = "#6E7A6E"
LINE = "#DDD8C2"

# The face stack the landing page asks for, degraded for a headless container.
# Matplotlib walks the list and takes the first installed family, so a Mac
# renders Inter/Helvetica and the Railway image falls back to DejaVu Sans
# rather than raising.
FONT_STACK = ["Inter", "Helvetica Neue", "Helvetica", "Arial", "DejaVu Sans"]

# Semantic colours for the digest chart's three series. Desaturated towards
# earth tones so no series looks louder than the others on the cream page.
# kcal=terracotta, water=slate blue, steps=moss green. The goal line uses INK
# rather than GOLD: gold sits too close to terracotta and went soft at the bar.
SERIES_KCAL = "#C05B3A"
SERIES_WATER = "#35708F"
SERIES_STEPS = "#6E8C4A"

__all__ = [
    "CREAM",
    "CREAM_DIM",
    "FONT_STACK",
    "GOLD",
    "GOLD_SOFT",
    "GREEN",
    "GREEN_SOFT",
    "INK",
    "LINE",
    "MUTED",
    "PAPER",
    "SAGE",
    "SERIES_KCAL",
    "SERIES_STEPS",
    "SERIES_WATER",
]
