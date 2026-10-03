"""Shared test helpers (kept dependency-free so every test module can import)."""
from __future__ import annotations

import os
import shutil
import urllib.request
from pathlib import Path


def screen_covered(plan, sx, sy) -> bool:
    """Is screen point (sx, sy) inside some tile's screen box?"""
    for t in plan.tiles:
        l, tp, w, h = t.screen
        if l - 1e-9 <= sx < l + w + 1e-9 and tp - 1e-9 <= sy < tp + h + 1e-9:
            return True
    return False


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
