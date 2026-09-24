"""Viewport -> read plan. Pure math over :class:`SlideMeta`; no I/O.

Coordinate models (verified against libopenslide, see DESIGN.md §8):

* ``read_region(location=loc_l0, level=L, size=(w, h))`` returns an image of
  exactly ``(w, h)`` (out-of-bounds areas filled by the vendor, e.g.
  transparent black), whose top-left pixel is the level-L pixel
  ``floor(loc_l0 / ds[L])`` (per axis).
* Consequence: ``loc_l0 = ceil(P * ds[L])`` anchors the read at exactly
  level pixel ``P`` (for ``ds >= 1``; see ``test_plan.py``).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .viewport import SlideMeta, Viewport


@dataclass(frozen=True)
class Tile:
    """One display tile from the read plan."""

    key: tuple[int, int, int]  # (level, tx, ty); grid cells of ``tile_size`` level px
    crop: tuple[int, int, int, int]  # box (x0, y0, x1, y1) inside the read image
    screen: tuple[float, float, float, float]  # left, top, width, height in screen px


@dataclass(frozen=True)
class ReadPlan:
    """How to satisfy a viewport: one big region read, sliced into tiles."""

    level: int
    downsample: float
    loc: tuple[int, int]  # level-0 location for read_region
    size: tuple[int, int]  # read size in level-``level`` pixels
    read_origin: tuple[int, int]  # level px of the read image's top-left
    tiles: tuple[Tile, ...]


def select_level(level_downsamples: tuple[float, ...], zoom: float) -> int:
    """Finest pyramid level that is at least as detailed as the screen.

    Picks the smallest ``L`` with ``downsample[L] >= 1/zoom``, so rendering
    only ever down-scales (never a blurry up-scale). Falls back to the
    coarsest available level when the zoom is below the pyramid's range.

    Note: this is *not* ``get_best_level_for_downsample(1/zoom)`` — OpenSlide
    picks the coarsest level with ``ds <= need`` (which may up-scale).
    """
    need = 1.0 / zoom
    L = len(level_downsamples) - 1
    for L, ds in enumerate(level_downsamples):
        if ds >= need:
            break
    return L


def anchor_l0(p: int, ds: float) -> int:
    """Level-0 location that anchors ``read_region`` at level pixel ``p``.

    ``floor(anchor_l0(p, ds) / ds) == p`` for all ``ds >= 1``.
    """
    return int(math.ceil(p * ds))


def plan_viewport(
    meta: SlideMeta,
    vp: Viewport,
    tile_size: int = 256,
) -> ReadPlan:
    """Compute the read plan for a viewport.

    One region read per level (the level covering the viewport), clamped to
    the level bounds, sliced into a ``tile_size`` grid. The tile grid is
    global (aligned to 0 at every zoom), so tiles are cache-stable across
    pan/zoom.
    """
    level = select_level(meta.level_downsamples, vp.zoom)
    ds = meta.level_downsamples[level]
    lw, lh = meta.level_dimensions[level]
    z = vp.zoom
    x0, y0, x1, y1 = vp.l0_bbox  # level-0 floats

    # Viewport in level px (floats).
    lx0, ly0, lx1, ly1 = x0 / ds, y0 / ds, x1 / ds, y1 / ds

    # Grid cells overlapping the viewport (grid is global: cell (tx, ty)
    # covers level px [tx*T, (tx+1)*T) x [ty*T, (ty+1)*T)).
    t = tile_size
    tx0 = math.floor(lx0 / t)
    tx1 = math.ceil(lx1 / t) - 1
    ty0 = math.floor(ly0 / t)
    ty1 = math.ceil(ly1 / t) - 1
    if tx1 < tx0 or ty1 < ty0:
        return ReadPlan(level, ds, (0, 0), (0, 0), (0, 0), ())

    # Read rectangle = union of those cells, clamped to the level bounds.
    # Anchoring the read rect to the tile grid means each tile's crop
    # depends only on the cell and the slide boundary — never on the
    # viewport — so the (level, tx, ty) cache key is sound (a non-anchored
    # rect cuts cells at its edges and the cached partial crop gets served
    # stretched as a full cell later).
    rx = max(0, tx0 * t)
    ry = max(0, ty0 * t)
    rx1 = min(lw, (tx1 + 1) * t)
    ry1 = min(lh, (ty1 + 1) * t)
    if rx1 <= rx or ry1 <= ry:
        # Viewport entirely off-slide: nothing to read or draw.
        return ReadPlan(level, ds, (0, 0), (0, 0), (0, 0), ())

    loc = (anchor_l0(rx, ds), anchor_l0(ry, ds))
    size = (rx1 - rx, ry1 - ry)

    tiles: list[Tile] = []
    for ty in range(ty0, ty1 + 1):
        for tx in range(tx0, tx1 + 1):
            c0x = max(tx * t, rx)
            c0y = max(ty * t, ry)
            c1x = min(tx * t + t, rx1)
            c1y = min(ty * t + t, ry1)
            if c1x <= c0x or c1y <= c0y:
                continue  # outside the read rect (off-slide)
            # Screen box of the *crop* (what the image actually contains):
            left = (c0x * ds - x0) * z
            top = (c0y * ds - y0) * z
            width = (c1x - c0x) * ds * z
            height = (c1y - c0y) * ds * z
            tiles.append(
                Tile(
                    key=(level, tx, ty),
                    crop=(c0x - rx, c0y - ry, c1x - rx, c1y - ry),
                    screen=(left, top, width, height),
                )
            )

    return ReadPlan(level, ds, loc, size, (rx, ry), tuple(tiles))
