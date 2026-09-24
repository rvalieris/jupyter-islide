"""Unit tests for the tile cache."""
from islide.cache import TileCache


class FakeImg:
    def __init__(self, w, h):
        self.width = w
        self.height = h


def test_put_get():
    c = TileCache(1024)
    img = FakeImg(10, 10)
    c.put(("0", 0, 0), img)
    assert c.get(("0", 0, 0)) is img
    assert c.hits == 1 and c.misses == 0


def test_miss_counts():
    c = TileCache(1024)
    assert c.get("nope") is None
    assert c.misses == 1


def test_lru_eviction_by_bytes():
    c = TileCache(2048)  # room for exactly two 16x16 RGBA images (1024 B each)
    c.put("a", FakeImg(16, 16))
    c.put("b", FakeImg(16, 16))
    assert c.get("a") is not None  # 'a' is now MRU
    c.put("c", FakeImg(16, 16))  # must evict 'b' (LRU)
    assert c.get("b") is None
    assert c.get("a") is not None
    assert c.get("c") is not None
    assert len(c) == 2


def test_update_existing_key_no_double_count():
    # 3x1024 B images; budget fits 3 but not 4 -> a double-counted 'a'
    # would push the cache over budget and evict 'b'.
    c = TileCache(4095)
    c.put("a", FakeImg(16, 16))
    c.put("b", FakeImg(16, 16))
    c.put("a", FakeImg(16, 16))  # replace, must not double-count bytes
    c.put("c", FakeImg(16, 16))
    assert c.get("b") is not None  # 'b' still alive -> 'a' was not double-counted


def test_clear():
    c = TileCache(1024)
    c.put("a", FakeImg(16, 16))
    c.clear()
    assert len(c) == 0 and c.get("a") is None
