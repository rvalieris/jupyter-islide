"""Execute a :class:`ReadPlan`: cache lookups + one region read, sliced."""
from __future__ import annotations

from typing import Any

from .plan import ReadPlan, Tile


def fetch_tiles(backend: Any, cache: Any, plan: ReadPlan) -> dict[tuple, Any]:
    """Return ``{tile.key: PIL image}`` for every tile in the plan.

    Cache hits are served from the LRU cache; all misses are satisfied by
    *one* ``read_region`` call (the plan's single covering read), which is
    then sliced into tiles and the tiles cached.
    """
    if not plan.tiles:
        return {}

    result: dict[tuple, Any] = {}
    missing: list[Tile] = []
    for tile in plan.tiles:
        img = cache.get(tile.key)
        if img is not None:
            result[tile.key] = img
        else:
            missing.append(tile)

    if missing:
        big = backend.read_region(plan.loc, plan.level, plan.size)
        for tile in missing:
            img = big.crop(tile.crop)
            cache.put(tile.key, img)
            result[tile.key] = img

    return result
