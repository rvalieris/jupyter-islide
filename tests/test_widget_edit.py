"""`SlideViewer` edit-command tests — headless (no JS view).

Covers: the Py<->JS `annotation_edit` wire — the JS->Py last-event slot
holding the last issued edit command ({op: "delete" | "set_label" |
"set_color" | "set_vertex" | "add_vertex", id, ...}) that the Python
observer applies to the canonical annotation document (docs/DESIGN.md §6.5): known
ids applied (status "deleted #<id>" / "edited #<id> (label|color|vertex)"),
unknown or stale ids a no-op with the "edit ignored: unknown annotation id"
status and no warning, malformed or refused commands (a set_vertex with a bad
index or a move that would degenerate the geometry; an add_vertex with a bad
segment index, on top of an existing vertex, or degenerating the geometry)
a warning with no state change and an "edit ignored: ..." status, and the
last-event-slot semantics: the trait keeps the last issued command
(re-attach replays it, idempotent over the pushed set, like `last_polygon`).
Plus the public API (delete_annotation / set_annotation_label /
set_annotation_color / set_annotation_vertex / add_annotation_vertex)
issuing commands through the trait.
"""
from __future__ import annotations

import json
import warnings

import pytest
from traitlets import TraitError

from islide import SlideViewer

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

EMPTY = {"type": "FeatureCollection", "features": []}


def _as_json(value):
    """Round-trip through JSON, like the comm channel would."""
    return json.loads(json.dumps(value))


def _seed(viewer):
    """Two points with explicit ids (a1, a2) and properties to preserve."""
    viewer.set_annotations({
        "type": "FeatureCollection",
        "features": [
            {"id": "a1", "type": "Feature",
             "geometry": {"type": "Point", "coordinates": [1, 1]},
             "properties": {"label": "x"}},
            {"id": "a2", "type": "Feature",
             "geometry": {"type": "Point", "coordinates": [2, 2]},
             "properties": {"fill": "rgba(0,0,255,0.5)"}},
        ],
    })


@pytest.fixture()
def viewer(slide_path):
    v = SlideViewer(slide_path)
    v.wait()
    yield v
    v.close()


# ------------------------------------------------------------- the trait
def test_annotation_edit_trait_defaults_to_none(viewer):
    assert viewer.annotation_edit is None


def test_annotation_edit_rejects_non_dict_payloads(viewer):
    for bad in ("nope", 5, ["delete"]):
        with pytest.raises(TraitError):
            viewer.annotation_edit = bad  # type: ignore[assignment]
    assert viewer.annotation_edit is None


def test_annotation_edit_none_is_a_noop(viewer):
    _seed(viewer)
    before = viewer.annotations
    with warnings.catch_warnings(record=True) as rec:
        warnings.simplefilter("always")
        viewer.annotation_edit = None
    assert not rec
    assert viewer.annotations == before


# ------------------------------------------------------------- the observer
def test_annotation_edit_delete_applies(viewer):
    _seed(viewer)
    cmd = {"op": "delete", "id": "a1"}
    viewer.annotation_edit = cmd
    doc = viewer.annotations
    assert [f["id"] for f in doc["features"]] == ["a2"]
    assert viewer.status == "deleted #a1"
    # last-event slot: the trait keeps the command (re-attach replays it)
    assert viewer.annotation_edit == cmd
    # the updated document survives the JSON round-trip like any trait
    assert _as_json(doc) == doc


def test_annotation_edit_set_label_applies(viewer):
    _seed(viewer)
    cmd = {"op": "set_label", "id": "a1", "label": "tumor"}
    viewer.annotation_edit = _as_json(cmd)  # as the comm channel would deliver
    assert viewer.annotations["features"][0]["properties"]["label"] == "tumor"
    # the untouched feature kept its properties
    assert viewer.annotations["features"][1]["properties"] == {
        "fill": "rgba(0,0,255,0.5)"}
    assert viewer.status == "edited #a1 (label)"


def test_annotation_edit_set_color_applies(viewer):
    _seed(viewer)
    viewer.annotation_edit = {
        "op": "set_color", "id": "a2",
        "color": "red", "fill": "rgba(255,0,0,0.2)",
    }
    props = viewer.annotations["features"][1]["properties"]
    assert props == {"color": "red", "fill": "rgba(255,0,0,0.2)"}
    assert viewer.status == "edited #a2 (color)"


def test_annotation_edit_unknown_id_is_a_silent_noop(viewer):
    _seed(viewer)
    before = viewer.annotations
    with warnings.catch_warnings(record=True) as rec:
        warnings.simplefilter("always")
        viewer.annotation_edit = {"op": "delete", "id": "a9"}
    assert not rec  # a stale id is not an error
    assert viewer.annotations == before
    assert viewer.status == "edit ignored: unknown annotation id"


def test_annotation_edit_stale_id_after_clear_is_a_silent_noop(viewer):
    _seed(viewer)
    viewer.clear_annotations()
    with warnings.catch_warnings(record=True) as rec:
        warnings.simplefilter("always")
        viewer.annotation_edit = {"op": "delete", "id": "a1"}
    assert not rec
    assert viewer.annotations == EMPTY
    assert viewer.status == "edit ignored: unknown annotation id"


def test_annotation_edit_malformed_command_warns_and_leaves_state_unchanged(viewer):
    _seed(viewer)
    before = viewer.annotations
    for cmd in ({}, {"op": "nope", "id": "a1"}, {"op": "delete"},
                {"op": "set_label", "id": "a1"},
                {"op": "set_color", "id": "a1", "color": "red"},
                {"op": "set_vertex", "id": "a1"},
                {"op": "set_vertex", "id": "a1", "index": 0, "y": 0},
                {"op": "set_vertex", "id": "a1", "index": 0.0, "x": 0, "y": 0},
                {"op": "set_vertex", "id": "a1", "index": 5, "x": 0, "y": 0}):
        with pytest.warns(UserWarning):
            viewer.annotation_edit = cmd
        assert viewer.annotations == before
        assert viewer.status.startswith("edit ignored: ")


# --------------------------------------------- set_vertex (vertex editing)
def _seed_polygon(viewer):
    """One triangle (id a1): rings open, three positions, index 1 = [4, 0]."""
    viewer.set_annotations({
        "type": "FeatureCollection",
        "features": [
            {"id": "a1", "type": "Feature",
             "geometry": {"type": "Polygon",
                          "coordinates": [[[0, 0], [4, 0], [4, 4]]]},
             "properties": {}},
        ],
    })


def test_annotation_edit_set_vertex_applies(viewer):
    _seed_polygon(viewer)
    cmd = {"op": "set_vertex", "id": "a1", "index": 1, "x": 5, "y": 1}
    viewer.annotation_edit = _as_json(cmd)  # as the comm channel would deliver
    assert viewer.annotation_edit == cmd
    assert viewer.annotations["features"][0]["geometry"]["coordinates"] == [
        [[0.0, 0.0], [5.0, 1.0], [4.0, 4.0]]
    ]
    assert viewer.status == "edited #a1 (vertex)"
    # last-event slot: a replay is idempotent over the pushed set
    before = viewer.annotations
    viewer.annotation_edit = cmd
    assert viewer.annotations == before


def test_annotation_edit_set_vertex_unknown_id_is_a_silent_noop(viewer):
    _seed_polygon(viewer)
    before = viewer.annotations
    with warnings.catch_warnings(record=True) as rec:
        warnings.simplefilter("always")
        viewer.annotation_edit = {"op": "set_vertex", "id": "a9",
                                  "index": 0, "x": 1, "y": 1}
    assert not rec
    assert viewer.annotations == before
    assert viewer.status == "edit ignored: unknown annotation id"


def test_annotation_edit_set_vertex_degenerate_move_is_refused(viewer):
    _seed_polygon(viewer)
    before = viewer.annotations
    with pytest.warns(UserWarning):
        # the third vertex onto the first: a zero-area ring
        viewer.annotation_edit = {"op": "set_vertex", "id": "a1",
                                  "index": 2, "x": 0, "y": 0}
    assert viewer.annotations == before
    assert viewer.status.startswith("edit ignored: set_vertex")


def test_public_api_set_annotation_vertex(viewer):
    _seed_polygon(viewer)
    viewer.set_annotation_vertex("a1", 0, 1.5, 2.5)
    assert viewer.annotation_edit == {
        "op": "set_vertex", "id": "a1", "index": 0, "x": 1.5, "y": 2.5}
    assert viewer.annotations["features"][0]["geometry"]["coordinates"] == [
        [[1.5, 2.5], [4.0, 0.0], [4.0, 4.0]]
    ]
    assert viewer.status == "edited #a1 (vertex)"

    before = viewer.annotations
    with pytest.warns(UserWarning):
        # vertex 1 onto vertex 0's current position: a zero-area ring
        viewer.set_annotation_vertex("a1", 1, 1.5, 2.5)
    assert viewer.annotations == before
    assert viewer.status.startswith("edit ignored: set_vertex")


# ----------------------------------------------- add_vertex (vertex insert)
def test_annotation_edit_add_vertex_applies(viewer):
    _seed_polygon(viewer)
    cmd = {"op": "add_vertex", "id": "a1", "index": 0, "x": 2, "y": -1}
    viewer.annotation_edit = _as_json(cmd)  # as the comm channel would deliver
    assert viewer.annotation_edit == cmd
    assert viewer.annotations["features"][0]["geometry"]["coordinates"] == [
        [[0.0, 0.0], [2.0, -1.0], [4.0, 0.0], [4.0, 4.0]]
    ]
    assert viewer.status == "edited #a1 (vertex)"
    # last-event slot: a replay is the no-op over the pushed set
    before = viewer.annotations
    viewer.annotation_edit = cmd
    assert viewer.annotations == before


def test_annotation_edit_add_vertex_unknown_id_is_a_silent_noop(viewer):
    _seed_polygon(viewer)
    before = viewer.annotations
    with warnings.catch_warnings(record=True) as rec:
        warnings.simplefilter("always")
        viewer.annotation_edit = {"op": "add_vertex", "id": "a9",
                                  "index": 0, "x": 1, "y": 1}
    assert not rec
    assert viewer.annotations == before
    assert viewer.status == "edit ignored: unknown annotation id"


def test_annotation_edit_add_vertex_refusals_keep_state(viewer):
    _seed_polygon(viewer)
    before = viewer.annotations
    with pytest.warns(UserWarning):
        # out of range: the triangle has segments 0..2 (2 = closing edge)
        viewer.annotation_edit = {"op": "add_vertex", "id": "a1",
                                  "index": 3, "x": 1, "y": 1}
    assert viewer.annotations == before
    assert viewer.status.startswith("edit ignored: add_vertex")
    with pytest.warns(UserWarning):
        # on top of an existing vertex (the segment's near end)
        viewer.annotation_edit = {"op": "add_vertex", "id": "a1",
                                  "index": 0, "x": 0, "y": 0}
    assert viewer.annotations == before
    assert viewer.status.startswith("edit ignored: add_vertex")


def test_public_api_add_annotation_vertex(viewer):
    _seed_polygon(viewer)
    viewer.add_annotation_vertex("a1", 1, 5, 2)
    assert viewer.annotation_edit == {
        "op": "add_vertex", "id": "a1", "index": 1, "x": 5.0, "y": 2.0}
    assert viewer.annotations["features"][0]["geometry"]["coordinates"] == [
        [[0.0, 0.0], [4.0, 0.0], [5.0, 2.0], [4.0, 4.0]]
    ]
    assert viewer.status == "edited #a1 (vertex)"

    before = viewer.annotations
    with pytest.warns(UserWarning):
        # the closing edge's far end is an existing vertex: refused
        viewer.add_annotation_vertex("a1", 3, 0, 0)
    assert viewer.annotations == before
    assert viewer.status.startswith("edit ignored: add_vertex")


def test_annotation_edit_idempotent_set_ops(viewer):
    _seed(viewer)
    cmd = {"op": "set_label", "id": "a1", "label": "tumor"}
    viewer.annotation_edit = cmd
    once = viewer.annotations
    viewer.annotation_edit = cmd  # replay: same result
    assert viewer.annotations == once
    assert viewer.status == "edited #a1 (label)"


# ------------------------------------------------------------- public API
def test_public_api_issues_commands_through_the_trait(viewer):
    _seed(viewer)
    viewer.set_annotation_label("a2", "mitosis")
    assert viewer.annotation_edit == {
        "op": "set_label", "id": "a2", "label": "mitosis"}
    assert viewer.annotations["features"][1]["properties"]["label"] == "mitosis"

    viewer.set_annotation_color("a1", color="blue", fill=None)
    # fill=None drops the key: the canonical form has no None values
    assert viewer.annotations["features"][0]["properties"] == {
        "label": "x", "color": "blue"}

    viewer.delete_annotation("a2")
    assert viewer.annotation_edit == {"op": "delete", "id": "a2"}
    assert [f["id"] for f in viewer.annotations["features"]] == ["a1"]


def test_public_api_unknown_id_is_a_silent_noop(viewer):
    _seed(viewer)
    before = viewer.annotations
    with warnings.catch_warnings(record=True) as rec:
        warnings.simplefilter("always")
        viewer.delete_annotation("nope")
        viewer.set_annotation_label("nope", "x")
        viewer.set_annotation_color("nope", color="red", fill=None)
    assert not rec
    assert viewer.annotations == before
    assert viewer.status == "edit ignored: unknown annotation id"
