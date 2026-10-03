"""M3.5 canonical annotation document tests (no openslide, no widget, no I/O).

Covers: the canonical document shape (FeatureCollection of
{id, geometry, properties} features), document inputs (FeatureCollection /
single Feature / bare geometry), structure-preserving normalization
(Point/LineString/Polygon/Multi*, rings open, z dropped), GeometryCollection
expansion (one feature per member, properties stamped, the M2 id-stamping
bug now a ValueError), id assignment (explicit kept + str-coerced, fresh
aN skipping used ids, duplicates a ValueError), properties passthrough
(label/color/fill string-checked, the rest untouched), unit conversion
(px default, um via mpp, before the degenerate checks), degenerate drops
(Polygon, LineString, MultiPolygon islands, feature-level warning), the
malformed-input ValueError contract, and the M4 pure edit commands
(apply_edit: delete / set_label / set_color, unknown id -> None, malformed
commands -> ValueError, input never mutated, idempotent over the document).
"""
from __future__ import annotations

import copy

import warnings

import pytest

from islide.annotations import apply_edit, normalize_ring, parse_annotations

EMPTY = {"type": "FeatureCollection", "features": []}


def feat(geom, props=None, fid=None):
    f = {"type": "Feature", "geometry": geom}
    if props is not None:
        f["properties"] = props
    if fid is not None:
        f["id"] = fid
    return f


def fc(*feats):
    return {"type": "FeatureCollection", "features": list(feats)}


def point(x, y):
    return {"type": "Point", "coordinates": [x, y]}


def features(doc):
    return doc["features"]


# ----------------------------------------------------------- document shape
class TestDocumentShape:
    def test_bare_geometry_is_a_document(self):
        doc = parse_annotations(point(1, 2))
        assert doc == {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "id": "a0",
                    "geometry": {"type": "Point", "coordinates": [1.0, 2.0]},
                    "properties": {},
                }
            ],
        }

    def test_empty_feature_collection(self):
        assert parse_annotations(fc()) == EMPTY

    def test_null_geometry_features_are_legal_no_ops(self):
        doc = fc(
            {"type": "Feature", "geometry": None},
            {"type": "Feature", "geometry": None, "properties": None},
        )
        assert parse_annotations(doc) == EMPTY

    def test_parse_is_pure(self):
        src = {
            "type": "FeatureCollection",
            "features": [
                feat(point(0, 0), props={"label": "x"}, fid="p1"),
            ],
        }
        snapshot = {
            "type": "FeatureCollection",
            "features": [
                {"type": "Feature", "id": "p1",
                 "geometry": {"type": "Point", "coordinates": [0, 0]},
                 "properties": {"label": "x"}},
            ],
        }
        assert parse_annotations(src) == snapshot
        assert src == snapshot  # input not mutated


# ---------------------------------------------------- structure-preserving
class TestStructurePreserving:
    def test_point_drops_z(self):
        doc = parse_annotations({"type": "Point", "coordinates": [1, 2, 3]})
        assert features(doc)[0]["geometry"] == {
            "type": "Point", "coordinates": [1.0, 2.0]
        }

    def test_multi_point_stays_whole(self):
        doc = parse_annotations(
            {"type": "MultiPoint", "coordinates": [[0, 0], [1, 2], [3, 4]]}
        )
        feats = features(doc)
        assert len(feats) == 1
        assert feats[0]["geometry"] == {
            "type": "MultiPoint",
            "coordinates": [[0.0, 0.0], [1.0, 2.0], [3.0, 4.0]],
        }

    def test_line_string_stays_whole(self):
        doc = parse_annotations(
            {"type": "LineString", "coordinates": [[0, 0], [10, 20], [30, 40]]}
        )
        assert features(doc)[0]["geometry"] == {
            "type": "LineString",
            "coordinates": [[0.0, 0.0], [10.0, 20.0], [30.0, 40.0]],
        }

    def test_polygon_stays_whole_rings_open(self):
        doc = parse_annotations(
            {
                "type": "Polygon",
                "coordinates": [
                    [[0, 0], [4, 0], [4, 4], [0, 4], [0, 0]],  # closed outer
                    [[1, 1], [2, 1], [2, 2], [1, 2]],          # closed hole
                ],
            }
        )
        assert features(doc)[0]["geometry"] == {
            "type": "Polygon",
            "coordinates": [
                [[0.0, 0.0], [4.0, 0.0], [4.0, 4.0], [0.0, 4.0]],
                [[1.0, 1.0], [2.0, 1.0], [2.0, 2.0], [1.0, 2.0]],
            ],
        }

    def test_multi_polygon_stays_whole(self):
        doc = parse_annotations(
            {
                "type": "MultiPolygon",
                "coordinates": [
                    [
                        [[0, 0], [2, 0], [2, 2], [0, 2]],
                        [[0.5, 0.5], [1.5, 0.5], [1.5, 1.5]],
                    ],
                    [[[10, 10], [12, 10], [12, 12]]],
                ],
            }
        )
        feats = features(doc)
        assert len(feats) == 1
        geom = feats[0]["geometry"]
        assert geom["type"] == "MultiPolygon"
        assert len(geom["coordinates"]) == 2
        assert geom["coordinates"][0][0] == [
            [0.0, 0.0], [2.0, 0.0], [2.0, 2.0], [0.0, 2.0]
        ]


# ------------------------------------------------------- geometry collection
class TestGeometryCollection:
    def test_expands_one_feature_per_member_properties_stamped(self):
        doc = parse_annotations(
            feat(
                {
                    "type": "GeometryCollection",
                    "geometries": [
                        point(1, 1),
                        {"type": "LineString", "coordinates": [[0, 0], [5, 5]]},
                    ],
                },
                props={"label": "stamped"},
            )
        )
        feats = features(doc)
        assert [f["geometry"]["type"] for f in feats] == ["Point", "LineString"]
        assert [f["properties"] for f in feats] == [
            {"label": "stamped"} for _ in feats
        ]
        assert [f["id"] for f in feats] == ["a0", "a1"]

    def test_nested_collections_expand(self):
        doc = parse_annotations(
            {
                "type": "GeometryCollection",
                "geometries": [
                    {"type": "GeometryCollection", "geometries": [point(1, 1)]},
                    point(2, 2),
                ],
            }
        )
        assert [f["geometry"]["type"] for f in features(doc)] == [
            "Point", "Point"
        ]

    def test_null_members_are_skipped(self):
        doc = parse_annotations(
            {
                "type": "GeometryCollection",
                "geometries": [None, point(1, 1)],
            }
        )
        assert len(features(doc)) == 1


# ------------------------------------------------------------------------ ids
class TestIds:
    def test_fallback_ids_are_aN_in_document_order(self):
        doc = parse_annotations(fc(feat(point(0, 0)), feat(point(1, 1))))
        assert [f["id"] for f in features(doc)] == ["a0", "a1"]

    def test_explicit_ids_kept_and_str_coerced(self):
        doc = parse_annotations(
            fc(feat(point(0, 0), fid="p1"), feat(point(1, 1), fid=7),
               feat(point(2, 2), fid=2.5))
        )
        assert [f["id"] for f in features(doc)] == ["p1", "7", "2.5"]

    def test_fallback_ids_skip_used(self):
        doc = parse_annotations(fc(feat(point(0, 0), fid="a0"), feat(point(1, 1))))
        assert [f["id"] for f in features(doc)] == ["a0", "a1"]

    def test_duplicate_explicit_ids_raise(self):
        with pytest.raises(ValueError, match="duplicate"):
            parse_annotations(
                fc(feat(point(0, 0), fid="x"), feat(point(1, 1), fid="x"))
            )

    def test_coerced_duplicate_ids_raise(self):
        # int 1 and str "1" are the same id after coercion
        with pytest.raises(ValueError, match="duplicate"):
            parse_annotations(fc(feat(point(0, 0), fid=1), feat(point(1, 1), fid="1")))

    def test_geometry_collection_feature_id_cannot_be_shared(self):
        # The latent M2 bug, fixed: an id-bearing GC feature expands into
        # several features, which cannot share one id.
        doc = feat(
            {"type": "GeometryCollection", "geometries": [point(0, 0), point(1, 1)]},
            fid="x",
        )
        with pytest.raises(ValueError, match="duplicate"):
            parse_annotations(doc)

    def test_geometry_collection_feature_id_single_member_kept(self):
        doc = parse_annotations(
            feat(
                {"type": "GeometryCollection", "geometries": [point(0, 0)]},
                fid="x",
            )
        )
        assert [f["id"] for f in features(doc)] == ["x"]

    def test_id_never_renumbered_on_reimport(self):
        src = fc(
            feat(point(0, 0), fid="keep"),
            feat(point(1, 1)),
        )
        once = parse_annotations(src)
        assert parse_annotations(once) == once
        assert [f["id"] for f in features(once)] == ["keep", "a0"]


# --------------------------------------------------------------- properties
class TestProperties:
    def test_unknown_keys_pass_through_whole(self):
        doc = parse_annotations(
            feat(point(0, 0), props={"m": 5, "tags": [1, 2], "note": None})
        )
        # unknown keys are kept; the None-valued one is dropped
        assert features(doc)[0]["properties"] == {"m": 5, "tags": [1, 2]}

    def test_absent_properties_become_empty_object(self):
        doc = parse_annotations(point(0, 0))
        assert features(doc)[0]["properties"] == {}

    def test_none_properties_become_empty_object(self):
        doc = parse_annotations(
            {"type": "Feature", "geometry": point(0, 0), "properties": None}
        )
        assert features(doc)[0]["properties"] == {}

    @pytest.mark.parametrize("key", ["label", "color", "fill"])
    def test_non_string_style_properties_raise(self, key):
        with pytest.raises(ValueError, match="must be a string"):
            parse_annotations(feat(point(0, 0), props={key: 5}))

    def test_none_style_properties_are_dropped(self):
        doc = parse_annotations(
            feat(point(0, 0), props={"label": None, "color": None, "fill": None})
        )
        # legal input; the canonical form has no None values — the keys
        # are absent
        assert features(doc)[0]["properties"] == {}


# ----------------------------------------------------------- coordinate values
class TestCoordinates:
    def test_px_coordinates_pass_through(self):
        # level-0 slide px, as given (coerced to float)
        doc = parse_annotations(point(2, 3))
        assert features(doc)[0]["geometry"]["coordinates"] == [2.0, 3.0]


# ----------------------------------------------------------- degenerate drops
class TestDegenerateDrops:
    def test_degenerate_polygon_dropped_with_warning(self):
        doc = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [2, 0]]]}
        with pytest.warns(UserWarning, match="Polygon"):
            assert parse_annotations(doc) == EMPTY

    def test_polygon_with_degenerate_hole_drops_whole(self):
        doc = {
            "type": "Polygon",
            "coordinates": [
                [[0, 0], [4, 0], [4, 4], [0, 4]],
                [[1, 1], [2, 1], [3, 1]],  # collinear hole
            ],
        }
        with pytest.warns(UserWarning, match="Polygon"):
            assert parse_annotations(doc) == EMPTY

    def test_short_line_dropped_with_warning(self):
        doc = {"type": "LineString", "coordinates": [[0, 0], [0, 0]]}
        with pytest.warns(UserWarning, match="LineString"):
            assert parse_annotations(doc) == EMPTY

    def test_multi_polygon_loses_degenerate_islands(self):
        doc = {
            "type": "MultiPolygon",
            "coordinates": [
                [[[0, 0], [1, 0], [2, 0]]],  # collinear island
                [[[10, 10], [12, 10], [12, 12]]],  # fine island
            ],
        }
        with pytest.warns(UserWarning, match="MultiPolygon"):
            parsed = parse_annotations(doc)
        feats = features(parsed)
        assert len(feats) == 1
        assert feats[0]["geometry"]["coordinates"][0][0] == [
            [10.0, 10.0], [12.0, 10.0], [12.0, 12.0]
        ]

    def test_multi_polygon_all_islands_dropped_drops_feature(self):
        doc = {
            "type": "MultiPolygon",
            "coordinates": [
                [[[0, 0], [1, 0], [2, 0]]],
                [[[0, 0], [0, 1], [1, 1]], [[0.2, 0.2], [0.3, 0.2], [0.4, 0.2]]],
            ],
        }
        with pytest.warns(UserWarning) as rec:
            assert parse_annotations(doc) == EMPTY
        # two island warnings + the feature-level drop
        assert len(rec) == 3

    def test_geometry_collection_all_members_dropped_drops_feature(self):
        doc = {
            "type": "GeometryCollection",
            "geometries": [
                {"type": "LineString", "coordinates": [[0, 0], [0, 0]]},
                {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [2, 0]]]},
            ],
        }
        with pytest.warns(UserWarning) as rec:
            assert parse_annotations(doc) == EMPTY
        # one warning per degenerate member + the feature-level drop
        assert len(rec) == 3

    def test_geometry_collection_member_drops_others_survive(self):
        doc = {
            "type": "GeometryCollection",
            "geometries": [
                {"type": "LineString", "coordinates": [[0, 0], [0, 0]]},
                point(5, 6),
            ],
        }
        with pytest.warns(UserWarning, match="LineString"):
            parsed = parse_annotations(doc)
        feats = features(parsed)
        assert [f["geometry"]["type"] for f in feats] == ["Point"]
        assert [f["id"] for f in feats] == ["a0"]


# ------------------------------------------------------------- idempotency
class TestIdempotent:
    def test_parse_of_canonical_document_is_unchanged(self):
        src = fc(
            feat(point(1, 2), props={"label": "a"}, fid="p1"),
            feat({"type": "MultiPoint", "coordinates": [[3, 4], [5, 6]]}, fid="mp"),
            feat(
                {"type": "LineString", "coordinates": [[0, 0], [9, 8]]},
                fid="l",
            ),
            feat(
                {
                    "type": "Polygon",
                    "coordinates": [
                        [[0, 0], [4, 0], [4, 4], [0, 4]],
                        [[1, 1], [2, 1], [2, 2], [1, 2]],
                    ],
                },
                fid="g",
            ),
            feat(
                {
                    "type": "MultiPolygon",
                    "coordinates": [
                        [
                            [[10, 0], [12, 0], [12, 2]],
                            [[10.5, 0.5], [11.5, 0.5], [11.5, 1.5]],
                        ]
                    ],
                },
                fid="mg",
            ),
        )
        once = parse_annotations(src)
        assert parse_annotations(once) == once


# ------------------------------------------------------------------- malformed
class TestMalformed:
    @pytest.mark.parametrize(
        "bad",
        [
            None,
            5,
            "x",
            [1, 2],
            {"type": "Bogus", "coordinates": [[0, 0]]},
            {"type": "FeatureCollection"},  # missing features
            {"type": "FeatureCollection", "features": "nope"},
            {"type": "FeatureCollection", "features": [point(0, 0)]},  # not a Feature
            {"type": "Feature", "geometry": [1, 2]},  # geometry not an object
            {"type": "Feature"},  # missing geometry member
            {"type": "Feature", "geometry": {"type": "Bogus"}},
            {"type": "Feature", "geometry": {"type": "Point"}},  # missing coords
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": [0]}},
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": [True, 0]}},
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": [float("inf"), 0]}},
            {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[0, 0]]}},
            {"type": "Feature", "geometry": {"type": "LineString"}},
            {"type": "Feature", "geometry": {"type": "MultiPoint", "coordinates": "nope"}},
            {"type": "Feature", "geometry": {"type": "MultiPoint", "coordinates": [[0]]}},
            {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": []}},
            {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 1]]]}},
            {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 1], [2, 2], [3, "x"]]]}},
            {"type": "Feature", "geometry": {"type": "MultiPolygon", "coordinates": "nope"}},
            {"type": "Feature", "geometry": {"type": "GeometryCollection"}},
            {"type": "Feature", "geometry": {"type": "GeometryCollection", "geometries": "nope"}},
        ],
    )
    def test_bad_documents_raise(self, bad):
        with pytest.raises(ValueError):
            parse_annotations(bad)

    @pytest.mark.parametrize("fid", [[1, 2], True])
    def test_bad_feature_ids_raise(self, fid):
        with pytest.raises(ValueError, match="id"):
            parse_annotations(feat(point(0, 0), fid=fid))

    def test_non_dict_properties_raise(self):
        with pytest.raises(ValueError, match="properties"):
            parse_annotations(
                {"type": "Feature", "geometry": point(0, 0), "properties": [1]}
            )


# ------------------------------------------------------- bare geometry inputs
class TestBareGeometryInputs:
    @pytest.mark.parametrize(
        "geom",
        [
            point(0, 1),
            {"type": "MultiPoint", "coordinates": [[0, 0], [1, 1]]},
            {"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
            {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1]]]},
            {"type": "MultiPolygon", "coordinates": [[[[0, 0], [1, 0], [1, 1]]]]},
            {"type": "GeometryCollection", "geometries": [point(0, 0)]},
        ],
    )
    def test_each_bare_geometry_is_a_document(self, geom):
        doc = parse_annotations(geom)
        assert doc["type"] == "FeatureCollection"
        assert len(features(doc)) >= 1
        for f in features(doc):
            assert f["type"] == "Feature"
            assert isinstance(f["id"], str)
            assert f["properties"] == {}


# ------------------------------------------------------------- normalize_ring
class TestNormalizeRing:
    def test_closing_position_dropped(self):
        assert normalize_ring([[0, 0], [4, 0], [4, 4], [0, 0]]) == [
            [0.0, 0.0], [4.0, 0.0], [4.0, 4.0]
        ]

    def test_open_ring_kept(self):
        assert normalize_ring([[0, 0], [4, 0], [4, 4]]) == [
            [0.0, 0.0], [4.0, 0.0], [4.0, 4.0]
        ]

    def test_non_finite_returns_none(self):
        assert normalize_ring([[0, 0], [float("inf"), 0], [0, 1]]) is None

    def test_collinear_returns_none(self):
        assert normalize_ring([[0, 0], [1, 0], [2, 0]]) is None

    def test_two_point_returns_none(self):
        assert normalize_ring([[0, 0], [1, 0]]) is None

    def test_malformed_returns_none(self):
        assert normalize_ring("nope") is None
        assert normalize_ring(None) is None
        assert normalize_ring([[0, 0], [1]]) is None


# ------------------------------------------------------------- M4: apply_edit

def _m4_doc():
    return fc(
        feat(point(1, 1), fid="a1"),
        feat(point(2, 2), {"label": "x", "fill": "rgba(0,0,255,0.5)"}, fid="a2"),
        feat({"type": "LineString", "coordinates": [[0, 0], [3, 3]]}, fid="a3"),
    )


class TestApplyEditOps:
    def test_delete_removes_only_the_addressed_feature(self):
        out = apply_edit(_m4_doc(), {"op": "delete", "id": "a2"})
        assert [f["id"] for f in features(out)] == ["a1", "a3"]

    def test_delete_middle_keeps_order_and_the_rest(self):
        out = apply_edit(_m4_doc(), {"op": "delete", "id": "a2"})
        assert out["type"] == "FeatureCollection"
        assert out["features"][0]["geometry"] == point(1, 1)
        assert out["features"][1]["geometry"]["type"] == "LineString"

    def test_set_label_stores_and_clears(self):
        out = apply_edit(_m4_doc(), {"op": "set_label", "id": "a1", "label": "tumor"})
        assert out["features"][0]["properties"]["label"] == "tumor"
        for label in ("", "   ", None):  # clear: the key is dropped
            out = apply_edit(
                _m4_doc(), {"op": "set_label", "id": "a2", "label": label}
            )
            assert "label" not in out["features"][1]["properties"]

    def test_set_color_stores_pair_and_resets_to_the_default(self):
        out = apply_edit(
            _m4_doc(),
            {"op": "set_color", "id": "a1", "color": "red", "fill": "rgba(0,255,0,0.2)"},
        )
        assert out["features"][0]["properties"] == {
            "color": "red", "fill": "rgba(0,255,0,0.2)"
        }
        out = apply_edit(_m4_doc(), {"op": "set_color", "id": "a2", "color": None, "fill": None})
        assert out["features"][1]["properties"] == {"label": "x"}

    def test_set_ops_preserve_other_property_keys(self):
        out = apply_edit(_m4_doc(), {"op": "set_label", "id": "a2", "label": "y"})
        assert out["features"][1]["properties"] == {
            "label": "y", "fill": "rgba(0,0,255,0.5)",
        }


class TestApplyEditUnknownId:
    def test_unknown_id_is_none_and_input_untouched(self):
        doc = _m4_doc()
        snapshot = copy.deepcopy(doc)
        assert apply_edit(doc, {"op": "delete", "id": "nope"}) is None
        assert doc == snapshot

    def test_unknown_id_on_empty_doc(self):
        assert apply_edit(EMPTY, {"op": "set_label", "id": "a0", "label": "x"}) is None


class TestApplyEditPurityAndIdempotence:
    def test_input_is_never_mutated(self):
        for cmd in (
            {"op": "delete", "id": "a1"},
            {"op": "set_label", "id": "a1", "label": "z"},
            {"op": "set_color", "id": "a3", "color": "blue", "fill": None},
        ):
            doc = _m4_doc()
            snapshot = copy.deepcopy(doc)
            out = apply_edit(doc, cmd)
            assert out is not doc
            assert doc == snapshot

    def test_idempotent_over_the_document(self):
        doc = _m4_doc()
        once = apply_edit(doc, {"op": "set_label", "id": "a1", "label": "t"})
        twice = apply_edit(once, {"op": "set_label", "id": "a1", "label": "t"})
        assert apply_edit(twice, {"op": "delete", "id": "a1"}) is not None
        # a replayed delete finds no feature (None); the set ops are
        # value-idempotent
        assert once["features"][0]["properties"]["label"] == "t"
        assert twice["features"][0]["properties"]["label"] == "t"


class TestApplyEditMalformed:
    @pytest.mark.parametrize(
        "cmd",
        [
            None,
            "delete",
            ["delete"],
            {},
            {"op": "nope", "id": "a1"},
            {"op": "delete"},  # missing id
            {"op": "delete", "id": ""},
            {"op": "delete", "id": 123},
            {"op": "delete", "id": None},
            {"op": "set_label", "id": "a1"},  # missing label
            {"op": "set_label", "id": "a1", "label": 5},
            {"op": "set_color", "id": "a1", "color": "red"},  # missing fill
            {"op": "set_color", "id": "a1", "color": 5, "fill": None},
            {"op": "set_color", "id": "a1", "fill": "red"},  # missing color
        ],
    )
    def test_malformed_command_is_a_value_error(self, cmd):
        doc = _m4_doc()
        with pytest.raises(ValueError):
            apply_edit(doc, cmd)

    def test_malformed_doc_is_a_value_error(self):
        with pytest.raises(ValueError):
            apply_edit({"features": "nope"}, {"op": "delete", "id": "a1"})
        with pytest.raises(ValueError):
            apply_edit(None, {"op": "delete", "id": "a1"})


