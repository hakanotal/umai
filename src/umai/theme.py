"""The brand palette, sampled from umai-logo.jpg.

A Telegram chat cannot be themed; the colours the product actually shows
are the chart PNGs, and later the Mini App. Both draw from here, so the
answer to "what colour is Umai?" is one file rather than a memory.

Sampled by quantising the logo (Pillow, 8-colour median cut) plus a grid
pass for the small-area accent:

  forest  #3c513b  the deep green, the primary data colour
  sand    #dad7bd  large calm areas, secondary series
  olive   #8d8d6a  tertiary series, raw readings
  cream   #f1efde  background wash
  gold    #b08030  the accent, reserved for the target line

Contrast notes: gold on cream and forest on cream both hold up at chart
line weights; sand and olive are fill/area colours and never carry a line
on their own.
"""

from __future__ import annotations

FOREST = "#3c513b"
SAND = "#dad7bd"
OLIVE = "#8d8d6a"
CREAM = "#f1efde"
GOLD = "#b08030"

__all__ = ["CREAM", "FOREST", "GOLD", "OLIVE", "SAND"]
