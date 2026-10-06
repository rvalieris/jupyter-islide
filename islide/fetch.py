"""Execute a :class:`ReadPlan`: cache lookups + chunked region reads,
sliced."""
from __future__ import annotations

from typing import Any

from .plan import Chunk, ReadPlan, Tile


def fetch_chunk(
    backend: Any, cache: Any, chunk: Chunk, plan: ReadPlan
) -> dict[tuple, Any]:
    """Return ``{tile.key: PIL image}`` for one chunk's tiles.

    Cache hits are served from the LRU cache; all misses are satisfied
    by *one* ``read_region`` call for the chunk's read rect
    (``chunk.loc`` / ``chunk.size``), sliced into the chunk's tiles and
    cached. Tile crops are relative to ``plan.read_origin`` (the union
    read rect); the chunk read is sliced after converting them to
    ``chunk.read_origin`` (the block top-left — a tile cell is always
    inside its block, so the offsets are non-negative).
    """
    result: dict[tuple, Any] = {}
    missing: list[Tile] = []
    for tile in chunk.tiles:
        img = cache.get(tile.key)
        if img is not None:
            result[tile.key] = img
        else:
            missing.append(tile)

    if missing:
        big = backend.read_region(chunk.loc, plan.level, chunk.size)
        prx, pry = plan.read_origin
        crx, cry = chunk.read_origin
        for tile in missing:
            x0, y0, x1, y1 = tile.crop
            img = big.crop(
                (prx + x0 - crx, pry + y0 - cry, prx + x1 - crx, pry + y1 - cry)
            )
            cache.put(tile.key, img)
            result[tile.key] = img
    return result


