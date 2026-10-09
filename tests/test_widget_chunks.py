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
from PIL import Image, ImageDraw
from islide import SlideViewer
from islide.fetch import fetch_chunk
from islide.plan import anchor_l0, plan_viewport
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


def missing_union(plan, tiles):
    """The read that satisfies exactly ``tiles``: (level-0 location,
    level-px size) of the grid-aligned union of their clamped cells,
    absolute level px — what ``fetch_chunk`` asks ``read_region`` for."""
    rx, ry = plan.read_origin
    x0 = min(rx + t.crop[0] for t in tiles)
    y0 = min(ry + t.crop[1] for t in tiles)
    x1 = max(rx + t.crop[2] for t in tiles)
    y1 = max(ry + t.crop[3] for t in tiles)
    ds = plan.downsample
    return (anchor_l0(x0, ds), anchor_l0(y0, ds)), (x1 - x0, y1 - y0)


class FakeSlide:
    """Multi-level slide for a custom slide library (duck-typed openslide
    OO API). ``read_region(location, level, size)`` follows openslide's
    semantics: location in level-0 px, size in level px; out-of-bounds is
    filled (PIL's crop pads), as the vendor fills.

    Each pyramid level is a deterministic row-stripe image (row ``y`` is
    gray ``y % 256``): shape/size is what most tests check, but the stripe
    pattern makes a misaligned crop encode differently, so the
    byte-identity assertions catch a wrong region.
    """

    def __init__(self, meta: SlideMeta):
        self.level_count = meta.level_count
        self.level_downsamples = list(meta.level_downsamples)
        self.level_dimensions = list(meta.level_dimensions)
        self._levels = [
            self._stripe(w, h) for (w, h) in meta.level_dimensions
        ]
        self.properties = {"openslide.vendor": "fake"}
        self.read_calls: list[tuple] = []
        self.closed = False

    @staticmethod
    def _stripe(w: int, h: int) -> Image.Image:
        img = Image.new("L", (w, h))
        d = ImageDraw.Draw(img)
        for y in range(h):
            d.line([(0, y), (w, y)], fill=y % 256)
        return img.convert("RGB")

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

    # one read per chunk — the union of the chunk's tiles' cells — center
    # chunk first (each push follows its own read; nothing is read twice)
    assert len(fake_reads) == len(plan.chunks)
    for (loc, level, size), c in zip(fake_reads, plan.chunks):
        want = missing_union(plan, c.tiles)
        assert (loc, level, size) == (want[0], plan.level, want[1])


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
    # the single read is the union of the chunk's tiles' cells (clamped to
    # level bounds) — not the whole block
    want = missing_union(plan, plan.chunks[0].tiles)
    assert fake_reads == [(want[0], 0, want[1])]


def test_partial_miss_reads_only_the_missing_tiles(viewer):
    """A chunk whose cache lost some tiles (LRU eviction / level switch)
    is satisfied by one read of just the missing tiles' cells — not the
    whole block — and the re-read tiles come back byte-identical to the
    cold fetch (same pixels, same crop, same encoding)."""
    fake = viewer.backend._os
    vp = Viewport(2048, 2048, 1.0, canvas_w=960, canvas_h=540)
    plan = plan_viewport(BIG, vp, tile_size=256)
    assert plan.level == 0
    cache = viewer.cache

    cold: dict = {}
    for chunk in plan.chunks:
        cold.update(fetch_chunk(viewer.backend, cache, chunk, plan))

    # drop the top row of the first chunk's 2x2 tiles from the cache
    c = plan.chunks[0]
    assert len(c.tiles) == 4
    top = min(t.key[2] for t in c.tiles)
    evicted = [t for t in c.tiles if t.key[2] == top]
    for t in evicted:
        del cache._data[t.key]

    fake.read_calls.clear()
    out = fetch_chunk(viewer.backend, cache, c, plan)
    assert len(fake.read_calls) == 1  # the hits cost no read
    want = missing_union(plan, evicted)
    assert fake.read_calls[0] == (want[0], plan.level, want[1])
    # strictly smaller than the chunk's block rect (the old behavior read
    # the whole block, missing or not)
    assert want[1] < c.size
    # re-read tiles are byte-identical to the cold fetch; hits as-is
    for t in c.tiles:
        assert out[t.key] == cold[t.key]


# --- coalescing: one push per viewport change, even for rapid changes ---
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
            "canvas_w": 960,
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
