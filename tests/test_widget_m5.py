"""M5 canvas-viewer tests — headless, no openslide.

Covers the M5 (DESIGN.md §6.6) two-stage push over the canvas widget:
a multi-chunk plan pushes the center chunk first, then the full tile set
(both pushes carry identical absolute level-px geometry); a single-chunk
plan pushes once with a single grid-anchored read (the pre-M5 wire
contract); reads happen center-first in chunk order; and a burst of
JS-originated viewports coalesces to a final state matching the final
plan (the render-loop handoff).
"""
from __future__ import annotations

import time

import pytest
from PIL import Image

from islide import SlideViewer
from islide.plan import plan_viewport
from islide.viewport import SlideMeta, Viewport


def make_big_meta() -> SlideMeta:
    return SlideMeta(
        dimensions=(4096, 4096),
        level_count=4,
        level_downsamples=(1.0, 2.0, 4.0, 8.0),
        level_dimensions=((4096, 4096), (2048, 2048), (1024, 1024), (512, 512)),
    )


BIG = make_big_meta()


def key(k: tuple[int, int, int]) -> str:
    return f"{k[0]}:{k[1]}:{k[2]}"


def expected_geo(plan) -> dict[str, list[int]]:
    rx, ry = plan.read_origin
    return {
        key(t.key): [
            t.key[0],
            rx + t.crop[0],
            ry + t.crop[1],
            t.crop[2] - t.crop[0],
            t.crop[3] - t.crop[1],
        ]
        for t in plan.tiles
    }


class FakeSlide:
    """Multi-level slide for a custom slide library (duck-typed openslide
    OO API). ``read_region(location, level, size)`` follows openslide's
    semantics: location in level-0 px, size in level px; out-of-bounds is
    filled (PIL's crop pads), as the vendor fills.

    Each pyramid level is a solid-color image (content is irrelevant;
    the shape/size contract is what is under test).
    """

    def __init__(self, meta: SlideMeta):
        self.level_count = meta.level_count
        self.level_downsamples = list(meta.level_downsamples)
        self.level_dimensions = list(meta.level_dimensions)
        self.properties = {"openslide.vendor": "fake"}
        self._levels = [
            Image.new("RGB", (w, h), (200, 60, 60))
            for (w, h) in meta.level_dimensions
        ]
        self.read_calls: list[tuple] = []
        self.closed = False

    @property
    def dimensions(self):
        return self.level_dimensions[0]

    def read_region(self, location, level, size):
        self.read_calls.append((tuple(location), level, tuple(size)))
        img = self._levels[level]
        ds = self.level_downsamples[level]
        x = int(location[0] / ds)
        y = int(location[1] / ds)
        w, h = int(size[0]), int(size[1])
        return img.crop((x, y, x + w, y + h))

    def get_thumbnail(self, size):
        return self._levels[0].resize(tuple(int(s) for s in size))

    def close(self):
        self.closed = True


@pytest.fixture()
def viewer():
    fake = FakeSlide(BIG)
    v = SlideViewer(slide=fake)
    try:
        v.wait()
        yield v
    finally:
        v.close()


# -------------------------------------------------------------- two-stage
def test_multi_chunk_plan_pushes_center_then_full(viewer):
    tile_pushes: list[dict] = []
    geo_pushes: list[dict] = []
    viewer.observe(lambda c: tile_pushes.append(c["new"]), names="tiles")
    viewer.observe(lambda c: geo_pushes.append(c["new"]), names="tile_geo")
    fake_reads = viewer.backend._os.read_calls  # the FakeSlide behind the backend
    fake_reads.clear()

    # zoom 1.0, 960x540 default canvas, fit center (2048, 2048): level 0,
    # 4 blocks around the center
    vp = Viewport(2048, 2048, 1.0, canvas_w=960, canvas_h=540)
    viewer.set_zoom(1.0)
    plan = plan_viewport(BIG, vp, tile_size=256)
    assert plan.level == 0
    assert len(plan.chunks) == 4

    # two pushes: center chunk, then the full set
    assert len(tile_pushes) == 2, [len(p) for p in tile_pushes]
    first, full = tile_pushes
    center_keys = {key(t.key) for t in plan.chunks[0].tiles}
    assert set(first) == center_keys
    assert set(full) == {key(t.key) for t in plan.tiles}
    assert set(first) < set(full)

    # both pushes carry absolute level-px geometry, identical for shared
    # tiles; the final state is the full set
    assert geo_pushes[0] == {k: geo_pushes[1][k] for k in geo_pushes[0]}
    assert geo_pushes[1] == expected_geo(plan)
    assert viewer.tiles == full
    assert viewer.tile_geo == expected_geo(plan)

    # one read per chunk, center chunk first (then cached in the full pass)
    assert len(fake_reads) == len(plan.chunks)
    for (loc, level, size), c in zip(fake_reads, plan.chunks):
        assert (loc, level, size) == (c.loc, plan.level, c.size)


def test_single_chunk_plan_pushes_once_with_single_read(viewer):
    tile_pushes: list[dict] = []
    viewer.observe(lambda c: tile_pushes.append(c["new"]), names="tiles")
    fake_reads = viewer.backend._os.read_calls
    fake_reads.clear()

    # zoom 4.0 around (1536, 1536): l0 240x135 inside block (1,1)
    # -> one push, one read
    vp = Viewport(1536, 1536, 4.0, canvas_w=960, canvas_h=540)
    viewer.set_zoom(4.0, cx=1536, cy=1536)
    plan = plan_viewport(BIG, vp, tile_size=256)
    assert plan.level == 0
    assert len(plan.chunks) == 1

    assert len(tile_pushes) == 1
    assert set(tile_pushes[0]) == {key(t.key) for t in plan.tiles}
    assert viewer.tile_geo == expected_geo(plan)
    # the single read is the grid-anchored block (clamped to level bounds)
    assert fake_reads == [(plan.chunks[0].loc, 0, plan.chunks[0].size)]


# ---------------------------------------------------------- coalescing
def test_rapid_js_viewports_coalesce_to_final_plan(viewer):
    """A burst of JS-originated (comm) viewport updates must end in the
    final viewport's plan — superseded viewports may be skipped, but the
    last one must always be rendered (render-loop handoff)."""
    n = 8
    for i in range(n):
        viewer.viewport = {
            "cx": 1200 + i * 200, "cy": 2000, "zoom": 1.0,
            "canvas_w": 960, "canvas_h": 540,
        }
        time.sleep(0.002)
    deadline = time.monotonic() + 10
    while viewer._rendering_bg and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not viewer._rendering_bg
    final = Viewport(1200 + (n - 1) * 200, 2000, 1.0, canvas_w=960, canvas_h=540)
    plan = plan_viewport(BIG, final, tile_size=256)
    assert set(viewer.tiles) == {key(t.key) for t in plan.tiles}
    assert viewer.tile_geo == expected_geo(plan)
