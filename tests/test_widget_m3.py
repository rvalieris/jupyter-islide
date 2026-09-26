"""M3 `SlideViewer` tests — headless (no JS view).

Covers: the JS->Py `last_polygon` wire (the Python observer normalizes the
draft ring with the shared helper, appends a polygon shape with a fresh
non-colliding id, discards degenerate drafts with a warning and no state
change), ids after `clear_annotations()`, and the `jpeg_quality`
constructor argument (default 85, validation, payload-only effect).
"""
from __future__ import annotations

import json
import os
import warnings

import pytest
from traitlets import TraitError

from islide import SlideViewer, normalize_ring

SLIDE = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "data", "testslide.tiff")
)

try:
    import openslide  # noqa: F401
    HAS_OPENSLLIDE = True
except ImportError:
    HAS_OPENSLLIDE = False

pytestmark = [
    pytest.mark.skipif(not HAS_OPENSLLIDE, reason="openslide-python not installed"),
    pytest.mark.skipif(not os.path.exists(SLIDE), reason="data/testslide.tiff missing"),
]

TRIANGLE = [[0.0, 0.0], [100.0, 0.0], [0.0, 100.0]]


def _as_json(value):
    """Round-trip through JSON, like the comm channel would."""
    return json.loads(json.dumps(value))


@pytest.fixture()
def viewer():
    v = SlideViewer(str(SLIDE))
    v.wait()
    yield v
    v.close()


# ---------------------------------------------------------- last_polygon
def test_last_polygon_appends_normalized_shape(viewer):
    assert viewer.annotations == []
    # A ring with a redundant closing position (what a JS draft would
    # never send, but the observer must handle): stripped, floats.
    viewer.last_polygon = [[0, 0], [100, 0], [0, 100], [0, 0]]
    assert viewer.annotations == [
        {"id": "a0", "kind": "polygon", "points": [TRIANGLE],
         "label": None, "color": None, "fill": None}
    ]
    assert viewer.status == "Added polygon #a0"
    # synced state survives the JSON round-trip
    assert _as_json(viewer.annotations) == [
        {"id": "a0", "kind": "polygon", "points": [TRIANGLE],
         "label": None, "color": None, "fill": None}
    ]


def test_last_polygon_keeps_open_ring_unclamped(viewer):
    ring = [[-50, -60], [4000, 0], [0, 9000]]  # partly off-slide
    viewer.last_polygon = ring
    shape = viewer.annotations[0]
    assert shape["kind"] == "polygon"
    assert shape["points"] == [[list(map(float, p)) for p in ring]]
    assert shape["label"] is None and shape["color"] is None and shape["fill"] is None
    ring_pts = shape["points"][0]
    for pos in ring_pts:
        assert isinstance(pos, list) and len(pos) == 2
        assert all(isinstance(c, float) for c in pos)


def test_last_polygon_degenerate_drafts_warn_and_leave_state_unchanged(viewer):
    bad_drafts = [
        [[0, 0]],                      # too short (also the "no points yet" case)
        [[0, 0], [10, 10]],
        [[0, 0], [0, 0], [0, 0]],     # duplicate points: zero area
        [[0, 0], [10, 0], [20, 0]],   # collinear: zero area
        [[0, 0], [10, 0], [float("nan"), 0]],  # non-finite coordinate
        [[0, 0], [10], [0, 10]],      # malformed position
    ]
    for ring in bad_drafts:
        with pytest.warns(UserWarning):
            viewer.last_polygon = ring
        assert viewer.status == "Discarded: polygon needs ≥ 3 non-collinear points"
    assert viewer.annotations == []


def test_last_polygon_null_is_a_noop(viewer):
    with warnings.catch_warnings(record=True) as rec:
        warnings.simplefilter("always")
        viewer.last_polygon = None
    assert not rec
    assert viewer.annotations == []


def test_last_polygon_malformed_list_warns_and_leaves_state_unchanged(viewer):
    # traitlets coerces the string into a list; the observer sees a
    # malformed list -> warning, no state change (same as a bad JS payload)
    with pytest.warns(UserWarning):
        viewer.last_polygon = "nope"  # type: ignore[assignment]
    assert viewer.annotations == []


def test_last_polygon_non_iterable_assignment_is_a_trait_error(viewer):
    with pytest.raises(TraitError):
        viewer.last_polygon = 5  # type: ignore[assignment]
    assert viewer.annotations == []


def test_successive_saves_get_fresh_ids(viewer):
    for i, w in enumerate((5, 6, 7)):
        viewer.last_polygon = [[0, 0], [w, 0], [0, w]]
    assert [s["id"] for s in viewer.annotations] == ["a0", "a1", "a2"]
    assert [s["points"] for s in viewer.annotations] == [
        [[[0.0, 0.0], [float(w), 0.0], [0.0, float(w)]]] for w in (5, 6, 7)
    ]


def test_last_polygon_appends_after_clear_annotations(viewer):
    viewer.last_polygon = [[0, 0], [5, 0], [0, 5]]
    viewer.clear_annotations()
    viewer.last_polygon = [[0, 0], [6, 0], [0, 6]]
    assert len(viewer.annotations) == 1
    assert viewer.annotations[0]["id"] == "a0"  # fresh scan after the clear


def test_last_polygon_appends_after_set_annotations(viewer):
    shapes = viewer.set_annotations({
        "type": "FeatureCollection",
        "features": [
            {"id": "a1", "type": "Feature",
             "geometry": {"type": "Point", "coordinates": [1, 1]},
             "properties": {}},
            {"id": "a3", "type": "Feature",
             "geometry": {"type": "Point", "coordinates": [2, 2]},
             "properties": {}},
        ],
    })
    assert [s["id"] for s in shapes] == ["a1", "a3"]
    viewer.last_polygon = [[0, 0], [5, 0], [0, 5]]
    ids = [s["id"] for s in viewer.annotations]
    assert ids[:2] == ["a1", "a3"]
    assert ids[2] not in {"a1", "a3"}  # the fresh scan skipped the used ids


def test_normalize_ring_rejects_non_list_like_junk(viewer):
    # the shared helper: None and malformed content -> None
    assert normalize_ring(None) is None
    assert normalize_ring("nope") is None
    assert normalize_ring([[0, 0], [10, 0]]) is None


# ---------------------------------------------------------- jpeg_quality
def test_jpeg_quality_defaults_to_85(viewer):
    assert viewer._jpeg_quality == 85


def test_jpeg_quality_validation(viewer):
    for bad in (0, 96, -1, 100, 1000, None, "high"):
        with pytest.raises(ValueError, match="jpeg_quality"):
            SlideViewer(str(SLIDE), jpeg_quality=bad)


def test_jpeg_quality_changes_tile_payload_only(viewer):
    low = SlideViewer(str(SLIDE), jpeg_quality=40)
    try:
        low.wait()
        plan_def = viewer._render_once()
        plan_low = low._render_once()
    finally:
        low.close()
    # same tiles planned; only the payload quality/size moves
    assert list(viewer.tiles) == list(low.tiles)
    assert viewer.tile_geo == low.tile_geo
    size_low = sum(len(u) for u in low.tiles.values())
    size_def = sum(len(u) for u in viewer.tiles.values())
    assert size_low < size_def
