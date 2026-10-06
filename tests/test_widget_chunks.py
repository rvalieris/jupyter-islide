"""Canvas-viewer tests — headless, no openslide.

Covers the per-chunk push over the canvas widget (docs/DESIGN.md §6.6.2):
a multi-chunk plan pushes one chunk at a time in the plan's
viewport-center-first order (each push's geometry is the chunk's absolute
level-px crops; the union of the render's pushes is the full viewport
set, the traits hold the last chunk — last-event semantics); a
single-chunk plan pushes once with a single grid-anchored read (the
single-push wire contract); reads happen center-first in chunk order;
and a burst of JS-originated viewports coalesces to a final state
matching the final plan (the render-loop handoff).
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
        yield v
    finally:
        v.close()


# ------------------------------------------------------------ per-chunk
def test_multi_chunk_plan_pushes_one_chunk_at_a_time(viewer):
    """Multi-chunk: each chunk is read, encoded, and pushed as its own
    ``tiles``/``tile_geo`` assignment as soon as its read lands — the
    viewport-center chunk first, the rest in center-out order. The
    traits hold the last chunk (last-event semantics); the union of the
    render's pushes is the full viewport set."""
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

    # one tiles push per chunk, in the plan's center-first order
    assert len(tile_pushes) == len(plan.chunks), [len(p) for p in tile_pushes]
    for i, chunk in enumerate(plan.chunks):
        assert set(tile_pushes[i]) == {key(t.key) for t in chunk.tiles}
        for k in tile_pushes[i]:
            assert tile_pushes[i][k].startswith("data:image/jpeg;base64,")
    # the union of the pushes is the full viewport set; the traits hold
    # the last chunk
    union = {}
    for p in tile_pushes:
        union.update(p)
    assert set(union) == {key(t.key) for t in plan.tiles}
    assert viewer.tiles == tile_pushes[-1]

    # each push carries absolute level-px geometry for its chunk (the
    # plan's full geometry, per chunk); the trait holds the last chunk's
    assert len(geo_pushes) == len(plan.chunks)
    geo = expected_geo(plan)
    merged_geo = {}
    for i, chunk in enumerate(plan.chunks):
        want = {key(t.key): geo[key(t.key)] for t in chunk.tiles}
        assert geo_pushes[i] == want
        merged_geo.update(geo_pushes[i])
    assert merged_geo == geo
    assert viewer.tile_geo == geo_pushes[-1]

    # one read per chunk, center chunk first (each push follows its own
    # read; nothing is read twice)
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
    tile_pushes: list[dict] = []
    viewer.observe(lambda c: tile_pushes.append(c["new"]), names="tiles")
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
    # the final render's per-chunk pushes cover the final plan; the
    # traits hold its last chunk (last-event semantics)
    final_pushes = tile_pushes[-len(plan.chunks):]
    assert len(final_pushes) == len(plan.chunks)
    union = {}
    for p in final_pushes:
        union.update(p)
    assert set(union) == {key(t.key) for t in plan.tiles}
    assert viewer.tiles == final_pushes[-1]


# -------------------------------------------------------------- lifecycle
def test_close_releases_backend_and_is_idempotent(viewer):
    fake = viewer.backend._os
    viewer.close()
    assert fake.closed
    assert viewer.backend is None and not viewer.slide_open
    viewer.close()  # idempotent


def test_frontend_comm_close_releases_backend(viewer):
    """Frontend disposal (e.g. clearing the cell output) sends a comm
    close with no Python-side dispose hook in ipywidgets 8; the
    on_close handler registered in the constructor releases the
    kernel-side slide handle (docs/DESIGN.md §5.2)."""
    fake = viewer.backend._os
    viewer.comm.on_close({})  # what the comm machinery calls
    assert fake.closed
    assert viewer.backend is None and not viewer.slide_open


def test_close_waits_for_inflight_background_render(viewer):
    """close() must not close the slide handle out from under a running
    background render: it blocks until the render thread finishes."""
    viewer.set_zoom(2.0)  # schedules a background render
    viewer.close()
    assert viewer.backend is None
    assert not viewer._rendering_bg
