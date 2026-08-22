"""Preparing a photo for the model, and keeping the original.

A modern iPhone photo is 4032x3024 or larger and several megabytes. Most of that
resolution is discarded by the model's tiling anyway, so you pay tokens and
latency for nothing. Downscale the long edge and re-encode before sending.

Store the original regardless. Re-analysing old meals with a better model later
is one of the quiet advantages of self-hosting, and you cannot do it from a
thumbnail.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import io
from pathlib import Path

from PIL import Image, ImageOps

# The long edge sent to the model. Verify against your own photos with the eval
# harness before treating this as settled; the gotcha list says to expect a cost
# and latency drop with no measurable accuracy change, not to assume it.
SEND_LONG_EDGE = 1024
SEND_QUALITY = 85

EXIF_DATETIME_ORIGINAL = 36867


def prepare_for_model(path: Path, long_edge: int = SEND_LONG_EDGE) -> tuple[str, str]:
    """Return (base64 JPEG, media type) for a stored photo.

    `exif_transpose` first: iPhone photos carry an orientation tag rather than
    rotated pixels, and a sideways plate measurably degrades both identification
    and the scale cues the gram estimate depends on.
    """
    with Image.open(path) as opened:
        image = ImageOps.exif_transpose(opened) or opened
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        image.thumbnail((long_edge, long_edge), Image.Resampling.LANCZOS)

        buf = io.BytesIO()
        image.save(buf, format="JPEG", quality=SEND_QUALITY, optimize=True)

    return base64.b64encode(buf.getvalue()).decode(), "image/jpeg"


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def taken_at(path: Path) -> dt.datetime | None:
    """When the photo was taken, from EXIF.

    Worth the effort: it is the difference between a meal logged at the time you
    ate it and one logged at the time you got round to sending it, which is the
    same distinction occurred_at versus logged_at exists to keep.

    Returns naive local time as recorded by the camera; the caller attaches the
    user's timezone, because EXIF rarely carries an offset.
    """
    try:
        with Image.open(path) as im:
            exif = im.getexif()
    except Exception:
        return None
    raw = exif.get(EXIF_DATETIME_ORIGINAL) or exif.get(306)  # 306 = DateTime
    if not isinstance(raw, str):
        return None
    try:
        return dt.datetime.strptime(raw, "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None
