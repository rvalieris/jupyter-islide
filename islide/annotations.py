"""Pure GeoJSON -> canonical annotation document (M3.5, DESIGN.md §6.3).

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
  ``ValueError`` — including the latent M2 case of a
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
            # they cannot share one id (the latent M2 bug, now a hard error).
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
    unchecked (M3.5)."""
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
    drawn rings (`SlideViewer.last_polygon`, M3) and imported GeoJSON rings
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
# M4: edit commands


# M4 edit operations (DESIGN.md §6.5): the `annotation_edit` last-event
# command is discriminated by `op`; the ops are idempotent over a
# normalized document (a replayed `delete` finds no feature, the replayed
# set ops store the same values).
EDIT_OPS = ("delete", "set_label", "set_color")


def apply_edit(doc: dict, cmd: Any) -> dict | None:
    """Purely apply one M4 edit command to the canonical document.

    ``cmd`` is one of (DESIGN.md §6.5):

        {"op": "delete", "id": <str>}
        {"op": "set_label", "id": <str>, "label": <str | None>}
        {"op": "set_color", "id": <str>, "color": <str | None>,
         "fill": <str | None>}

    Returns a **new** document, or ``None`` if ``id`` does not address a
    feature of ``doc`` (a stale id — the caller keeps the current state).
    ``set_label`` stores ``label`` (``""``/whitespace-only/``None`` = no
    label — the ``label`` key is dropped); ``set_color`` stores the
    ``color``/``fill`` pair (``None`` members are dropped — the M2
    use-the-default convention: black stroke, transparent fill). The
    canonical form has no ``None``-valued properties. The input is never
    mutated; the returned document shares
    geometry objects with it (the widget re-normalizes through
    ``parse_annotations`` on assignment). Raises ``ValueError`` on a
    malformed command: unknown op, missing/empty/non-string ``id``, or a
    non-string ``label``/``color``/``fill`` (missing keys count as
    malformed — the wire form always carries them).
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
    # set_color
    for key in ("color", "fill"):
        if key not in cmd:
            raise ValueError(f"set_color command needs a {key!r} member")
    return _set_props(doc, idx, {"color": _edit_text(cmd["color"]), "fill": _edit_text(cmd["fill"])})


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
