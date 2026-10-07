"""Execute a :class:`ReadPlan`: cache lookups + chunked region reads,
sliced and encoded into tile data URLs."""
from __future__ import annotations

from typing import Any

from .encode import jpeg_data_url
from .plan import Chunk, ReadPlan, Tile


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
    are satisfied by *one* ``read_region`` call for the chunk's read rect
    (``chunk.loc`` / ``chunk.size``), sliced into the chunk's tiles, encoded
    at ``quality``, and cached. Tile crops are relative to ``plan.read_origin``
    (the union read rect); the chunk read is sliced after converting them to
    ``chunk.read_origin`` (the block top-left — a tile cell is always inside
    its block, so the offsets are non-negative).
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
        big = backend.read_region(chunk.loc, plan.level, chunk.size)
        prx, pry = plan.read_origin
        crx, cry = chunk.read_origin
        for tile in missing:
            x0, y0, x1, y1 = tile.crop
            img = big.crop(
                (prx + x0 - crx, pry + y0 - cry, prx + x1 - crx, pry + y1 - cry)
            )
            url = jpeg_data_url(img, quality)
            cache.put(tile.key, url)
            result[tile.key] = url
    return result
