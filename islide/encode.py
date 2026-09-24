"""Image encoding for transfer over the widget comm."""
from __future__ import annotations

import base64
import io

from PIL import Image


def jpeg_data_url(img: Image.Image, quality: int = 85) -> str:
    """Encode a tile as a base64 JPEG data URL.

    Alpha is composited onto white: out-of-bounds slide areas arrive from
    libopenslide as transparent black, and JPEG has no alpha — without this
    the slide edge would render as a black band.
    """
    if img.mode == "RGBA":
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[3])
        img = bg
    elif img.mode != "RGB":
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return "data:image/jpeg;base64," + b64
