"""Shared test helpers (kept dependency-free so every test module can import)."""
from __future__ import annotations

import os
import shutil
import urllib.request
from pathlib import Path


def tile_level_rect(plan, tile) -> tuple[int, int, int, int]:
    """Absolute rect (x0, y0, x1, y1) in level px that a tile covers.

    ``tile.crop`` is relative to ``plan.read_origin``; adding it back gives
    the tile's grid cell clipped to the level — the same geometry the wire
    sends as ``tile_geo``.
    """
    rx, ry = plan.read_origin
    x0, y0, x1, y1 = tile.crop
    return (rx + x0, ry + y0, rx + x1, ry + y1)


def screen_covered(plan, vp, sx, sy) -> bool:
    """Is the canvas point (sx, sy) inside some tile?

    Canvas px are measured from the top-left of the viewport's level-0 bbox
    (the frame the view draws in), so the point maps to level px as
    ``((x0 + sx / zoom) / downsample, ...)``. The plan used to carry this
    projection as ``Tile.screen``; nothing on the wire or in the widget uses
    it, so the mapping lives in the harness instead.
    """
    x0, y0, _, _ = vp.l0_bbox
    ds = plan.downsample
    lx = (x0 + sx / vp.zoom) / ds
    ly = (y0 + sy / vp.zoom) / ds
    return any(
        rx0 - 1e-9 <= lx < rx1 + 1e-9 and ry0 - 1e-9 <= ly < ry1 + 1e-9
        for rx0, ry0, rx1, ry1 in (tile_level_rect(plan, t) for t in plan.tiles)
    )


# --------------------------------------------------------------- test slide
# The integration/widget tests run against a real slide that is NOT
# committed to the repo: the generic-TIFF test slide from openslide-testdata
# (whole-slide WSI, 46000 x 32914 px, 9 levels, 1.0 mm/px, opens via the
# generic-TIFF vendor). It is downloaded to data/ on demand (see
# tests/conftest.py, `slide_path` fixture).
SLIDE_NAME = "CMU-1.tiff"
SLIDE_URL = ("https://openslide.cs.cmu.edu/download/openslide-testdata/"
             f"Generic-TIFF/{SLIDE_NAME}")
SLIDE_PATH = Path(__file__).resolve().parents[1] / "data" / SLIDE_NAME


def ensure_slide() -> bool:
    """Make sure the test slide exists, downloading it at test time if not.

    Streams to a ``.part`` file and moves it into place atomically, so a
    failed/interrupted download never leaves a truncated slide behind.
    Returns True if the slide is usable, False if it is missing and the
    download failed (caller skips).
    """
    if SLIDE_PATH.exists():
        return True
    part = SLIDE_PATH.with_name(SLIDE_NAME + ".part")
    try:
        SLIDE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(SLIDE_URL, timeout=60) as resp:
            with part.open("wb") as f:
                shutil.copyfileobj(resp, f, length=1 << 20)
        os.replace(part, SLIDE_PATH)
    except Exception:
        part.unlink(missing_ok=True)
        return False
    return True
