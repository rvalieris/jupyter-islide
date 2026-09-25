"""Integration tests against the real test slide (needs openslide + data/testslide.tiff)."""
import os
import re

import pytest

try:
    import openslide  # noqa: F401
    HAS_OPENSLLIDE = True
except ImportError:
    HAS_OPENSLLIDE = False

SLIDE = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "data", "testslide.tiff")
)

pytestmark = [
    pytest.mark.skipif(not HAS_OPENSLLIDE, reason="openslide-python not installed"),
    pytest.mark.skipif(not os.path.exists(SLIDE), reason="data/testslide.tiff missing"),
]

from islide.backend import OpenSlideBackend
from islide.cache import TileCache
from islide.encode import jpeg_data_url
from islide.fetch import fetch_tiles
from islide.plan import anchor_l0, plan_viewport
from islide.viewport import Viewport
from islide.widget import HtmlSlideViewer, _SEAM_MARGIN_PX

from util import screen_covered


class CountingBackend:
    def __init__(self, inner):
        self.inner = inner
        self.reads = 0

    def read_region(self, loc, level, size):
        self.reads += 1
        return self.inner.read_region(loc, level, size)


@pytest.fixture(scope="module")
def backend():
    b = OpenSlideBackend(SLIDE)
    yield b
    b.close()


def test_meta(backend):
    m = backend.meta
    assert m.dimensions == (37382, 73222)
    assert m.level_count == 8
    assert m.level_downsamples[0] == 1.0
    assert m.level_downsamples == tuple(sorted(m.level_downsamples))
    assert m.mpp == 0.25


def test_render_roundtrip(backend):
    cache = TileCache()
    cb = CountingBackend(backend)
    vp = Viewport(cx=37382 / 2, cy=73222 / 2, zoom=0.007, canvas_w=960, canvas_h=540)
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
    vp = Viewport(cx=37382 / 2, cy=73222 / 2, zoom=1.0, canvas_w=960, canvas_h=540)
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


def test_m0_html_adjacent_tiles_overlap(backend):
    """M0 <img> boxes carry the same seam-margin inflation as the canvas
    compositor (frontend/compositor.js): every screen-adjacent pair of
    tiles must overlap by ~1 px, so the browser's independent per-element
    rounding of fractional CSS boxes cannot open a white hairline."""
    v = HtmlSlideViewer(SLIDE, canvas_w=960, canvas_h=540)
    try:
        html = v.html.value
    finally:
        v.close()
    parsed = [
        (float(l), float(t), float(w), float(h))
        for l, t, w, h in re.findall(
            r"left:([\d.-]+)px;top:([\d.-]+)px;width:([\d.-]+)px;height:([\d.-]+)px",
            html,
        )
    ]
    assert len(parsed) >= 4  # grid of tiles, so neighbours exist
    m = _SEAM_MARGIN_PX
    # style strings are rounded to 2 dp: allow 0.02 px when comparing edges
    for i in range(len(parsed)):
        for j in range(i + 1, len(parsed)):
            l1, t1, w1, h1 = parsed[i]
            l2, t2, w2, h2 = parsed[j]
            # un-inflated (exact) screen boxes
            u1 = (l1 + m, t1 + m, l1 + w1 - m, t1 + h1 - m)
            u2 = (l2 + m, t2 + m, l2 + w2 - m, t2 + h2 - m)
            y_overlap = min(u1[3], u2[3]) - max(u1[1], u2[1])
            x_overlap = min(u1[2], u2[2]) - max(u1[0], u2[0])
            if abs(u1[2] - u2[0]) < 0.05 and y_overlap > 1:  # i west of j
                assert (l1 + w1) - l2 > 0.5
            elif abs(u2[2] - u1[0]) < 0.05 and y_overlap > 1:  # j west of i
                assert (l2 + w2) - l1 > 0.5
            if abs(u1[3] - u2[1]) < 0.05 and x_overlap > 1:  # i north of j
                assert (t1 + h1) - t2 > 0.5
            elif abs(u2[3] - u1[1]) < 0.05 and x_overlap > 1:  # j north of i
                assert (t2 + h2) - t1 > 0.5


def test_m0_canvas_h_renders_new_height(backend):
    """M0: the canvas_h property re-renders the HTML composite at the
    new viewport height."""
    v = HtmlSlideViewer(SLIDE, canvas_w=960, canvas_h=540)
    try:
        assert v.canvas_h == 540
        v.canvas_h = 800
        assert v.canvas_h == 800
        assert v.viewport.canvas_h == 800
        assert "height:800px" in v.html.value
    finally:
        v.close()


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
