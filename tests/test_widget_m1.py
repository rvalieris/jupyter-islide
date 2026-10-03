"""M1 `SlideViewer` (custom DOMWidget) tests — headless (no JS view).

Covers: background open + wait(), synced-state contract (tiles/tile_geo),
programmatic viewport API, cache invariance across panning, error path,
close(), and the cross-language trait contract with frontend/defaults.js.
"""
from __future__ import annotations

import re
import time
from pathlib import Path

import ipywidgets as widgets
import pytest

from islide import SlideViewer, SlideMeta
from islide.plan import plan_viewport

from util import SLIDE_PATH

SLIDE = str(SLIDE_PATH)

try:
    import openslide  # noqa: F401
    HAS_OPENSLLIDE = True
except ImportError:
    HAS_OPENSLLIDE = False

pytestmark = [
    pytest.mark.skipif(not HAS_OPENSLLIDE, reason="openslide-python not installed"),
]

REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_DEFAULTS = REPO_ROOT / "frontend" / "defaults.js"

IDENTITY_TRAITS = {
    "_model_name",
    "_view_name",
    "_model_module",
    "_view_module",
    "_model_module_version",
    "_view_module_version",
}


@pytest.fixture()
def viewer(slide_path):
    v = SlideViewer(slide_path)
    yield v
    v.close()


# ------------------------------------------------------------------ identity
def test_widget_identity_matches_frontend():
    from traitlets import TraitType

    cls = SlideViewer
    assert cls._model_name.default() == "SlideModel"
    assert cls._view_name.default() == "SlideView"
    assert cls._model_module.default() == "jupyter-islide"
    assert cls._view_module.default() == "jupyter-islide"
    assert cls._model_module_version.default() == "2.0.0"
    assert cls._view_module_version.default() == "2.0.0"
    # every synced identity/data trait has the sync tag
    for name, trait in vars(cls).items():
        if isinstance(trait, TraitType):
            assert trait.metadata.get("sync") is True, name


def test_synced_data_traits_match_frontend_defaults():
    """The JS model defaults and the Python sync traits are one contract."""
    from traitlets import TraitType

    py_data = {
        name
        for name, trait in vars(SlideViewer).items()
        if isinstance(trait, TraitType) and name not in IDENTITY_TRAITS
    }
    js_text = FRONTEND_DEFAULTS.read_text()
    block = js_text[js_text.index("= {") + 3 : js_text.index("};")]
    js_data = set(re.findall(r"^\s*([a-z_]+):", block, re.M))
    assert py_data == js_data


# ------------------------------------------------------------------- open API
def test_background_open_and_wait(viewer):
    viewer.wait()
    assert viewer.slide_open
    assert viewer.backend is not None
    # after the initial render the status line carries the render info
    # (the live zoom/µm-px live in the JS readout, not the status)
    assert "level" in viewer.status and "tiles" in viewer.status


def test_slide_kwarg_accepts_opened_openslide_object(slide_path):
    """A pre-opened openslide object (e.g. from a same-API custom lib)
    can be passed instead of a path; the viewer closes it on close()."""
    import openslide

    os_slide = openslide.open_slide(slide_path)
    v = SlideViewer(slide=os_slide)
    try:
        v.wait()
        assert v.slide_open
        assert v.path == slide_path  # best-effort path lifted from the object
        assert v.meta["dimensions"] == [46000, 32914]
        assert len(v.render().tiles) > 0
    finally:
        v.close()
    # the backend closed the passed-in object
    with pytest.raises(Exception) as err:
        os_slide.read_region((0, 0), 0, (8, 8))
    assert "closed" in str(err.value)


def test_meta_trait_shape(viewer):
    viewer.wait()
    meta = viewer.meta
    assert set(meta) == {
        "dimensions", "level_count", "level_downsamples",
        "level_dimensions", "mpp", "vendor",
    }
    assert meta["dimensions"] == [46000, 32914]
    assert meta["level_count"] == 9
    assert meta["level_downsamples"][0] == 1.0
    assert len(meta["level_dimensions"]) == 9
    assert meta["mpp"] == 1000


def test_headless_default_viewport_is_fit(viewer):
    viewer.wait()
    vp = viewer.viewport
    assert vp is not None
    assert set(vp) == {"cx", "cy", "zoom", "canvas_w", "canvas_h"}
    assert vp["cx"] == 46000 / 2 and vp["cy"] == 32914 / 2
    assert vp["canvas_w"] == 960 and vp["canvas_h"] == 540
    expected_fit = min(960 / 46000, 540 / 32914)
    assert vp["zoom"] == pytest.approx(expected_fit)


def test_constructor_canvas_h_sets_trait_and_fit_viewport(slide_path):
    v = SlideViewer(slide_path, canvas_h=800)
    try:
        v.wait()
        assert v.canvas_h == 800
        vp = v.viewport
        assert vp["canvas_w"] == 960 and vp["canvas_h"] == 800
        assert vp["zoom"] == pytest.approx(min(960 / 46000, 800 / 32914))
    finally:
        v.close()


def test_runtime_canvas_h_rebases_viewport_and_replans(viewer):
    """Headless: changing canvas_h re-bases the shared viewport onto the
    taller canvas and the background re-render picks it up (a new tiles
    dict is pushed, even if the tile set happens to be identical)."""
    from traitlets import TraitError

    viewer.wait()
    with pytest.raises(TraitError):
        viewer.canvas_h = 0  # rejected, keeps the old value
    assert viewer.canvas_h == 540
    viewer.set_zoom(2.0)
    assert viewer.viewport_bbox()[3] - viewer.viewport_bbox()[1] == pytest.approx(540 / 2)
    tiles_before = viewer.tiles
    viewer.canvas_h = 800
    assert viewer.viewport["canvas_h"] == 800
    # l0 view height follows the canvas: 540/2 -> 800/2 at zoom 2.0
    assert viewer.viewport_bbox()[3] - viewer.viewport_bbox()[1] == pytest.approx(800 / 2)
    deadline = time.monotonic() + 10
    while viewer.tiles is tiles_before and time.monotonic() < deadline:
        time.sleep(0.01)
    assert viewer.tiles is not tiles_before  # background re-render ran


def test_minimap_is_jpeg_data_url(viewer):
    viewer.wait()
    assert viewer.minimap_img.startswith("data:image/jpeg;base64,")


# ------------------------------------------------------------------- tiles
def _key_of(key: str) -> tuple[int, int, int]:
    level, tx, ty = key.split(":")
    return int(level), int(tx), int(ty)


def test_tiles_and_tile_geo_contract(viewer):
    viewer.wait()
    plan = viewer.render()
    tiles, geo = viewer.tiles, viewer.tile_geo
    assert set(tiles) == set(geo)
    assert len(tiles) == len(plan.tiles) and len(tiles) > 0
    for key, url in tiles.items():
        assert url.startswith("data:image/jpeg;base64,")
        level, tx, ty = _key_of(key)
        g = geo[key]
        assert len(g) == 5
        glevel, ox, oy, cw, ch = g
        assert glevel == level
        # crop is inside the level bounds
        lw, lh = viewer.meta["level_dimensions"][level]
        assert 0 <= ox < lw and 0 <= oy < lh
        assert cw > 0 and ch > 0
        assert ox + cw <= lw and oy + ch <= lh
    # geo matches the plan exactly (level-space crop origins)
    for t in plan.tiles:
        k = f"{t.key[0]}:{t.key[1]}:{t.key[2]}"
        x0, y0, x1, y1 = t.crop
        rx, ry = plan.read_origin
        assert geo[k] == [t.key[0], rx + x0, ry + y0, x1 - x0, y1 - y0]


def test_revisit_after_pan_serves_identical_tiles(viewer):
    """Cache invariant end-to-end through the widget: revisiting a
    panned viewport must produce byte-identical tile payloads."""
    viewer.wait()
    first = dict(viewer.tiles)
    viewer.center_on(23000, 16457)
    viewer.set_zoom(2.0)
    mid = dict(viewer.tiles)
    viewer.center_on(20000, 20000)
    viewer.set_zoom(4.0)
    viewer.center_on(23000, 16457)
    viewer.set_zoom(2.0)
    again = dict(viewer.tiles)
    # 1:1-ish center revisit of the same viewport -> identical payload
    assert again == mid
    # panning changed something (otherwise the test is vacuous)
    assert set(first) != set(again)


# ------------------------------------------------------------ programmatic
def test_set_zoom_and_center_on(viewer):
    viewer.wait()
    vp = viewer.set_zoom(2.0)
    assert vp.zoom == pytest.approx(2.0)
    assert viewer.viewport["zoom"] == pytest.approx(2.0)
    vp = viewer.center_on(23000, 16457)
    assert vp.cx == pytest.approx(23000)
    assert vp.cy == pytest.approx(16457)


def test_zoom_clamps(viewer):
    viewer.wait()
    fit = min(960 / 46000, 540 / 32914)
    assert viewer.set_zoom(1e9).zoom == pytest.approx(16.0)
    assert viewer.set_zoom(1e-9).zoom == pytest.approx(fit / 4.0)


def test_viewport_bbox_and_read_crop(viewer):
    viewer.wait()
    viewer.set_zoom(1.0)
    viewer.center_on(23000, 16457)
    x0, y0, x1, y1 = viewer.viewport_bbox()
    assert (x0, y0, x1, y1) == (22520, 16187, 23480, 16727)
    img = viewer.read_crop((x0, y0, x1, y1))
    assert img.size == (x1 - x0, y1 - y0)
    assert img.mode == "RGBA"


def test_render_returns_readplan(viewer):
    viewer.wait()
    plan = viewer.render()
    assert plan.level >= 0
    assert len(plan.tiles) == len(viewer.tiles)


def test_js_originated_viewport_triggers_background_render(viewer):
    viewer.wait()
    before = viewer.status
    # Simulate the JS view: set the trait directly (what a comm update does).
    viewer.viewport = {
        "cx": 23000, "cy": 16457, "zoom": 2.0, "canvas_w": 960, "canvas_h": 540
    }
    deadline = time.monotonic() + 10
    while viewer.status == before and time.monotonic() < deadline:
        time.sleep(0.01)
    assert before != viewer.status  # background render reported the new plan
    assert viewer.tiles  # repopulated by the background render


# ------------------------------------------------------------------- errors
def test_bad_path_reports_error_and_wait_raises():
    v = SlideViewer("/nonexistent/slide.svs")
    with pytest.raises(RuntimeError, match="failed to open slide"):
        v.wait()
    assert v.slide_open is False
    assert v.status.startswith("error opening slide")
    v.close()
    v.close()  # idempotent


def test_close_is_idempotent(viewer):
    viewer.wait()
    viewer.close()
    viewer.close()


def test_is_domwidget_subclass():
    assert issubclass(SlideViewer, widgets.DOMWidget)
