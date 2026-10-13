"""Viewport wire-boundary tests — headless, no openslide.

The ``viewport`` trait is public and ``sync=True``, so a dict can reach
planning from the JS view, from a restored notebook state, or from user
code (``viewer.viewport = {...}``). Covers: zoom clamped to the same
floor/ceiling the Python-side API applies (``zoom=0`` used to divide by
zero inside planning), malformed values reported (warn + ``status``)
instead of raising in the render thread, the last-good-viewport fallback,
and a render failure that neither escapes the render thread nor kills the
render loop.
"""
from __future__ import annotations

import time
import warnings

import pytest
from PIL import Image

from islide import SlideViewer
from islide.viewport import SlideMeta, fit_zoom


META = SlideMeta(
    dimensions=(2048, 2048),
    level_count=2,
    level_downsamples=(1.0, 2.0),
    level_dimensions=((2048, 2048), (1024, 1024)),
)


class FakeSlide:
    """Minimal duck-typed openslide object (blank levels; shapes only)."""

    def __init__(self, meta: SlideMeta):
        self.level_count = meta.level_count
        self.level_downsamples = list(meta.level_downsamples)
        self.level_dimensions = list(meta.level_dimensions)
        self.properties = {"openslide.vendor": "fake"}
        self.closed = False

    @property
    def dimensions(self):
        return self.level_dimensions[0]

    def read_region(self, location, level, size):
        return Image.new("RGB", (max(1, int(size[0])), max(1, int(size[1]))))

    def get_thumbnail(self, size):
        return Image.new("RGB", tuple(max(1, int(s)) for s in size))

    def close(self):
        self.closed = True


@pytest.fixture()
def viewer():
    v = SlideViewer(slide=FakeSlide(META))
    try:
        yield v
    finally:
        v.close()


def wire(viewer, **over) -> dict:
    """A wire-shaped viewport dict (the ``_vp_dict`` form) with overrides."""
    vp = viewer._vp_current
    d = {"cx": vp.cx, "cy": vp.cy, "zoom": vp.zoom, "canvas_w": vp.canvas_w}
    d.update(over)
    return d


def floor_of(canvas_w: int = 960, canvas_h: int = 540) -> float:
    """The zoom floor: fit zoom / 4 (the JS ``MIN_ZOOM_PAD``)."""
    return fit_zoom(META, canvas_w, canvas_h) / 4.0


def wait_idle(viewer, timeout: float = 10.0) -> None:
    """Wait for a background render (started by the viewport observer)."""
    end = time.monotonic() + timeout
    while viewer._rendering_bg and time.monotonic() < end:
        time.sleep(0.005)
    assert not viewer._rendering_bg, "background render did not finish"


# ------------------------------------------------------------- zoom clamping
def test_valid_wire_viewport_passes_through(viewer):
    """In-range values are not rewritten — the boundary must not fight the
    JS view's own transform."""
    viewer.viewport = wire(viewer, cx=1000.0, cy=900.0, zoom=1.0)
    vp = viewer._vp_current
    assert (vp.cx, vp.cy, vp.zoom, vp.canvas_w) == (1000.0, 900.0, 1.0, 960)


def test_wire_zoom_zero_clamps_to_floor(viewer):
    """``zoom=0`` used to reach ``need = 1.0 / zoom`` in planning and raise
    ``ZeroDivisionError`` in the render thread."""
    viewer.viewport = wire(viewer, zoom=0.0)
    plan = viewer.render()
    assert viewer._vp_current.zoom == pytest.approx(floor_of())
    assert plan.tiles


def test_wire_zoom_out_of_range_clamps_both_ways(viewer):
    viewer.viewport = wire(viewer, zoom=floor_of() / 1000.0)
    assert viewer._vp_current.zoom == pytest.approx(floor_of())

    viewer.viewport = wire(viewer, zoom=1e6)
    assert viewer._vp_current.zoom == pytest.approx(viewer._max_zoom)


def test_wire_negative_zoom_clamps_to_floor(viewer):
    """A negative zoom is not an error, it is the floor — and it must not
    mirror the bbox."""
    viewer.viewport = wire(viewer, zoom=-50.0)
    vp = viewer._vp_current
    assert vp.zoom == pytest.approx(floor_of())
    x0, y0, x1, y1 = vp.l0_bbox
    assert x1 > x0 and y1 > y0


def test_wire_and_python_zoom_clamps_agree(viewer):
    """The wire boundary and ``set_zoom`` apply the same bounds, so a JS
    wheel-zoom limit and a Python ``set_zoom`` cannot disagree."""
    viewer.viewport = wire(viewer, zoom=0.0)
    wire_zoom = viewer._vp_current.zoom
    assert viewer.set_zoom(0.0).zoom == pytest.approx(wire_zoom)


# ------------------------------------------------------------- malformed dicts
@pytest.mark.parametrize(
    "bad",
    [
        {"zoom": float("nan")},
        {"zoom": float("inf")},
        {"cx": "abc"},
        {"cy": None},
        {"canvas_w": 0},
        {"canvas_w": "960"},
        {"cx": float("inf")},
    ],
    ids=["nan_zoom", "inf_zoom", "string_cx", "none_cy", "zero_canvas_w",
         "string_canvas_w", "inf_cx"],
)
def test_malformed_wire_viewport_is_reported_not_raised(viewer, bad):
    """Each of these used to crash planning (or silently plan nonsense);
    now: warn, keep the last good viewport, keep rendering."""
    good = viewer._vp_current
    viewer.render()  # render the good viewport once before it goes bad
    with pytest.warns(UserWarning, match="malformed viewport"):
        viewer.viewport = wire(viewer, **bad)
        wait_idle(viewer)
    assert viewer._vp_current == good  # the last good viewport is kept
    assert viewer.render().tiles  # still renders the kept viewport


def test_malformed_wire_viewport_missing_key(viewer):
    """A dict missing a wire key (``KeyError`` before) is malformed too."""
    good = viewer._vp_current
    d = wire(viewer)
    del d["cy"]
    with pytest.warns(UserWarning, match="malformed viewport"):
        viewer.viewport = d
        wait_idle(viewer)
    assert viewer._vp_current == good


def test_malformed_wire_viewport_reports_in_status(viewer):
    """The boundary also says it in ``status`` (the view's status line).
    Checked with the observer's background render suppressed: a successful
    fallback render reports its own status right after, which is the
    honest thing to show the user."""
    viewer._syncing_viewport = True  # the observer must not start a render
    try:
        viewer.viewport = wire(viewer, zoom=float("nan"))
        with pytest.warns(UserWarning, match="malformed viewport"):
            vp = viewer._vp_current
    finally:
        viewer._syncing_viewport = False
    assert "viewport ignored" in viewer.status
    assert vp.zoom == pytest.approx(fit_zoom(META, 960, 540))


def test_malformed_wire_viewport_warns_once_per_value(viewer):
    """A trait stuck on a bad dict must not warn on every render."""
    bad = wire(viewer, zoom=float("nan"))
    with pytest.warns(UserWarning, match="malformed viewport") as rec:
        viewer.viewport = bad
        wait_idle(viewer)
    assert len(rec) == 1
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # a repeat warning would raise here
        viewer.render()
        viewer.render()


def test_finite_but_extreme_viewport_renders_without_crash(viewer):
    """Far off-slide at max zoom is legal (JS can pan off the slide): it
    plans nothing rather than raising, and the zoom is still clamped."""
    viewer.viewport = wire(viewer, cx=1e12, cy=1e12, zoom=1e9)
    plan = viewer.render()
    assert viewer._vp_current.zoom == pytest.approx(viewer._max_zoom)
    assert not plan.tiles
    assert "0 tiles" in viewer.status


# ------------------------------------------------------------- render failures
def test_render_loop_survives_a_failing_render(viewer, monkeypatch):
    """A render that raises must not escape the thread (traceback in the
    kernel log, stale frame, no clue) and must not kill the loop."""
    def boom():
        raise RuntimeError("planning exploded")

    monkeypatch.setattr(viewer, "_render_once", boom)
    with pytest.warns(UserWarning, match="render failed"):
        viewer._schedule_render()
        wait_idle(viewer)

    assert viewer._rendering_bg is False  # the loop handed off, not died
    assert viewer.status.startswith("render failed")

    monkeypatch.undo()
    assert viewer.render().tiles  # the loop is reusable afterwards


def test_render_loop_renders_a_superseding_viewport_after_a_failure(
    viewer, monkeypatch
):
    """The dirty-flag handoff still works after a failed render."""
    calls = {"n": 0}
    real = SlideViewer._render_once  # the unpatched implementation

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("first render failed")
        return real(viewer)

    monkeypatch.setattr(viewer, "_render_once", flaky)
    with pytest.warns(UserWarning, match="render failed"):
        wait_idle(viewer)  # quiesce the constructor's render first
        viewer._render_dirty = True  # a viewport arrived mid-failure
        viewer._schedule_render()
        wait_idle(viewer)
    assert calls["n"] == 2
    assert "tiles" in viewer.status
