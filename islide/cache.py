"""Byte-budgeted LRU cache for decoded display tiles."""
from __future__ import annotations

from collections import OrderedDict
from typing import Any

_DEFAULT_MAX_BYTES = 256 * 1024 * 1024


class TileCache:
    """LRU cache keyed by ``(level, tx, ty)``.

    Values are decoded images (PIL); the byte budget is estimated as
    ``w * h * 4`` (conservative: >= RGBA size for any mode we use).
    """

    def __init__(self, max_bytes: int = _DEFAULT_MAX_BYTES) -> None:
        self.max_bytes = max_bytes
        self._data: "OrderedDict[Any, tuple[Any, int]]" = OrderedDict()
        self._size = 0
        self.hits = 0
        self.misses = 0

    def _est_bytes(self, img: Any) -> int:
        return img.width * img.height * 4

    def get(self, key: Any):
        item = self._data.get(key)
        if item is None:
            self.misses += 1
            return None
        self._data.move_to_end(key)
        self.hits += 1
        return item[0]

    def put(self, key: Any, img: Any) -> None:
        est = self._est_bytes(img)
        if key in self._data:
            self._size -= self._data[key][1]
            self._data.pop(key)
        self._data[key] = (img, est)
        self._size += est
        while self._size > self.max_bytes and self._data:
            self._size -= self._data.popitem(last=False)[1][1]

    def __len__(self) -> int:
        return len(self._data)

    def __contains__(self, key: Any) -> bool:
        return key in self._data

    def clear(self) -> None:
        self._data.clear()
        self._size = 0
