"""Unit tests for the tile cache."""
from islide.cache import TileCache


class FakeImg:
    def __init__(self, w, h):
        self.width = w
        self.height = h


def test_put_get():
    c = TileCache(4)
    img = FakeImg(10, 10)
    c.put(("0", 0, 0), img)
    assert c.get(("0", 0, 0)) is img
    assert c.hits == 1 and c.misses == 0


def test_miss_counts():
    c = TileCache(4)
    assert c.get("nope") is None
    assert c.misses == 1


def test_lru_eviction_by_count():
    c = TileCache(2)  # room for exactly two tiles
    c.put("a", FakeImg(16, 16))
    c.put("b", FakeImg(16, 16))
    assert c.get("a") is not None  # 'a' is now MRU
    c.put("c", FakeImg(16, 16))  # must evict 'b' (LRU)
    assert c.get("b") is None
    assert c.get("a") is not None
    assert c.get("c") is not None
    assert len(c) == 2


def test_update_existing_key_no_double_count():
    # capacity 3; a double-counted 'a' would leave only room for 'b'.
    c = TileCache(3)
    c.put("a", FakeImg(16, 16))
    c.put("b", FakeImg(16, 16))
    c.put("a", FakeImg(16, 16))  # replace, must not double-count
    c.put("c", FakeImg(16, 16))
    assert c.get("b") is not None  # 'b' still alive -> 'a' was not double-counted
    assert len(c) == 3


def test_relimit_down_evicts_lru():
    c = TileCache(3)
    c.put("a", FakeImg(16, 16))
    c.put("b", FakeImg(16, 16))
    c.put("c", FakeImg(16, 16))
    c.get("a")  # 'a' is now MRU
    c.relimit(2)  # must evict 'b' (LRU)
    assert len(c) == 2 and c.max_tiles == 2
    assert c.get("b") is None
    assert c.get("a") is not None and c.get("c") is not None


def test_relimit_up_only_raises_headroom():
    c = TileCache(1)
    c.put("a", FakeImg(16, 16))
    c.relimit(3)
    assert c.max_tiles == 3 and len(c) == 1
    c.put("b", FakeImg(16, 16))
    c.put("c", FakeImg(16, 16))
    assert len(c) == 3


def test_clear():
    c = TileCache(4)
    c.put("a", FakeImg(16, 16))
    c.clear()
    assert len(c) == 0 and c.get("a") is None