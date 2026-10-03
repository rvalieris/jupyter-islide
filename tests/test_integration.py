"""Integration tests against the real test slide (needs openslide +
data/CMU-1.tiff, downloaded at test time when missing -- see conftest.py)."""
import pytest

try:
    import openslide  # noqa: F401
    HAS_OPENSLLIDE = True
except ImportError:
    HAS_OPENSLLIDE = False

from islide.backend import OpenSlideBackend
from islide.cache import TileCache
from islide.encode import jpeg_data_url
from islide.fetch import fetch_tiles
from islide.plan import anchor_l0, plan_viewport
from islide.viewport import Viewport

from util import SLIDE_PATH, screen_covered

SLIDE = str(SLIDE_PATH)

pytestmark = [
    pytest.mark.skipif(not HAS_OPENSLLIDE, reason="openslide-python not installed"),
]


class CountingBackend:
    def __init__(self, inner):
        self.inner = inner
        self.reads = 0

    def read_region(self, loc, level, size):
        self.reads += 1
        return self.inner.read_region(loc, level, size)


@pytest.fixture(scope="module")
def backend(slide_path):
    b = OpenSlideBackend(slide_path)
    yield b
    b.close()


def test_meta(backend):
    m = backend.meta
    assert m.dimensions == (46000, 32914)
    assert m.level_count == 9
    assert m.level_downsamples[0] == 1.0
    assert m.level_downsamples == tuple(sorted(m.level_downsamples))
    assert m.mpp == 1000


def test_render_roundtrip(backend):
    cache = TileCache()
    cb = CountingBackend(backend)
    W, H = backend.meta.dimensions
    vp = Viewport(cx=W / 2, cy=H / 2, zoom=0.007, canvas_w=960, canvas_h=540)
    plan = plan_viewport(backend.meta, vp, 256)
    assert plan.tiles
    tiles = fetch_tiles(cb, cache, plan)
    assert cb.reads == 1  # one big read for the whole viewport
    for t in plan.tiles:
        img = tiles[t.key]
        assert img.size == (t.crop[2] - t.crop[0], t.crop[3] - t.crop[1])
        assert img.mode == "RGBA"
        url = jpeg_data_url(img)
        assert url.startswith("data:image/jpeg;base64,")
        assert len(url) > 1000


def test_cache_warm_second_fetch_no_reads(backend):
    cache = TileCache()
    cb = CountingBackend(backend)
    W, H = backend.meta.dimensions
    vp = Viewport(cx=W / 2, cy=H / 2, zoom=1.0, canvas_w=960, canvas_h=540)
    plan = plan_viewport(backend.meta, vp, 256)
    assert plan.level == 0  # zoom 1.0 -> level 0
    fetch_tiles(cb, cache, plan)
    n = cb.reads
    assert n >= 1
    fetch_tiles(cb, cache, plan)  # same viewport: everything from cache
    assert cb.reads == n


def test_off_slide(backend):
    vp = Viewport(cx=10**7, cy=10**7, zoom=0.5)
    plan = plan_viewport(backend.meta, vp, 256)
    assert plan.tiles == ()


def test_corner_viewport(backend):
    vp = Viewport(cx=0, cy=0, zoom=0.25)
    plan = plan_viewport(backend.meta, vp, 256)
    assert plan.tiles
    assert all(t.screen[0] >= -1e-9 and t.screen[1] >= -1e-9 for t in plan.tiles)
    # the in-slide corner of the canvas is covered, off-slide is not
    # (canvas is 960x540, zoom 0.25 -> on-slide region is sx in [480, 960)
    # and sy in [270, 540))
    assert screen_covered(plan, 700, 500)
    assert not screen_covered(plan, 100, 100)


def test_tile_cache_is_viewport_invariant(backend):
    """A tile's image must depend only on (level, tx, ty) and the slide bounds,
    never on which viewport fetched it. (Previously the read rect's edges cut
    through cells, so a tile cached as a partial crop at one viewport's edge
    was later served stretched as a full cell.)"""
    T = 256
    cache = TileCache()
    ds0 = backend.meta.level_downsamples[0]
    W, H = backend.meta.dimensions
    for dx, dy in ((0, 0), (150, 0), (0, 150), (400, 0), (0, 900), (-300, 0)):
        vp = Viewport(cx=8192 + dx, cy=8192 + dy, zoom=16.0, canvas_w=300, canvas_h=300)
        plan = plan_viewport(backend.meta, vp, T)
        assert plan.level == 0
        fetch_tiles(backend, cache, plan)
        for t in plan.tiles:
            _, tx, ty = t.key
            img = cache.get(t.key)
            cw = min(tx * T + T, W) - tx * T
            ch = min(ty * T + T, H) - ty * T
            assert img.size == (cw, ch)
            # content must be exactly the slide pixels for that cell
            truth = backend.read_region(
                (anchor_l0(tx * T, ds0), anchor_l0(ty * T, ds0)), 0, (T, T)
            )
            lx0 = t.crop[0] + plan.read_origin[0] - tx * T
            ly0 = t.crop[1] + plan.read_origin[1] - ty * T
            assert img.tobytes() == truth.crop(
                (lx0, ly0, lx0 + img.width, ly0 + img.height)
            ).tobytes()
