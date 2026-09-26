"""M2 `SlideViewer` annotation API tests — headless (no JS view).

Covers: the synced `annotations` trait (starts as an empty canonical
document, JSON-serializable, assignments coerced through the normalizer),
set_annotations from dict and from file (replace semantics, returns the
canonical document), clear_annotations, and the units="um" path (waits for
open, converts via the slide's mpp).
"""
from __future__ import annotations

import json
import os

import pytest
from traitlets import TraitError

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

EMPTY = {"type": "FeatureCollection", "features": []}

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
         "geometry": {"type": "Polygon",
                      "coordinates": [[[0, 0], [100, 0], [100, 100], [0, 100], [0, 0]]]},
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
    assert viewer.annotations == EMPTY


def test_set_annotations_dict(viewer):
    doc = viewer.set_annotations(DOC)
    assert viewer.annotations == doc
    assert doc == {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "id": "p1",
             "geometry": {"type": "Point", "coordinates": [100.0, 200.0]},
             "properties": {"label": "pt"}},
            {"type": "Feature", "id": "a0",
             "geometry": {"type": "LineString",
                          "coordinates": [[0.0, 0.0], [50.0, 50.0]]},
             "properties": {"color": "red"}},
            {"type": "Feature", "id": "a1",
             "geometry": {"type": "Polygon",
                          "coordinates": [[[0.0, 0.0], [100.0, 0.0],
                                           [100.0, 100.0], [0.0, 100.0]]]},
             "properties": {}},
        ],
    }
    # plain JSON — comm-serializable
    json.dumps(viewer.annotations)


def test_set_annotations_from_path(viewer, tmp_path):
    p = tmp_path / "roi.geojson"
    p.write_text(json.dumps(DOC))
    assert len(viewer.set_annotations(p)["features"]) == 3  # Path object
    assert len(viewer.set_annotations(str(p))["features"]) == 3  # str path
    assert [f["geometry"]["type"] for f in viewer.annotations["features"]] == [
        "Point", "LineString", "Polygon"
    ]


def test_set_annotations_replaces_and_clear(viewer):
    viewer.set_annotations(DOC)
    one = {"type": "FeatureCollection", "features": [
        {"type": "Feature",
         "geometry": {"type": "Point", "coordinates": [1, 2]}},
    ]}
    viewer.set_annotations(one)
    assert len(viewer.annotations["features"]) == 1
    viewer.clear_annotations()
    assert viewer.annotations == EMPTY


def test_trait_assignment_is_coerced_through_the_normalizer(viewer):
    viewer.annotations = {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature",
             "geometry": {"type": "Point", "coordinates": [1, 2]}},
        ],
    }
    f = viewer.annotations["features"][0]
    assert f["id"] == "a0"
    assert f["properties"] == {}
    # malformed documents raise a TraitError on trait assignment
    with pytest.raises(TraitError):
        viewer.annotations = {"type": "Bogus"}
    with pytest.raises(TraitError):
        viewer.annotations = [1, 2]  # type: ignore[assignment]
    assert viewer.annotations["features"][0]["id"] == "a0"


def test_set_annotations_um_uses_slide_mpp(viewer):
    # testslide mpp is 0.25: (10 um, 20 um) -> (40 px, 80 px)
    assert viewer._meta.mpp == pytest.approx(0.25)
    doc = viewer.set_annotations({"type": "Point", "coordinates": [10, 20]}, units="um")
    assert viewer.annotations == doc
    assert doc["features"][0]["geometry"]["coordinates"] == [40.0, 80.0]


def test_set_annotations_um_waits_for_open():
    v = SlideViewer(str(SLIDE))
    try:
        assert not v.slide_open
        v.set_annotations({"type": "Point", "coordinates": [10, 20]}, units="um")
        assert v.slide_open
        f = v.annotations["features"][0]
        assert f["geometry"]["coordinates"] == [40.0, 80.0]
    finally:
        v.close()


def test_set_annotations_px_does_not_wait_for_open():
    # px mode never needs the slide: no wait, no mpp
    v = SlideViewer(str(SLIDE))
    try:
        doc = v.set_annotations({"type": "Point", "coordinates": [7, 8]})
        assert doc["features"][0]["geometry"]["coordinates"] == [7.0, 8.0]
    finally:
        v.close()
