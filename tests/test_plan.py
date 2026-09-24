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
        "ds", [1.0, 2.0000808194891597, 16.01229444470048, 128.68154869933454]
    )
    @pytest.mark.parametrize("p", [0, 1, 7, 99, 100, 256, 9999])
    def test_roundtrip(self, ds, p):
        """floor(anchor_l0(p, ds) / ds) == p — the exact inverse of the vendor's
        floor(int_loc / ds) location mapping (verified against libopenslide)."""
        assert math.floor(anchor_l0(p, ds) / ds) == p

    @pytest.mark.parametrize(
        "ds", [1.0, 2.0000808194891597, 16.01229444470048]
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
        assert plan.loc == (256, 0)  # ds == 1: level-0 anchor is exact
        assert plan.size == (512, 512)
        assert plan.tiles

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
        assert plan.size == (0, 0)

    def test_corner_clamped(self):
        plan = plan_viewport(META, vp(0, 0, 1.0), tile_size=256)
        assert plan.loc == (0, 0)
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
        assert plan.size[0] <= lw and plan.size[1] <= lh
        assert plan.loc[0] >= 0 and plan.loc[1] >= 0

    def test_crops_inside_read(self):
        plan = plan_viewport(META, vp(512, 256, 0.5), tile_size=256)
        for t in plan.tiles:
            x0, y0, x1, y1 = t.crop
            assert 0 <= x0 < x1 <= plan.size[0]
            assert 0 <= y0 < y1 <= plan.size[1]
