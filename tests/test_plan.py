"""Unit tests for the pure planning math (no openslide needed)."""
import math

import pytest

from islide.plan import anchor_l0, plan_viewport, select_level
from islide.viewport import SlideMeta, Viewport

from util import screen_covered


def make_meta() -> SlideMeta:
    return SlideMeta(
        dimensions=(1024, 512),
        level_count=3,
        level_downsamples=(1.0, 2.0, 4.0),
        level_dimensions=((1024, 512), (512, 256), (256, 128)),
    )


META = make_meta()


def vp(cx, cy, zoom, w=400, h=200) -> Viewport:
    return Viewport(cx=cx, cy=cy, zoom=zoom, canvas_w=w, canvas_h=h)


def _read_size(plan) -> tuple[int, int]:
    """The union read rect's size in level px (extent of the tile crops,
    which are relative to ``plan.read_origin``)."""
    return (
        max(t.crop[2] for t in plan.tiles),
        max(t.crop[3] for t in plan.tiles),
    )


def _read_extent(plan) -> tuple[tuple[int, int], tuple[int, int]]:
    """The union read rect as absolute level-px corners (top-left, bottom-right)."""
    rx, ry = plan.read_origin
    rw, rh = _read_size(plan)
    return (rx, ry), (rx + rw, ry + rh)


class TestSelectLevel:
    def test_zoom_at_or_above_full_res(self):
        assert select_level((1.0, 2.0, 4.0), 4.0) == 0
        assert select_level((1.0, 2.0, 4.0), 1.0) == 0

    def test_mid_zoom(self):
        assert select_level((1.0, 2.0, 4.0), 0.5) == 1
        assert select_level((1.0, 2.0, 4.0), 0.25) == 2

    def test_below_pyramid_range_falls_back_to_coarsest(self):
        assert select_level((1.0, 2.0, 4.0), 0.01) == 2


class TestAnchor:
    @pytest.mark.parametrize(
        "ds", [1.0, 4.000121536217793, 32.01432201760585, 257.06193261173183]
    )
    @pytest.mark.parametrize("p", [0, 1, 7, 99, 100, 256, 9999])
    def test_roundtrip(self, ds, p):
        """floor(anchor_l0(p, ds) / ds) == p — the exact inverse of the vendor's
        floor(int_loc / ds) location mapping (verified against libopenslide)."""
        assert math.floor(anchor_l0(p, ds) / ds) == p

    @pytest.mark.parametrize(
        "ds", [1.0, 4.000121536217793, 32.01432201760585]
    )
    @pytest.mark.parametrize("p", [1, 100, 9999])
    def test_minimal(self, ds, p):
        """The anchor is the smallest level-0 location mapping to p."""
        assert math.floor((anchor_l0(p, ds) - 1) / ds) <= p - 1


class TestPlan:
    def test_center_level0(self):
        # viewport l0 = (312, 156, 712, 356); read rect is the union of the
        # overlapping 256-px grid cells: x in [256, 768), y in [0, 512)
        plan = plan_viewport(META, vp(512, 256, 1.0), tile_size=256)
        assert plan.level == 0
        assert plan.read_origin == (256, 0)
        assert plan.tiles
        # the tiles' crops cover the read rect: 256..768 x 0..512 (level px)
        assert _read_extent(plan) == ((256, 0), (768, 512))

    @pytest.mark.parametrize("zoom", [4.0, 1.0, 0.5, 0.25, 0.05])
    def test_covers_canvas(self, zoom):
        """Sampled on-slide screen points are all covered by some tile."""
        W, H = META.dimensions
        plan = plan_viewport(META, vp(512, 256, zoom), tile_size=256)
        assert plan.tiles
        x0, y0, x1, y1 = vp(512, 256, zoom).l0_bbox
        checked = 0
        for sx in range(0, 400, 13):
            for sy in range(0, 200, 13):
                lx, ly = x0 + sx / zoom, y0 + sy / zoom
                if not (0 <= lx < W and 0 <= ly < H):
                    continue  # off-slide: not required to be covered
                assert screen_covered(plan, sx, sy), (zoom, sx, sy)
                checked += 1
        assert checked > 0

    def test_off_slide_no_tiles(self):
        plan = plan_viewport(META, vp(5000, 256, 1.0), tile_size=256)
        assert plan.tiles == ()
        assert plan.chunks == ()
        assert plan.read_origin == (0, 0)

    def test_corner_clamped(self):
        plan = plan_viewport(META, vp(0, 0, 1.0), tile_size=256)
        assert plan.read_origin == (0, 0)
        assert plan.tiles
        # nothing is drawn left of the canvas edge (off-slide)
        assert all(t.screen[0] >= -1e-9 for t in plan.tiles)
        # the on-slide quarter of the canvas is covered (slide occupies the
        # canvas's bottom-right quadrant here)
        assert screen_covered(plan, 250, 150)
        assert not screen_covered(plan, 50, 50)  # off-slide: uncovered

    def test_tile_grid_global_across_zoom(self):
        """Same center at two zooms -> same (level, tx, ty) tile keys,
        so cache keys are stable under pan/zoom."""
        p1 = plan_viewport(META, vp(512, 256, 1.0), tile_size=256)
        p2 = plan_viewport(META, vp(512, 256, 2.0), tile_size=256)
        assert p1.level == p2.level == 0
        assert {t.key for t in p1.tiles} == {t.key for t in p2.tiles}

    def test_coarse_zoom_uses_upper_level(self):
        plan = plan_viewport(META, vp(512, 256, 0.25), tile_size=256)
        assert plan.level == 2
        lw, lh = META.level_dimensions[2]
        rx, ry = plan.read_origin
        assert rx >= 0 and ry >= 0
        _, (rx1, ry1) = _read_extent(plan)
        assert rx1 <= lw and ry1 <= lh

    def test_crops_inside_read(self):
        plan = plan_viewport(META, vp(512, 256, 0.5), tile_size=256)
        rw, rh = _read_size(plan)
        for t in plan.tiles:
            x0, y0, x1, y1 = t.crop
            assert 0 <= x0 < x1 <= rw
            assert 0 <= y0 < y1 <= rh


def make_big_meta() -> SlideMeta:
    return SlideMeta(
        dimensions=(4096, 4096),
        level_count=4,
        level_downsamples=(1.0, 2.0, 4.0, 8.0),
        level_dimensions=((4096, 4096), (2048, 2048), (1024, 1024), (512, 512)),
    )


BIG = make_big_meta()


class TestChunking:
    """The tile set is partitioned into 1024-px (4x4-tile) blocks,
    each an independent read unit, ordered center-first (docs/DESIGN.md §6.6)."""

    def test_single_chunk_when_plan_fits_one_block(self):
        # 4096-wide level, zoom 4 around one grid cell: one block
        plan = plan_viewport(BIG, vp(1900, 2000, 4.0), tile_size=256)
        assert plan.level == 0
        assert len(plan.chunks) == 1
        c = plan.chunks[0]
        assert c.tiles == plan.tiles
        assert c.read_origin == (1024, 1024)  # block (1,1) on the 1024 grid
        assert c.size == (1024, 1024)
        assert c.loc == (1024, 1024)  # ds == 1: the anchor is exact
        # the chunk read rect contains the union read rect (one covering
        # read per block; a single-block plan reads its whole block)
        rw, rh = _read_size(plan)
        assert c.read_origin[0] <= plan.read_origin[0]
        assert c.read_origin[1] <= plan.read_origin[1]
        assert c.read_origin[0] + c.size[0] >= plan.read_origin[0] + rw
        assert c.read_origin[1] + c.size[1] >= plan.read_origin[1] + rh

    def test_chunks_partition_the_tile_set(self):
        plan = plan_viewport(BIG, vp(1900, 2000, 1.0), tile_size=256)
        assert len(plan.chunks) == 4
        # partition: every tile in exactly one chunk (block grouping, so
        # chunk order is not tile order)
        in_chunks = [t.key for c in plan.chunks for t in c.tiles]
        assert sorted(in_chunks) == sorted(t.key for t in plan.tiles)
        assert len(in_chunks) == len(plan.tiles)

    def test_chunk_read_is_grid_anchored_block(self):
        plan = plan_viewport(BIG, vp(1900, 2000, 1.0), tile_size=256)
        ds = plan.downsample
        for c in plan.chunks:
            # grid-anchored: block origins on the 1024-px grid (level 0)
            assert c.read_origin[0] % 1024 == 0
            assert c.read_origin[1] % 1024 == 0
            assert c.size[0] <= 1024 and c.size[1] <= 1024
            assert c.loc == (anchor_l0(c.read_origin[0], ds),
                             anchor_l0(c.read_origin[1], ds))
            # the chunk's screen rect is the union of its tiles' screen
            # rects (left, top, width, height)
            l = min(t.screen[0] for t in c.tiles)
            tp = min(t.screen[1] for t in c.tiles)
            r = max(t.screen[0] + t.screen[2] for t in c.tiles)
            b = max(t.screen[1] + t.screen[3] for t in c.tiles)
            assert c.screen == (l, tp, r - l, b - tp)

    def test_tile_crops_land_inside_the_chunk_read(self):
        """fetch_chunk crops tiles from the chunk read: every tile crop,
        re-based onto the chunk origin, must fit the chunk rect."""
        plan = plan_viewport(BIG, vp(1900, 2000, 1.0), tile_size=256)
        rx, ry = plan.read_origin
        for c in plan.chunks:
            cx, cy = c.read_origin
            for t in c.tiles:
                x0, y0, x1, y1 = t.crop
                lx0, ly0 = rx + x0 - cx, ry + y0 - cy
                lx1, ly1 = rx + x1 - cx, ry + y1 - cy
                assert 0 <= lx0 < lx1 <= c.size[0]
                assert 0 <= ly0 < ly1 <= c.size[1]

    def test_center_first_ordering(self):
        # view (1700..2100) x (1900..2100): blocks (1,1), (2,1), (1,2),
        # (2,2); distances from (1900, 2000) to block centers differ, so
        # the sort is fully determined
        plan = plan_viewport(BIG, vp(1900, 2000, 1.0), tile_size=256)
        assert plan.level == 0
        assert [c.read_origin for c in plan.chunks] == [
            (1024, 1024), (1024, 2048), (2048, 1024), (2048, 2048)
        ]
        vx, vy = 1900.0, 2000.0
        dists = [
            (c.read_origin[0] + c.size[0] / 2 - vx) ** 2
            + (c.read_origin[1] + c.size[1] / 2 - vy) ** 2
            for c in plan.chunks
        ]
        assert dists == sorted(dists)

    def test_edge_block_clamped_to_level_bounds(self):
        rect = SlideMeta(
            dimensions=(5000, 3000),
            level_count=3,
            level_downsamples=(1.0, 2.0, 4.0),
            level_dimensions=((5000, 3000), (2500, 1500), (1250, 750)),
        )
        plan = plan_viewport(rect, vp(4900, 2900, 1.0), tile_size=256)
        assert plan.level == 0
        assert len(plan.chunks) == 1
        c = plan.chunks[0]
        # block (4,2): origin (4096, 2048), clamped to the level corner
        assert c.read_origin == (4096, 2048)
        assert c.size == (904, 952)  # (5000-4096, 3000-2048)
        assert c.tiles == plan.tiles
