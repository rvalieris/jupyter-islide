"""M2 `SlideViewer` annotation API tests — headless (no JS view).

Covers: the synced `annotations` trait (starts empty, JSON-serializable),
set_annotations from dict and from file (replace semantics), clear_annotations,
and the units="um" path (waits for open, converts via the slide's mpp).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from islide import SlideViewer

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

DOC = {
    "type": "FeatureCollection",
    "features": [
        {"type": "Feature", "id": "p1",
         "geometry": {"type": "Point", "coordinates": [100, 200]},
         "properties": {"label": "pt"}},
        {"type": "Feature",
         "geometry": {"type": "LineString", "coordinates": [[0, 0], [50, 50]]},
         "properties": {"color": "red"}},
        {"type": "Feature",
         "geometry": {"type": "Polygon", "coordinates":
                      [[[0, 0], [100, 0], [100, 100], [0, 100], [0, 0]]]},
         "properties": {}},
    ],
}


@pytest.fixture()
def viewer():
    v = SlideViewer(str(SLIDE))
    v.wait()
    yield v
    v.close()


def test_annotations_trait_starts_empty(viewer):
    assert viewer.annotations == []


def test_set_annotations_dict(viewer):
    shapes = viewer.set_annotations(DOC)
    assert len(shapes) == 3
    assert viewer.annotations == shapes
    s0, s1, s2 = viewer.annotations
    assert set(s0) == {"id", "kind", "points", "label", "color", "fill"}
    assert s0["id"] == "p1" and s0["kind"] == "point"
    assert s0["points"] == [[100.0, 200.0]] and s0["label"] == "pt"
    assert s0["color"] is None and s0["fill"] is None
    assert s1["kind"] == "line" and s1["color"] == "red"
    assert s2["kind"] == "polygon" and s2["fill"] is None
    # plain JSON (lists/floats/strs) — comm-serializable
    json.dumps(viewer.annotations)


def test_set_annotations_from_path(viewer, tmp_path):
    p = tmp_path / "roi.geojson"
    p.write_text(json.dumps(DOC))
    assert len(viewer.set_annotations(p)) == 3  # Path object
    assert len(viewer.set_annotations(str(p))) == 3  # str path
    assert [s["kind"] for s in viewer.annotations] == ["point", "line", "polygon"]


def test_set_annotations_replaces_and_clear(viewer):
    viewer.set_annotations(DOC)
    one = {"type": "FeatureCollection", "features": [
        {"type": "Feature",
         "geometry": {"type": "Point", "coordinates": [1, 2]},
         "properties": {}}]}
    viewer.set_annotations(one)
    assert len(viewer.annotations) == 1
    viewer.clear_annotations()
    assert viewer.annotations == []


def test_set_annotations_um_uses_slide_mpp(viewer):
    # testslide mpp is 0.25: (10 um, 20 um) -> (40 px, 80 px)
    assert viewer._meta.mpp == pytest.approx(0.25)
    viewer.set_annotations({"type": "Point", "coordinates": [10, 20]}, units="um")
    assert viewer.annotations[0]["points"] == [[40.0, 80.0]]


def test_set_annotations_um_waits_for_open():
    v = SlideViewer(str(SLIDE))
    try:
        assert not v.slide_open
        v.set_annotations({"type": "Point", "coordinates": [10, 20]}, units="um")
        assert v.slide_open
        assert v.annotations[0]["points"] == [[40.0, 80.0]]
    finally:
        v.close()


def test_set_annotations_px_does_not_wait_for_open():
    # px mode never needs the slide: no wait, no mpp
    v = SlideViewer(str(SLIDE))
    try:
        shapes = v.set_annotations({"type": "Point", "coordinates": [7, 8]})
        assert shapes[0]["points"] == [[7.0, 8.0]]
    finally:
        v.close()
