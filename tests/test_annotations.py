"""M2 pure GeoJSON parser tests (no openslide, no widget, no I/O).

Covers: document shapes (FeatureCollection/Feature/bare geometry),
per-kind normalization (point/line/polygon/multi/collection), unit
conversion (px default, um via mpp), property passthrough + type
validation, id assignment, degenerate-shape skipping (warning), and the
malformed-input ValueError contract.
"""
from __future__ import annotations

import math
import warnings

import pytest

from islide.annotations import normalize_ring, parse_annotations


def feat(geom, props=None, fid=None):
    f = {"type": "Feature", "geometry": geom, "properties": props or {}}
    if fid is not None:
        f["id"] = fid
    return f


def fc(*feats):
    return {"type": "FeatureCollection", "features": list(feats)}


def pt(x, y):
    return {"type": "Point", "coordinates": [x, y]}


def line(*xy):
    return {"type": "LineString", "coordinates": [list(p) for p in xy]}


def poly(ring, hole=None):
    rings = [list(ring)]
    if hole is not None:
        rings.append(list(hole))
    return {"type": "Polygon", "coordinates": rings}


# ---------------------------------------------------------------- documents
def test_bare_geometry_document():
    shapes = parse_annotations(pt(10, 20))
    assert shapes == [
        {"id": "a0", "kind": "point", "points": [[10.0, 20.0]],
         "label": None, "color": None, "fill": None},
    ]


def test_feature_collection_mixed_kinds_and_props():
    doc = fc(
        feat(pt(0, 0), {"label": "A"}, fid=1),
        feat(line((0, 0), (10, 0), (10, 5)), {"color": "red"}),
        feat(poly([(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)]),
             {"fill": "rgba(0,0,255,0.5)"}),
    )
    shapes = parse_annotations(doc)
    assert [s["id"] for s in shapes] == ["1", "a1", "a2"]
    assert [s["kind"] for s in shapes] == ["point", "line", "polygon"]
    assert shapes[0]["label"] == "A"
    assert shapes[1]["color"] == "red"
    assert shapes[2]["fill"] == "rgba(0,0,255,0.5)"
    assert shapes[1]["points"] == [[0.0, 0.0], [10.0, 0.0], [10.0, 5.0]]
    assert shapes[2]["points"] == [
        [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0]],
    ]


def test_multi_point_expands_to_point_shapes():
    doc = {"type": "MultiPoint", "coordinates": [[1, 2], [3, 4], [5, 6]]}
    shapes = parse_annotations(doc)
    assert [s["kind"] for s in shapes] == ["point", "point", "point"]
    assert [s["points"] for s in shapes] == [
        [[1.0, 2.0]], [[3.0, 4.0]], [[5.0, 6.0]],
    ]
    assert [s["id"] for s in shapes] == ["a0", "a1", "a2"]


def test_multi_polygon_and_geometry_collection():
    ring1 = [(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)]
    ring2 = [(100, 100), (110, 100), (110, 110), (100, 110), (100, 100)]
    doc = feat(
        {"type": "GeometryCollection", "geometries": [
            {"type": "MultiPolygon", "coordinates": [[ring1], [ring2]]},
            pt(1, 1),
        ]},
        {"label": "g"},
    )
    shapes = parse_annotations(doc)
    assert [s["kind"] for s in shapes] == ["polygon", "polygon", "point"]
    assert all(s["label"] == "g" for s in shapes)
    assert shapes[0]["points"] == [
        [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0]],
    ]


def test_polygon_holes_kept_as_rings():
    ring = [(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)]
    hole = [(2, 2), (4, 2), (4, 4), (2, 4), (2, 2)]
    shapes = parse_annotations(feat(poly(ring, hole)))
    assert shapes[0]["points"] == [
        [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0]],
        [[2.0, 2.0], [4.0, 2.0], [4.0, 4.0], [2.0, 4.0]],
    ]


def test_open_triangle_ring_is_accepted():
    shapes = parse_annotations(feat(poly([(0, 0), (10, 0), (0, 10)])))
    assert shapes[0]["points"] == [[[0.0, 0.0], [10.0, 0.0], [0.0, 10.0]]]


def test_rings_stored_open_closing_position_stripped():
    # Wire model (DESIGN.md §6.3): rings are stored open; a redundant
    # closing position is dropped so the renderer can rely on implicit
    # closure (it closes each ring when tracing).
    closed = [(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)]
    shapes = parse_annotations(feat(poly(closed)))
    assert shapes[0]["points"] == [[[0.0, 0.0], [10.0, 0.0],
                                     [10.0, 10.0], [0.0, 10.0]]]


def test_feature_with_null_geometry_is_a_noop():
    doc = fc(
        {"type": "Feature", "geometry": None, "properties": {}},
        feat(pt(1, 2)),
    )
    shapes = parse_annotations(doc)
    assert len(shapes) == 1
    assert shapes[0]["points"] == [[1.0, 2.0]]


def test_fallback_ids_are_unique_and_stable():
    doc = fc(feat(pt(0, 0)), feat(pt(1, 1)), feat(pt(2, 2)))
    shapes = parse_annotations(doc)
    assert [s["id"] for s in shapes] == ["a0", "a1", "a2"]
    assert len({s["id"] for s in shapes}) == len(shapes)


# ------------------------------------------------------------- unit modes
def test_px_is_default_and_unscaled():
    assert parse_annotations(pt(10, 20))[0]["points"] == [[10.0, 20.0]]
    assert parse_annotations(pt(10, 20), units="px")[0]["points"] == [[10.0, 20.0]]


def test_um_conversion_uses_mpp():
    # px = um / mpp; mpp 0.25 -> 4x
    shapes = parse_annotations(pt(10, 20), units="um", mpp=0.25)
    assert shapes[0]["points"] == [[40.0, 80.0]]


def test_um_requires_positive_finite_mpp():
    for mpp in (None, 0, -1, float("inf"), float("nan"), True):
        with pytest.raises(ValueError, match="mpp"):
            parse_annotations(pt(0, 0), units="um", mpp=mpp)


def test_bad_units_raises():
    with pytest.raises(ValueError, match="units"):
        parse_annotations(pt(0, 0), units="ft")


# ------------------------------------------------------------ validation
def test_malformed_documents_raise():
    for doc in [
        "not a doc",
        None,
        {"type": "Mystery"},
        {"type": "FeatureCollection"},  # missing features
        {"type": "FeatureCollection", "features": "nope"},
        {"type": "FeatureCollection", "features": [pt(0, 0)]},  # not a Feature
        {"type": "Feature", "properties": {}},  # missing geometry
        {"type": "Feature", "geometry": "nope"},
        {"type": "Feature", "geometry": pt(0, 0), "properties": "nope"},
        {"type": "GeometryCollection"},  # missing geometries
    ]:
        with pytest.raises(ValueError):
            parse_annotations(doc)


def test_bad_coordinates_raise():
    for g in [
        {"type": "Point", "coordinates": [0]},  # 1 number
        {"type": "Point", "coordinates": [None, 0]},
        {"type": "Point", "coordinates": [0, "x"]},
        {"type": "Point", "coordinates": [float("nan"), 0]},
        {"type": "Point", "coordinates": [float("inf"), 0]},
        {"type": "Point", "coordinates": [True, 0]},
        {"type": "LineString", "coordinates": [(0, 0)]},  # 1 position
        {"type": "LineString", "coordinates": "nope"},
    ]:
        with pytest.raises(ValueError):
            parse_annotations(g)


def test_bad_rings_raise():
    for doc in [
        {"type": "Polygon", "coordinates": [[(0, 0), (1, 1)]]},  # 2 positions
        {"type": "Polygon", "coordinates": []},  # no rings
        {"type": "Polygon", "coordinates": [1, 2]},  # not a list of rings
    ]:
        with pytest.raises(ValueError):
            parse_annotations(doc)


def test_bad_property_types_raise():
    with pytest.raises(ValueError, match="label"):
        parse_annotations(feat(pt(0, 0), {"label": 5}))
    with pytest.raises(ValueError, match="color"):
        parse_annotations(feat(pt(0, 0), {"color": 1.0}))
    with pytest.raises(ValueError, match="fill"):
        parse_annotations(feat(pt(0, 0), {"fill": [1]}))


def test_bad_feature_id_raises():
    with pytest.raises(ValueError, match="id"):
        parse_annotations(feat(pt(0, 0), None, fid=[1]))


# ----------------------------------------------------------- degenerate
def test_degenerate_shapes_skipped_with_warning():
    doc = fc(
        feat(poly([(0, 0), (1, 1), (2, 2), (0, 0)])),  # collinear: zero area
        feat(line((5, 5), (5, 5))),  # zero length
        feat(pt(9, 9)),
    )
    with pytest.warns(UserWarning) as rec:
        shapes = parse_annotations(doc)
    assert len(rec) == 2
    assert [s["kind"] for s in shapes] == ["point"]
    assert shapes[0]["points"] == [[9.0, 9.0]]


# ------------------------------------------------------- normalize_ring (M3)
def test_normalize_ring_strips_redundant_closing_position():
    assert normalize_ring([[0, 0], [10, 0], [0, 10], [0, 0]]) == [
        [0.0, 0.0], [10.0, 0.0], [0.0, 10.0],
    ]
    assert normalize_ring(((0, 0), (10, 0), (0, 10))) == [
        [0.0, 0.0], [10.0, 0.0], [0.0, 10.0],
    ]
    # open rings are kept as-is; tuples come back as float lists
    assert normalize_ring([[0, 0], [10, 0], [0, 10]]) == [
        [0.0, 0.0], [10.0, 0.0], [0.0, 10.0],
    ]


def test_normalize_ring_rejects_degenerate_and_malformed():
    assert normalize_ring(None) is None
    assert normalize_ring("nope") is None
    assert normalize_ring([1, 2]) is None
    assert normalize_ring([[0, 0]]) is None
    assert normalize_ring([[0, 0], [5, 5]]) is None
    assert normalize_ring([[0, 0], [0, 0], [0, 0]]) is None       # duplicates
    assert normalize_ring([[0, 0], [10, 0], [20, 0]]) is None     # collinear
    assert normalize_ring([[0, 0], [10, 0], [float("nan"), 0]]) is None
    assert normalize_ring([[0, 0], [10, 0], [0, float("inf")]]) is None
    assert normalize_ring([[0, 0], [10], [0, 10]]) is None        # bad arity
    assert normalize_ring([[0, 0], [10, 0, 0], [0, 10]]) is None
    # a 3-position [A, B, A] ring is a genuine closed form: area 0
    assert normalize_ring([[0, 0], [10, 0], [0, 0]]) is None
