"""Pure GeoJSON -> normalized annotation shapes (M2, DESIGN.md §6.3).

Read-only annotation import: a GeoJSON document (``FeatureCollection`` /
``Feature`` / bare geometry) is normalized into a list of shape dicts in
level-0 slide px (origin at the slide origin, **y down**, no CRS):

    {id, kind, points, label, color, fill}

* ``id``     : str (the feature ``id`` member, else the fallback ``"a{n}"``)
* ``kind``   : "point" | "line" | "polygon"
* ``points`` : point    -> ``[[x, y]]``
               line     -> ``[[x, y], ...]``
               polygon  -> list of rings, each a list of ``[x, y]``
               (holes preserved; the renderer fills with the evenodd rule)
* ``label``  : str | None  (drawn next to points; plain text, not HTML)
* ``color``  : CSS color | None  (stroke/outline; renderer defaults to black)
* ``fill``   : CSS color | None  (interior; renderer defaults to transparent)

Coordinate units: ``units="px"`` (default) means the document's coordinates
are already level-0 px; ``units="um"`` means microns from the slide origin,
converted with ``mpp`` (``px = um / mpp``; one mpp for both axes — the
slide's mpp-x, see ``SlideMeta.mpp``).

Validation contract: document-level structure is strict (``ValueError``:
non-dict document, unknown type, missing ``features``/``geometry``/
``geometries``/``coordinates`` members, non-dict ``properties``, non-numeric
or boolean or non-finite coordinates, rings with <3 positions, 1-point
LineStrings, non-string ``label``/``color``/``fill``). Degenerate but
well-formed geometry (zero-area polygon, zero-length line) is skipped with a
``warnings.warn``. A Feature whose ``geometry`` is ``null`` is a legal no-op.
Coordinates outside the slide are legal; the renderer culls/clips them.
"""
from __future__ import annotations

import math
import warnings
from typing import Any

__all__ = ["parse_annotations", "normalize_ring"]

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


def parse_annotations(
    doc: Any, units: str = "px", mpp: float | None = None
) -> list[dict]:
    """Normalize a GeoJSON annotation document into a shape list.

    ``doc`` is the parsed document (dict). Returns new shape dicts; nothing
    is mutated, and the input may be shared.

    Raises ``ValueError`` on malformed input (see module docstring) or on
    ``units="um"`` without a positive finite ``mpp``.
    """
    if units not in ("px", "um"):
        raise ValueError(f"units must be 'px' or 'um', got {units!r}")
    scale = 1.0
    if units == "um":
        if (
            isinstance(mpp, bool)
            or not isinstance(mpp, (int, float))
            or not math.isfinite(mpp)
            or mpp <= 0
        ):
            raise ValueError(
                "units='um' requires a positive finite mpp (slide mpp-x)"
            )
        scale = 1.0 / float(mpp)

    shapes: list[dict] = []
    n = 0
    for geom, props, fid in _document_features(doc):
        if geom is None:
            continue  # Feature with null geometry: legal, produces no shapes
        label = _opt_str(props, "label")
        color = _opt_str(props, "color")
        fill = _opt_str(props, "fill")
        for kind, points in _expand_geometry(geom, scale):
            shapes.append(
                {
                    "id": str(fid) if fid is not None else f"a{n}",
                    "kind": kind,
                    "points": points,
                    "label": label,
                    "color": color,
                    "fill": fill,
                }
            )
            n += 1
    return shapes


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
    props = feature.get("properties") or {}
    if not isinstance(props, dict):
        raise ValueError("feature 'properties' must be an object")
    fid = feature.get("id")
    if fid is not None and (
        isinstance(fid, bool) or not isinstance(fid, (str, int, float))
    ):
        raise ValueError("feature 'id' must be a string or number")
    return (geom, props, fid)


def _opt_str(props: dict, key: str) -> str | None:
    v = props.get(key)
    if v is None:
        return None
    if not isinstance(v, str):
        raise ValueError(
            f"property {key!r} must be a string, got {type(v).__name__}"
        )
    return v


# ---------------------------------------------------------------------------
# geometry expansion


def _expand_geometry(geom: Any, scale: float) -> list[tuple[str, list]]:
    """Yield ``(kind, scaled points)`` for each simple shape in a geometry."""
    if not isinstance(geom, dict):
        raise ValueError("geometry must be an object")
    gtype = geom.get("type")
    if gtype not in _GEOMETRY_TYPES:
        raise ValueError(f"unsupported geometry type: {gtype!r}")
    coords = geom.get("coordinates")
    out: list[tuple[str, list]] = []

    if gtype == "Point":
        x, y = _coord(coords, gtype)
        out.append(("point", [[x * scale, y * scale]]))
    elif gtype == "MultiPoint":
        if not isinstance(coords, list):
            raise ValueError("MultiPoint coordinates must be a list of positions")
        for p in coords:
            x, y = _coord(p, gtype)
            out.append(("point", [[x * scale, y * scale]]))
    elif gtype == "LineString":
        pts = _positions(coords, gtype, min_len=2)
        if _path_length(pts) < _DEGENERATE_LENGTH:
            warnings.warn(
                f"islide: skipping degenerate {gtype} annotation "
                "(zero length)",
                UserWarning,
            )
            return []
        out.append(("line", [[x * scale, y * scale] for x, y in pts]))
    elif gtype == "Polygon":
        rings = _rings(coords, gtype, scale)
        if rings:
            out.append(("polygon", rings))
    elif gtype == "MultiPolygon":
        if not isinstance(coords, list):
            raise ValueError("MultiPolygon coordinates must be a list of polygons")
        for poly in coords:
            rings = _rings(poly, gtype, scale)
            if rings:
                out.append(("polygon", rings))
    elif gtype == "GeometryCollection":
        geoms = geom.get("geometries")
        if not isinstance(geoms, list):
            raise ValueError("GeometryCollection requires a 'geometries' list")
        for g in geoms:
            out.extend(_expand_geometry(g, scale))
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


def _rings(coords: Any, what: str, scale: float) -> list[list[list[float]]] | None:
    """Outer + hole rings scaled to level-0 px, or ``None`` if degenerate."""
    if not isinstance(coords, list) or not coords:
        raise ValueError(f"{what}: coordinates must be a non-empty list of rings")
    # Strict structural check (the GeoJSON contract), then the shared
    # normalization for the numerics.
    rings: list[list[list[float]]] = []
    for ring in coords:
        if not isinstance(ring, list) or len(ring) < 3:
            raise ValueError(f"{what}: each ring needs at least 3 positions")
        pts = [_coord(p, what) for p in ring]
        rings.append([[x * scale, y * scale] for x, y in pts])
    norm = [normalize_ring(r) for r in rings]
    if any(r is None for r in norm):
        warnings.warn(
            f"islide: skipping degenerate {what} annotation (zero area)",
            UserWarning,
        )
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
