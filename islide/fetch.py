"""Execute a :class:`ReadPlan`: cache lookups + chunked region reads,
sliced and encoded into tile data URLs."""
from __future__ import annotations

from typing import Any

from .encode import jpeg_data_url
from .plan import Chunk, ReadPlan, Tile, anchor_l0


def fetch_chunk(
    backend: Any,
    cache: Any,
    chunk: Chunk,
    plan: ReadPlan,
    quality: int = 85,
) -> dict[tuple, str]:
    """Return ``{tile.key: JPEG data URL}`` for one chunk's tiles.

    Cache hits are served from the LRU cache as-is (the cache stores the
    *encoded* tiles, so a hit is re-used without re-encoding); all misses
    are satisfied by *one* ``read_region`` call for the union of the
    missing tiles' cells — not the whole chunk block, so a partially
    missed chunk decodes only what it is missing — sliced into the tiles,
    encoded at ``quality``, and cached. Tile crops are relative to
    ``plan.read_origin`` (the union read rect); the chunk read is sliced
    after converting them to the union origin (every missing cell is
    inside the union, so the offsets are non-negative).
    """
    result: dict[tuple, str] = {}
    missing: list[Tile] = []
    for tile in chunk.tiles:
        url = cache.get(tile.key)
        if url is not None:
            result[tile.key] = url
        else:
            missing.append(tile)

    if missing:
        prx, pry = plan.read_origin
        # Union of the missing tiles' cells, absolute level px: each
        # tile's crop re-based onto ``plan.read_origin`` is its
        # grid-aligned cell clamped to the level extent, so the union is
        # grid-aligned too, and ``anchor_l0`` anchors the read exactly at
        # its top-left.
        ux0 = min(prx + t.crop[0] for t in missing)
        uy0 = min(pry + t.crop[1] for t in missing)
        ux1 = max(prx + t.crop[2] for t in missing)
        uy1 = max(pry + t.crop[3] for t in missing)
        ds = plan.downsample
        big = backend.read_region(
            (anchor_l0(ux0, ds), anchor_l0(uy0, ds)),
            plan.level,
            (ux1 - ux0, uy1 - uy0),
        )
        for tile in missing:
            x0, y0, x1, y1 = tile.crop
            img = big.crop(
                (prx + x0 - ux0, pry + y0 - uy0, prx + x1 - ux0, pry + y1 - uy0)
            )
            url = jpeg_data_url(img, quality)
            cache.put(tile.key, url)
            result[tile.key] = url
    return result
