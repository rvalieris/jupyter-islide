"""Count-budgeted LRU cache for decoded display tiles."""
from __future__ import annotations

from collections import OrderedDict
from typing import Any

_DEFAULT_MAX_TILES = 1000


class TileCache:
    """LRU cache keyed by ``(level, tx, ty)``.

    Values are decoded images (PIL). The budget is a count of tiles:
    each tile is at most one ``tile_size`` level-px cell (edges smaller),
    so the count bounds memory closely, and it matches the JS view's
    decoded-image cap (``image_cache_max``) one-for-one.
    """

    def __init__(self, max_tiles: int = _DEFAULT_MAX_TILES) -> None:
        self.max_tiles = max_tiles
        self._data: "OrderedDict[Any, Any]" = OrderedDict()
        self.hits = 0
        self.misses = 0

    def get(self, key: Any):
        item = self._data.get(key)
        if item is None:
            self.misses += 1
            return None
        self._data.move_to_end(key)
        self.hits += 1
        return item

    def put(self, key: Any, img: Any) -> None:
        if key in self._data:
            self._data.pop(key)  # replace, not a double entry
        self._data[key] = img
        while len(self._data) > self.max_tiles and self._data:
            self._data.popitem(last=False)

    def relimit(self, max_tiles: int) -> None:
        """Change the cap at runtime; a decrease evicts LRU-oldest now,
        an increase just raises the headroom.
        """
        self.max_tiles = max_tiles
        while len(self._data) > self.max_tiles and self._data:
            self._data.popitem(last=False)

    def __len__(self) -> int:
        return len(self._data)

    def __contains__(self, key: Any) -> bool:
        return key in self._data

    def clear(self) -> None:
        self._data.clear()