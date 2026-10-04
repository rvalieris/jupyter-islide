"""Pure GeoJSON -> canonical annotation document (docs/DESIGN.md §6.3).

Read-only annotation import: a GeoJSON document (``FeatureCollection`` /
``Feature`` / bare geometry) is normalized into the **canonical annotation
document**, the value the ``annotations`` trait holds and the JS view
renders read-only:

    {"type": "FeatureCollection", "features": [
        {"type": "Feature", "id": "<str>", "geometry": <geometry>,
         "properties": <object>}, ...]}

* **Level-0 slide px** (origin at the slide origin, **y down**, no CRS):
  the document's coordinates already are level-0 slide px — no other unit
  convention is supported. A position's third component (z) is dropped.
* **Structure-preserving**: a ``Point``/``MultiPoint``/``LineString``/
  ``Polygon``/``MultiPolygon`` stays one feature, whole (a ``Multi*`` keeps
  all of its members). A ``GeometryCollection`` expands to one feature per
  member geometry (recursively), each stamped with the feature's
  ``properties``.
* **Rings** (all polygon rings, holes included) go through
  ``normalize_ring``: finite 2D positions, **stored open** (a redundant
  closing position is dropped). A ``Polygon`` with any degenerate ring —
  including a hole — is dropped (``UserWarning``); a ``MultiPolygon`` loses
  degenerate islands (warning each); a ``LineString`` below the length
  threshold is dropped (warning); a feature left without geometry is
  dropped (warning).
* **Ids**: a feature's ``id`` (str/int/float -> ``str``) if present, else a
  fresh ``aN``; ids are unique across the document and a collision raises
  ``ValueError`` — including the case of a
  ``GeometryCollection`` feature *with* an ``id`` expanding into 2+
  members (its id cannot be shared).
* **Properties**: a ``null``-valued key is dropped (the canonical form
  has no ``None`` values — absence is the no-value state);
  ``label``/``color``/``fill`` must be strings (or absent); every other
  key (and its value) passes through untouched.

Validation contract: document-level structure is strict (``ValueError``:
non-dict document, unknown type, missing ``features``/``geometry``/
``geometries``/``coordinates`` members, non-dict ``properties``, non-string
``label``/``color``/``fill``, non-numeric or boolean or non-finite
coordinates, rings with <3 positions, 1-point LineStrings, non-string or
boolean feature ``id``, duplicate ids). A Feature whose ``geometry`` is
``null`` is a legal no-op. Coordinates outside the slide are legal; the
renderer culls/clips them.
"""
from __future__ import annotations

import math
import warnings
from typing import Any

__all__ = ["parse_annotations", "normalize_ring", "apply_edit", "EDIT_OPS"]

_GEOMETRY_TYPES = (
    "Point",
    "MultiPoint",
    "LineString",
    "Polygon",
    "MultiPolygon",
    "GeometryCollection",
)

# Level-0 px thresholds below which a shape is invisible at any supported
# zoom: treated as degenerate (skipped with a warning).
_DEGENERATE_AREA = 1e-6      # px^2
_DEGENERATE_LENGTH = 1e-6    # px


def parse_annotations(doc: Any) -> dict:
    """Normalize a GeoJSON annotation document into the canonical document.

    ``doc`` is the parsed document (dict): a GeoJSON ``FeatureCollection``,
    a single ``Feature``, or a bare geometry (all six types). Coordinates
    are level-0 slide px. Returns a *new* document of the form
    ``{"type": "FeatureCollection", "features":
    [{type, id, geometry, properties}, ...]}`` (see the module docstring);
    nothing is mutated, and the input may be shared.

    Raises ``ValueError`` on malformed input; degenerate but well-formed
    members are skipped with a ``UserWarning``.
    """
    features: list[dict] = []
    used: set[str] = set()
    for geom, props, fid in _document_features(doc):
        if geom is None:
            continue  # Feature with null geometry: legal, produces nothing
        _check_props(props)
        leaves = _expand_geometry(geom)
        if not leaves:
            if geom["type"] in ("MultiPolygon", "GeometryCollection"):
                warnings.warn(
                    "islide: dropping annotation feature "
                    "(no surviving geometry)",
                    UserWarning,
                )
            continue
        fid_s = None if fid is None else str(fid)
        if fid_s is not None and len(leaves) > 1:
            # A GeometryCollection feature expands into several features;
            # they cannot share one id (a hard error).
            raise ValueError(
                f"duplicate annotation id {fid_s!r}: a GeometryCollection "
                f"feature expands into {len(leaves)} features, which cannot "
                "share one id"
            )
        for i, leaf in enumerate(leaves):
            features.append(
                {
                    "type": "Feature",
                    "id": _take_id(used, fid_s if i == 0 else None),
                    "geometry": leaf,
                    "properties": {
                        k: v for k, v in props.items() if v is not None
                    },
                }
            )
    return {"type": "FeatureCollection", "features": features}


# ---------------------------------------------------------------------------
# document / feature structure


def _document_features(doc: Any) -> list[tuple[dict | None, dict, Any]]:
    """Expand a document into ``[(geometry, properties, feature_id), ...]``."""
    if not isinstance(doc, dict):
        raise ValueError(
            "annotation document must be a parsed GeoJSON object (dict)"
        )
    dtype = doc.get("type")
    if dtype == "FeatureCollection":
        feats = doc.get("features")
        if not isinstance(feats, list):
            raise ValueError("FeatureCollection requires a 'features' list")
        return [_feature_parts(f) for f in feats]
    if dtype == "Feature":
        return [_feature_parts(doc)]
    if dtype in _GEOMETRY_TYPES:
        return [(doc, {}, None)]
    raise ValueError(
        f"unsupported annotation document type: {dtype!r} "
        "(expected FeatureCollection, Feature, or a geometry object)"
    )


def _feature_parts(feature: Any) -> tuple[dict | None, dict, Any]:
    if not isinstance(feature, dict) or feature.get("type") != "Feature":
        raise ValueError("FeatureCollection members must be Feature objects")
    if "geometry" not in feature:
        raise ValueError("feature requires a 'geometry' member")
    geom = feature["geometry"]
    if geom is not None and not isinstance(geom, dict):
        raise ValueError("feature 'geometry' must be an object or null")
    props = feature.get("properties")
    if props is None:
        props = {}
    elif not isinstance(props, dict):
        raise ValueError("feature 'properties' must be an object")
    fid = feature.get("id")
    if fid is not None and (
        isinstance(fid, bool) or not isinstance(fid, (str, int, float))
    ):
        raise ValueError("feature 'id' must be a string or number")
    return (geom, props, fid)


def _check_props(props: dict) -> None:
    """The renderer reads exactly label/color/fill from properties: those
    must be strings (or absent/null). Everything else passes through whole,
    unchecked."""
    for key in ("label", "color", "fill"):
        v = props.get(key)
        if v is not None and not isinstance(v, str):
            raise ValueError(
                f"property {key!r} must be a string, got {type(v).__name__}"
            )


def _take_id(used: set[str], fid: str | None) -> str:
    """Reserve an id: ``fid`` if given, else a fresh ``aN``. Raises
    ``ValueError`` on a collision (ids are unique across the document)."""
    if fid is None:
        n = 0
        while f"a{n}" in used:
            n += 1
        fid = f"a{n}"
    elif fid in used:
        raise ValueError(f"duplicate annotation id {fid!r}")
    used.add(fid)
    return fid


# ---------------------------------------------------------------------------
# geometry expansion


def _expand_geometry(geom: dict) -> list[dict]:
    """Expand a geometry into canonical leaf geometries (level-0 px).

    ``Point``/``MultiPoint``/``LineString``/``Polygon``/``MultiPolygon``
    normalize to themselves (one leaf each); a ``GeometryCollection``
    expands to its member leaves (recursively). Degenerate members are
    skipped with a ``UserWarning``; an empty result means the feature is
    dropped (the parse loop adds the feature-level warning for containers).
    """
    gtype = geom["type"]
    coords = geom.get("coordinates")

    if gtype == "Point":
        x, y = _coord(coords, gtype)
        return [{"type": "Point", "coordinates": [x, y]}]

    if gtype == "MultiPoint":
        positions = _positions(coords, gtype, min_len=0)
        return [
            {
                "type": "MultiPoint",
                "coordinates": [[x, y] for x, y in positions],
            }
        ]

    if gtype == "LineString":
        pts = [
            [x, y]
            for x, y in _positions(coords, gtype, min_len=2)
        ]
        if _path_length(pts) < _DEGENERATE_LENGTH:
            warnings.warn(
                f"islide: skipping degenerate {gtype} annotation "
                "(zero length)",
                UserWarning,
            )
            return []
        return [{"type": "LineString", "coordinates": pts}]

    if gtype == "Polygon":
        rings = _rings(coords, gtype)
        if rings is None:
            warnings.warn(
                f"islide: skipping degenerate {gtype} annotation (zero area)",
                UserWarning,
            )
            return []
        return [{"type": "Polygon", "coordinates": rings}]

    if gtype == "MultiPolygon":
        if not isinstance(coords, list):
            raise ValueError(
                "MultiPolygon coordinates must be a list of polygons"
            )
        islands = []
        for island in coords:
            rings = _rings(island, gtype)
            if rings is None:
                warnings.warn(
                    f"islide: skipping degenerate {gtype} annotation "
                    "(zero area)",
                    UserWarning,
                )
            else:
                islands.append(rings)
        return [{"type": "MultiPolygon", "coordinates": islands}] if islands else []

    # GeometryCollection: expand the members (recursively).
    geoms = geom.get("geometries")
    if not isinstance(geoms, list):
        raise ValueError("GeometryCollection requires a 'geometries' list")
    out: list[dict] = []
    for g in geoms:
        if g is None:
            continue
        out.extend(_expand_geometry(g))
    return out


def _coord(position: Any, what: str) -> tuple[float, float]:
    if not isinstance(position, (list, tuple)) or len(position) < 2:
        raise ValueError(f"{what}: coordinate must be [x, y]")
    x, y = position[0], position[1]
    if (
        isinstance(x, bool)
        or isinstance(y, bool)
        or not isinstance(x, (int, float))
        or not isinstance(y, (int, float))
    ):
        raise ValueError(f"{what}: coordinate values must be numbers")
    if not (math.isfinite(x) and math.isfinite(y)):
        raise ValueError(f"{what}: non-finite coordinate {list(position)[:2]!r}")
    return (float(x), float(y))


def _positions(
    coords: Any, what: str, min_len: int
) -> list[tuple[float, float]]:
    if not isinstance(coords, list):
        raise ValueError(f"{what}: coordinates must be a list of positions")
    if len(coords) < min_len:
        raise ValueError(f"{what}: needs at least {min_len} positions")
    return [_coord(p, what) for p in coords]


def _position_pair(pos: Any) -> tuple[float, float] | None:
    """A position as a `(float(x), float(y))` tuple, or ``None``."""
    if isinstance(pos, (list, tuple)) and len(pos) == 2:
        x, y = pos
        if (
            isinstance(x, (int, float))
            and not isinstance(x, bool)
            and isinstance(y, (int, float))
            and not isinstance(y, bool)
            and math.isfinite(x)
            and math.isfinite(y)
        ):
            return float(x), float(y)
    return None


def normalize_ring(ring: Any) -> list[list[float]] | None:
    """
    Normalize a position sequence (a polygon *ring*, level-0 px) to its
    canonical form: a list of `[x, y]` floats, **open** (redundant closing
    position removed), in input order.

    This is the single normalization every polygon ring goes through, so
    drawn rings (`SlideViewer.last_polygon`) and imported GeoJSON rings
    (below) cannot diverge. Returns ``None`` for a degenerate ring — fewer
    than 3 points, a non-finite coordinate, or zero area (collinear) — and
    also ``None`` for malformed input; callers that need strict structural
    errors check structure first (as `parse_annotations` does).
    """
    if not isinstance(ring, (list, tuple)) or len(ring) < 3:
        return None
    pts: list[tuple[float, float]] = []
    for pos in ring:
        xy = _position_pair(pos)
        if xy is None:
            return None
        pts.append(xy)
    # Drop a redundant closing position (GeoJSON-style closed ring).
    if len(pts) > 3 and pts[0] == pts[-1]:
        pts = pts[:-1]
    if len(pts) < 3 or _ring_area(pts) < _DEGENERATE_AREA:
        return None
    return [[x, y] for x, y in pts]


def _rings(coords: Any, what: str) -> list[list[list[float]]] | None:
    """Outer + hole rings in level-0 px, or ``None`` if any ring is
    degenerate (the caller decides the warning)."""
    if not isinstance(coords, list) or not coords:
        raise ValueError(f"{what}: coordinates must be a non-empty list of rings")
    # Strict structural check (the GeoJSON contract), then the shared
    # normalization for the numerics.
    rings: list[list[list[float]]] = []
    for ring in coords:
        if not isinstance(ring, list) or len(ring) < 3:
            raise ValueError(f"{what}: each ring needs at least 3 positions")
        pts = [_coord(p, what) for p in ring]
        rings.append([[x, y] for x, y in pts])
    norm = [normalize_ring(r) for r in rings]
    if any(r is None for r in norm):
        return None
    return norm


def _ring_area(pts: list[tuple[float, float]]) -> float:
    """Shoelace area of a ring (closed implicitly)."""
    a = 0.0
    n = len(pts)
    for i in range(n):
        x0, y0 = pts[i]
        x1, y1 = pts[(i + 1) % n]
        a += x0 * y1 - x1 * y0
    return abs(a) / 2.0



def _path_length(pts: list[tuple[float, float]]) -> float:
    total = 0.0
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        total += math.hypot(x1 - x0, y1 - y0)
    return total


# ---------------------------------------------------------------------------
# Edit commands


# Edit operations (docs/DESIGN.md §6.5): the `annotation_edit` last-event
# command is discriminated by `op`; the ops are idempotent over a
# normalized document (a replayed `delete` finds no feature, the replayed
# set ops store the same values; a replayed `set_vertex` stores the same
# position; a replayed `add_vertex` finds its position already at the
# insertion spot and is a no-op).
EDIT_OPS = ("delete", "set_label", "set_color", "set_vertex", "add_vertex")


def apply_edit(doc: dict, cmd: Any) -> dict | None:
    """Purely apply one edit command to the canonical document.

    ``cmd`` is one of (docs/DESIGN.md §6.5):

        {"op": "delete", "id": <str>}
        {"op": "set_label", "id": <str>, "label": <str | None>}
        {"op": "set_color", "id": <str>, "color": <str | None>,
         "fill": <str | None>}
        {"op": "set_vertex", "id": <str>, "index": <int>, "x": <float>,
         "y": <float>}
        {"op": "add_vertex", "id": <str>, "index": <int>, "x": <float>,
         "y": <float>}

    Returns a **new** document, or ``None`` if ``id`` does not address a
    feature of ``doc`` (a stale id — the caller keeps the current state).
    ``set_label`` stores ``label`` (``""``/whitespace-only/``None`` = no
    label — the ``label`` key is dropped); ``set_color`` stores the
    ``color``/``fill`` pair (``None`` members are dropped — the
    use-the-default convention: black stroke, transparent fill).
    ``set_vertex`` moves one position of the feature — the flat
    ``index`` in the feature's canonical position order (a Point's single
    position; a MultiPoint's / LineString's positions; a Polygon's rings
    in order — outer first, then holes, positions within a ring; a
    MultiPolygon's islands in order, rings within an island, positions
    within a ring) — to level-0 px ``(x, y)`` (docs/annotations.md);
    the moved
    geometry must keep the document's degeneracy invariants (every ring
    at or above the area threshold, a LineString at or above the length
    threshold) — a move that degenerates the geometry raises
    ``ValueError``.
    ``add_vertex`` inserts one position of ``[x, y]`` (level-0 px) at
    flat segment ``index`` in the feature's canonical segment order (a
    LineString's ``n`` positions give ``n - 1`` segments; a ring of ``n``
    positions gives ``n`` segments — the last being the closing edge back
    to the ring's first position; a Polygon's rings in order, a
    MultiPolygon's islands in order, rings within an island): the new
    position is placed between the segment's two endpoints (appended to
    the ring for a closing edge); the same degeneracy invariants apply —
    an insertion that degenerates the geometry or one that lands on top
    of an existing vertex raises ``ValueError``; an insertion whose
    position already sits at the insertion spot is a replay (the input
    document is returned unchanged). The
    canonical form has no ``None``-valued properties. The input is never
    mutated; the returned document shares
    geometry objects with it (the widget re-normalizes through
    ``parse_annotations`` on assignment). Raises ``ValueError`` on a
    malformed command: unknown op, missing/empty/non-string ``id``, a
    non-string ``label``/``color``/``fill`` (missing keys count as
    malformed — the wire form always carries them), or ``set_vertex`` /
    ``add_vertex`` ``index``/``x``/``y`` that are not an int / finite
    numbers, or a ``set_vertex`` ``index`` that does not address a
    position of the feature, or an ``add_vertex`` ``index`` that does not
    address a segment of the feature.
    """
    if not isinstance(doc, dict) or not isinstance(doc.get("features"), list):
        raise ValueError("annotation document must be a FeatureCollection")
    if not isinstance(cmd, dict):
        raise ValueError(f"edit command must be an object, got {type(cmd).__name__}")
    op = cmd.get("op")
    fid = cmd.get("id")
    if op not in EDIT_OPS:
        raise ValueError(f"unknown edit op {op!r} (expected one of {EDIT_OPS})")
    if not isinstance(fid, str) or not fid:
        raise ValueError(f"edit command 'id' must be a non-empty string, got {fid!r}")
    idx = _feature_index(doc, fid)
    if idx is None:
        return None
    if op == "delete":
        feats = doc["features"]
        return {"type": "FeatureCollection", "features": [f for i, f in enumerate(feats) if i != idx]}
    if op == "set_label":
        if "label" not in cmd:
            raise ValueError("set_label command needs a 'label' member")
        return _set_props(doc, idx, {"label": _edit_text(cmd["label"])})
    if op == "set_color":
        for key in ("color", "fill"):
            if key not in cmd:
                raise ValueError(f"set_color command needs a {key!r} member")
        return _set_props(doc, idx, {"color": _edit_text(cmd["color"]), "fill": _edit_text(cmd["fill"])})
    if op == "add_vertex":
        index = cmd.get("index")
        if isinstance(index, bool) or not isinstance(index, int):
            raise ValueError(f"add_vertex 'index' must be an integer, got {index!r}")
        x = _vertex_number(cmd.get("x"), "x", "add_vertex")
        y = _vertex_number(cmd.get("y"), "y", "add_vertex")
        geom = doc["features"][idx]["geometry"]
        count = _segment_count(geom)
        if not (0 <= index < count):
            raise ValueError(
                f"add_vertex 'index' {index} is out of range "
                f"(the feature has {count} segment{'s' if count != 1 else ''})"
            )
        moved, kind = _insert_vertex(geom, index, (x, y))
        if kind == "noop":
            return doc  # replayed insert: the position is already there
        if kind == "coincident":
            raise ValueError(
                "add_vertex would place a vertex on top of an existing one"
            )
        if _is_degenerate(moved):
            raise ValueError(
                "add_vertex would make the geometry degenerate "
                "(a zero-area ring or a zero-length line)"
            )
        feature = dict(doc["features"][idx])
        feature["geometry"] = moved
        out = list(doc["features"])
        out[idx] = feature
        return {"type": "FeatureCollection", "features": out}
    # set_vertex — the unguarded tail (only reachable with op == "set_vertex":
    # delete / set_label / set_color / add_vertex all returned above)
    index = cmd.get("index")
    if isinstance(index, bool) or not isinstance(index, int):
        raise ValueError(
            f"set_vertex 'index' must be an integer, got {index!r}"
        )
    x = _vertex_number(cmd.get("x"), "x")
    y = _vertex_number(cmd.get("y"), "y")
    geom = doc["features"][idx]["geometry"]
    count = _vertex_count(geom)
    if not (0 <= index < count):
        raise ValueError(
            f"set_vertex 'index' {index} is out of range "
            f"(the feature has {count} position{'s' if count != 1 else ''})"
        )
    moved = _move_vertex(geom, index, (x, y))
    if moved is None:  # defensive: the count check above already covered it
        raise ValueError(f"set_vertex 'index' {index} is out of range")
    if _is_degenerate(moved):
        raise ValueError(
            "set_vertex would make the geometry degenerate "
            "(a zero-area ring or a zero-length line)"
        )
    feature = dict(doc["features"][idx])
    feature["geometry"] = moved
    out = list(doc["features"])
    out[idx] = feature
    return {"type": "FeatureCollection", "features": out}


def _vertex_number(v: Any, what: str, op: str = "set_vertex") -> float:
    """A ``set_vertex`` / ``add_vertex`` ``x``/``y`` member: a finite
    number (level-0 px)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValueError(
            f"{op} '{what}' must be a number, got {type(v).__name__}"
        )
    if not math.isfinite(v):
        raise ValueError(f"{op} '{what}' must be finite, got {v!r}")
    return float(v)


def _canonical_rings(geometry: dict) -> list | None:
    """A polygon-family geometry's rings in canonical position order —
    a Polygon's rings (outer first, then holes); a MultiPolygon's islands
    in order, rings within each island. ``None`` for other geometry types
    (and malformed coordinates)."""
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if not isinstance(coords, list):
        return None
    if gtype == "Polygon":
        return [r for r in coords if isinstance(r, list)]
    if gtype == "MultiPolygon":
        rings = []
        for island in coords:
            if isinstance(island, list):
                rings.extend(r for r in island if isinstance(r, list))
        return rings
    return None


def _vertex_count(geometry: dict) -> int:
    """How many positions the feature has, in the canonical position order
    that a ``set_vertex`` ``index`` addresses: a Point's single position;
    a MultiPoint's / LineString's positions; a Polygon's ring positions
    (rings in order, positions within a ring); a MultiPolygon's
    island-then-ring positions."""
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if not isinstance(coords, list):
        return 0
    if gtype == "Point":
        return 1
    if gtype in ("MultiPoint", "LineString"):
        return len(coords)
    rings = _canonical_rings(geometry)
    return sum(len(r) for r in rings) if rings is not None else 0


def _segment_count(geometry: dict) -> int:
    """How many segments a flat ``add_vertex`` ``index`` can address:
    a LineString's ``n`` positions give ``n - 1``; a ring of ``n``
    positions gives ``n`` (the closing edge back to its first position
    counts); Point / MultiPoint give ``0``."""
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if not isinstance(coords, list):
        return 0
    if gtype == "LineString":
        return max(0, len(coords) - 1)
    rings = _canonical_rings(geometry)
    if rings is None:
        return 0
    return sum(len(r) for r in rings)


def _insert_vertex(
    geometry: dict, seg: int, point: tuple[float, float]
) -> tuple[dict, str]:
    """Insert position ``point`` at flat segment ``seg`` (canonical
    order), returning ``(geometry, kind)``: kind is ``'inserted'`` (the
    new geometry — the input is never mutated), ``'noop'`` (a position
    equal to ``point`` already sits at the insertion spot — the replay
    case, the input returned unchanged), or ``'coincident'`` (``point``
    lies on top of the segment's other endpoint — a degenerate insert).
    """
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if gtype == "LineString":
        if not (0 <= seg < len(coords) - 1):
            return geometry, "coincident"
        a = coords[seg]
        if (a[0], a[1]) == point:
            return geometry, "coincident"
        b = coords[seg + 1]
        if (b[0], b[1]) == point:
            return geometry, "noop"  # the insertion spot already holds it
        new_coords = coords[: seg + 1] + [[point[0], point[1]]] + coords[seg + 1:]
        return {**geometry, "coordinates": new_coords}, "inserted"
    rings = _canonical_rings(geometry)
    if rings is None:
        return geometry, "coincident"
    offset = 0
    for ring in rings:
        n = len(ring)
        if offset <= seg < offset + n:
            local = seg - offset
            a = ring[local]
            b = ring[(local + 1) % n]
            if (a[0], a[1]) == point:
                return geometry, "coincident"
            if local == n - 1:  # the closing edge
                if (b[0], b[1]) == point:
                    return geometry, "coincident"  # no insertion spot to replay from
                new_ring = ring + [[point[0], point[1]]]
            else:
                if (b[0], b[1]) == point:
                    return geometry, "noop"  # the insertion spot already holds it
                new_ring = ring[: local + 1] + [[point[0], point[1]]] + ring[local + 1:]
            if gtype == "Polygon":
                geom2 = {
                    **geometry,
                    "coordinates": [new_ring if r is ring else r for r in coords],
                }
            else:
                geom2 = {**geometry, "coordinates": [
                    [new_ring if r is ring else r for r in island]
                    if isinstance(island, list) and ring in island
                    else island
                    for island in coords
                ]}
            return geom2, "inserted"
        offset += n
    return geometry, "coincident"


def _move_vertex(geometry: dict, index: int, point: tuple[float, float]) -> dict | None:
    """The feature's geometry with the position at ``index`` (canonical
    position order) replaced by ``point`` — a new geometry; the input is
    never mutated. ``None`` if ``index`` does not address a position."""
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if not isinstance(coords, list):
        return None
    if gtype == "Point":
        if index != 0:
            return None
        return {**geometry, "coordinates": [point[0], point[1]]}
    if gtype in ("MultiPoint", "LineString"):
        if not (0 <= index < len(coords)):
            return None
        return {
            **geometry,
            "coordinates": [
                [point[0], point[1]] if i == index else [p[0], p[1]]
                for i, p in enumerate(coords)
            ],
        }
    rings = _canonical_rings(geometry)
    if rings is None:
        return None
    offset = 0
    for ring in rings:
        if offset <= index < offset + len(ring):
            local = index - offset
            new_ring = [
                [point[0], point[1]] if j == local else [p[0], p[1]]
                for j, p in enumerate(ring)
            ]
            if gtype == "Polygon":
                return {
                    **geometry,
                    "coordinates": [new_ring if r is ring else r for r in coords],
                }
            return {
                **geometry,
                "coordinates": [
                    [new_ring if r is ring else r for r in island]
                    if isinstance(island, list) and ring in island
                    else island
                    for island in coords
                ],
            }
        offset += len(ring)
    return None


def _is_degenerate(geometry: dict) -> bool:
    """Whether the geometry breaks the document's degeneracy invariants:
    a ring below the area threshold, or a LineString below the length
    threshold (Points and MultiPoints cannot be degenerate)."""
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if not isinstance(coords, list):
        return False
    if gtype == "Polygon":
        return any(
            _ring_area(r) < _DEGENERATE_AREA
            for r in coords
            if isinstance(r, list) and len(r) >= 3
        )
    if gtype == "MultiPolygon":
        return any(
            _ring_area(r) < _DEGENERATE_AREA
            for island in coords
            if isinstance(island, list)
            for r in island
            if isinstance(r, list) and len(r) >= 3
        )
    if gtype == "LineString":
        pts = [
            (p[0], p[1])
            for p in coords
            if isinstance(p, list) and len(p) >= 2
        ]
        return _path_length(pts) < _DEGENERATE_LENGTH
    return False


def _feature_index(doc: dict, fid: str) -> int | None:
    """Index of the feature addressed by ``fid`` (ids are unique across the
    normalized document), else ``None`` (a stale id)."""
    for i, f in enumerate(doc["features"]):
        if isinstance(f, dict) and f.get("id") == fid:
            return i
    return None


def _edit_text(v: Any) -> str | None:
    """A command string-or-null member: ``null`` passes through (no label /
    use-the-default); ``""``/whitespace-only -> ``null``."""
    if v is None:
        return None
    if not isinstance(v, str):
        raise ValueError(f"edit command member must be a string or null, got {type(v).__name__}")
    return v if v.strip() else None


def _set_props(doc: dict, idx: int, updates: dict) -> dict:
    f = doc["features"][idx]
    props = dict(f.get("properties") or {})
    for key, v in updates.items():
        # the canonical form has no None values: a cleared member is the
        # key's absence
        if v is None:
            props.pop(key, None)
        else:
            props[key] = v
    feature = dict(f)
    feature["properties"] = props
    out = list(doc["features"])
    out[idx] = feature
    return {"type": "FeatureCollection", "features": out}
