/**
 * Annotation overlay (M2 primitives; M3.5 canonical document).
 *
 * `drawAnnotations` renders the synced `annotations` trait — the canonical
 * annotation document, a GeoJSON FeatureCollection in level-0 px (DESIGN.md
 * §6.3) — read-only on the overlay canvas. Each feature keeps its geometry
 * type: `Point`/`MultiPoint` draw white-haloed circle markers (one per
 * position), `LineString` strokes an open path, `Polygon`/`MultiPolygon`
 * fill with the evenodd rule (holes cut out; a MultiPolygon's islands
 * flatten into one ring set) and outline every ring. `label`/`color`/`fill`
 * (and future styling keys) are read from the feature's `properties`
 * verbatim: `label` is 12 px screen-space text at the feature's first
 * vertex (white text halo), `color` is the stroke/outline/marker-body
 * fallback (default black), `fill` is the marker body / polygon interior
 * (default: marker falls back to `color`, polygon interior transparent).
 *
 * Culling: a feature whose level-0 bbox does not intersect the canvas (plus
 * a screen-constant margin) is not drawn. A feature whose geometry the
 * renderer cannot draw is skipped (the one-line guard below) and the rest
 * still draw.
 *
 * M4 (DESIGN.md §6.5): `selectedId` marks one feature as selected — it is
 * drawn last (topmost) with a `SELECTED_COLOR` accent stroke at
 * `SELECTED_STROKE_WIDTH` (a selected point gets an accent ring around its
 * marker); `hitTest` is the pure screen-space counterpart of the draw.
 *
 * Style constants are screen-space (CSS px) and do not scale with zoom:
 * the compositor draws the overlay after resetting the CTM (see view.js).
 */
import * as math from './tilemath.js';

export const DEFAULT_COLOR = '#000000';
export const STROKE_WIDTH = 1.5; // screen px (lines, polygon outlines)
export const POINT_RADIUS = 4; // screen px
export const HALO_EXTRA = 1.5; // white ring around point bodies, screen px
export const LABEL_FONT = '12px sans-serif';
export const LABEL_HALO = 3; // white text halo, screen px
// M4 selection (screen px): the accent stroke of a selected feature and
// the hit-test tolerances (a line is hit within LINE_HIT_TOLERANCE of any
// segment; a polygon outline is hit within STROKE_WIDTH of a ring edge).
export const SELECTED_COLOR = '#ff8c00';
export const SELECTED_STROKE_WIDTH = 3;
export const POINT_HIT_RADIUS = POINT_RADIUS + HALO_EXTRA + 2; // ~8 px
export const LINE_HIT_TOLERANCE = 6;

/**
 * Draw every feature of the annotation document under `transform` onto
 * `ctx`. `annotations` is the canonical document (a GeoJSON
 * FeatureCollection; see DESIGN.md §6.3); a missing/invalid document
 * draws nothing. `alpha` (0..1) sets the whole layer's opacity via
 * `globalAlpha`. `selectedId` (M4) marks the selected feature: it is
 * drawn last (topmost) with the accent style. Returns the number of
 * features drawn (culled ones excluded).
 */
export function drawAnnotations(
  ctx,
  { transform: t, annotations, alpha = 1, selectedId = null },
) {
  const features =
    annotations && Array.isArray(annotations.features)
      ? annotations.features
      : [];
  if (features.length === 0) return 0;
  const a = Math.min(1, Math.max(0, Number(alpha) || 0));
  const [vx0, vy0] = math.screenToL0(t, 0, 0);
  const [vx1, vy1] = math.screenToL0(t, t.canvasW, t.canvasH);
  const margin = (POINT_RADIUS + HALO_EXTRA + LABEL_HALO) / t.zoom; // l0 px
  const x0 = vx0 - margin, y0 = vy0 - margin, x1 = vx1 + margin, y1 = vy1 + margin;

  ctx.save();
  ctx.globalAlpha = a;
  let n = 0;
  let selected = null;
  for (const f of features) {
    const g = f && f.geometry;
    // One-line guard: the positions of a drawable canonical geometry, or
    // null if this feature cannot be drawn (skipped; the rest still draw).
    const positions = geometryPositions(g);
    if (!positions) continue;
    const [bx0, by0, bx1, by1] = bboxOf(positions);
    if (bx0 > x1 || bx1 < x0 || by0 > y1 || by1 < y0) continue;
    if (selectedId !== null && f.id === selectedId) {
      selected = { f, g, positions }; // drawn last (topmost)
      continue;
    }
    drawFeature(ctx, t, f, g, positions, false);
    n += 1;
  }
  if (selected) {
    drawFeature(ctx, t, selected.f, selected.g, selected.positions, true);
    n += 1;
  }
  ctx.restore();
  return n;
}

/**
 * M4 hit-test (DESIGN.md §6.5): the id of the topmost (last-in-document)
 * feature under a screen point `(x, y)`, or `null` (a miss). Points and
 * markers: within `POINT_HIT_RADIUS` screen px of a marker position.
 * Lines: within `LINE_HIT_TOLERANCE` screen px of any segment. Polygons:
 * inside under the evenodd parity used for the fill (holes excluded,
 * MultiPolygon islands included) or on the outline (within
 * `STROKE_WIDTH` screen px of a ring edge — the visible outline is a hit).
 * Undrawable features (the renderer's guard) are unhittable. Pure: reads
 * the document and the transform, touches no canvas.
 */
export function hitTest(annotations, transform, x, y) {
  const features =
    annotations && Array.isArray(annotations.features)
      ? annotations.features
      : [];
  // Reverse scan: the renderer draws in document order, so the last
  // feature is topmost.
  for (let i = features.length - 1; i >= 0; i--) {
    const f = features[i];
    const g = f && f.geometry;
    const positions = geometryPositions(g);
    if (!positions) continue;
    const type = g.type;
    let hit = false;
    if (type === 'Point' || type === 'MultiPoint') {
      hit = positionsNear(positions, transform, x, y, POINT_HIT_RADIUS);
    } else if (type === 'LineString') {
      hit = segmentsNear(positions, transform, x, y, LINE_HIT_TOLERANCE);
    } else {
      hit = ringsHit(ringsOf(g), transform, x, y);
    }
    if (hit) return f.id != null ? f.id : null;
  }
  return null;
}

// ------------------------------------------------------------------ M4

function positionsNear(positions, t, x, y, r) {
  for (const [x0, y0] of positions) {
    const [sx, sy] = math.l0ToScreen(t, x0, y0);
    if (screenDist2(sx, sy, x, y) <= r * r) return true;
  }
  return false;
}

/** Within `tol` screen px of any consecutive segment of `positions`. */
function segmentsNear(positions, t, x, y, tol) {
  let [ax, ay] = math.l0ToScreen(t, positions[0][0], positions[0][1]);
  for (let i = 1; i < positions.length; i++) {
    const [bx, by] = math.l0ToScreen(t, positions[i][0], positions[i][1]);
    if (segmentDist2(ax, ay, bx, by, x, y) <= tol * tol) return true;
    [ax, ay] = [bx, by];
  }
  return false;
}

/** evenodd interior (all rings at once, so holes and MultiPolygon islands
 * behave like the fill) or within `STROKE_WIDTH` screen px of an outline
 * edge (the stroke is drawn closed: the closing edge is checked too).
 */
function ringsHit(rings, t, x, y) {
  let inside = false;
  let outline = false;
  for (const ring of rings) {
    let [ax, ay] = math.l0ToScreen(t, ring[0][0], ring[0][1]);
    // Canonical rings are stored open; the closing edge (last -> first)
    // is part of the fill and of the stroked outline, so it participates
    // in both checks below.
    for (let i = 1; i <= ring.length; i++) {
      const p = ring[i % ring.length];
      const [bx, by] = math.l0ToScreen(t, p[0], p[1]);
      // evenodd raycast (horizontal ray to the left, y-up in screen px is
      // irrelevant — the test is symmetric in x).
      if ((ay > y) !== (by > y)) {
        const xi = ax + (y - ay) * (bx - ax) / (by - ay);
        if (x < xi) inside = !inside;
      }
      if (
        !outline &&
        segmentDist2(ax, ay, bx, by, x, y) <= STROKE_WIDTH * STROKE_WIDTH
      ) {
        outline = true;
      }
      [ax, ay] = [bx, by];
    }
  }
  return inside || outline;
}

function screenDist2(px, py, x, y) {
  const dx = x - px, dy = y - py;
  return dx * dx + dy * dy;
}

/** Squared distance from (x, y) to segment (ax, ay)-(bx, by). */
function segmentDist2(ax, ay, bx, by, x, y) {
  const dx = bx - ax, dy = by - ay;
  const l2 = dx * dx + dy * dy;
  let u = l2 === 0 ? 0 : ((x - ax) * dx + (y - ay) * dy) / l2;
  u = Math.max(0, Math.min(1, u));
  const px = ax + u * dx, py = ay + u * dy;
  const ex = x - px, ey = y - py;
  return ex * ex + ey * ey;
}

function drawFeature(ctx, t, f, g, positions, selected) {
  const props = (f && f.properties) || {};
  const baseColor = typeof props.color === 'string' ? props.color : DEFAULT_COLOR;
  const fill = typeof props.fill === 'string' ? props.fill : null;
  const label = typeof props.label === 'string' ? props.label : null;
  const type = g.type;

  if (type === 'Point' || type === 'MultiPoint') {
    drawMarkers(ctx, t, positions, baseColor, fill);
    if (selected) drawSelectedRing(ctx, t, positions);
    if (label) {
      drawLabel(ctx, t, positions[0], label, selected ? SELECTED_COLOR : baseColor,
                POINT_RADIUS + 2);
    }
  } else if (type === 'LineString') {
    traceRings(ctx, t, [positions], false);
    if (selected) {
      ctx.strokeStyle = SELECTED_COLOR;
      ctx.lineWidth = SELECTED_STROKE_WIDTH;
    } else {
      ctx.strokeStyle = baseColor;
      ctx.lineWidth = STROKE_WIDTH;
    }
    ctx.lineJoin = 'round';
    ctx.lineCap = 'round';
    ctx.stroke();
    if (label) {
      drawLabel(ctx, t, positions[0], label, selected ? SELECTED_COLOR : baseColor, 2);
    }
  } else {
    // Polygon / MultiPolygon: every ring (islands flattened) in one
    // evenodd path.
    traceRings(ctx, t, ringsOf(g), true);
    if (fill && fill !== 'transparent') {
      ctx.fillStyle = fill;
      ctx.fill('evenodd');
    }
    if (selected) {
      ctx.strokeStyle = SELECTED_COLOR;
      ctx.lineWidth = SELECTED_STROKE_WIDTH;
    } else {
      ctx.strokeStyle = baseColor;
      ctx.lineWidth = STROKE_WIDTH;
    }
    ctx.lineJoin = 'round';
    ctx.stroke();
    if (label) {
      drawLabel(ctx, t, positions[0], label, selected ? SELECTED_COLOR : baseColor, 2);
    }
  }
}

/** The M4 accent ring around a selected point's marker: an
 * `SELECTED_COLOR` stroke at `SELECTED_STROKE_WIDTH` spanning the halo. */
function drawSelectedRing(ctx, t, positions) {
  for (const [x, y] of positions) {
    const [sx, sy] = math.l0ToScreen(t, x, y);
    ctx.beginPath();
    ctx.arc(sx, sy, POINT_RADIUS + HALO_EXTRA, 0, 2 * Math.PI);
    ctx.strokeStyle = SELECTED_COLOR;
    ctx.lineWidth = SELECTED_STROKE_WIDTH;
    ctx.stroke();
  }
}

/**
 * The positions of a canonical leaf geometry, flattened (a MultiPolygon's
 * islands included), or null if the geometry is not one the renderer can
 * draw. Canonical input is producer-validated (see islide/annotations.py);
 * this guard is the renderer's defensive one-line skip for a bad feature.
 */
function geometryPositions(g) {
  if (!g || typeof g !== 'object' || !Array.isArray(g.coordinates)) return null;
  const t = g.type;
  if (t === 'Point') {
    return isPosition(g.coordinates) ? [g.coordinates] : null;
  }
  if (t === 'MultiPoint' || t === 'LineString') {
    if (!g.coordinates.length || !g.coordinates.every(isPosition)) return null;
    return g.coordinates;
  }
  if (t === 'Polygon' || t === 'MultiPolygon') {
    const out = [];
    for (const ring of ringsOf(g)) {
      if (!isRing(ring)) return null;
      for (const p of ring) out.push(p);
    }
    return out.length ? out : null;
  }
  return null;
}

/** A Polygon's or MultiPolygon's rings (MultiPolygon's islands flattened). */
function ringsOf(g) {
  const out = [];
  if (g.type === 'Polygon') {
    for (const ring of g.coordinates) out.push(ring);
  } else {
    for (const island of g.coordinates) for (const ring of island) out.push(ring);
  }
  return out;
}

function isNumber(v) {
  return typeof v === 'number' && Number.isFinite(v);
}

function isPosition(p) {
  return Array.isArray(p) && p.length >= 2 && isNumber(p[0]) && isNumber(p[1]);
}

function isRing(ring) {
  return Array.isArray(ring) && ring.length >= 3 && ring.every(isPosition);
}

/** Level-0 bbox over a feature's positions: [x0, y0, x1, y1]. */
function bboxOf(positions) {
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const [x, y] of positions) {
    if (x < x0) x0 = x;
    if (x > x1) x1 = x;
    if (y < y0) y0 = y;
    if (y > y1) y1 = y;
  }
  return [x0, y0, x1, y1];
}

function drawMarkers(ctx, t, positions, color, fill) {
  for (const [x, y] of positions) {
    const [sx, sy] = math.l0ToScreen(t, x, y);
    ctx.beginPath();
    ctx.arc(sx, sy, POINT_RADIUS + HALO_EXTRA, 0, 2 * Math.PI);
    ctx.fillStyle = '#ffffff';
    ctx.fill();
    ctx.beginPath();
    ctx.arc(sx, sy, POINT_RADIUS, 0, 2 * Math.PI);
    ctx.fillStyle = fill || color;
    ctx.fill();
  }
}

/**
 * Trace rings into the current path: each ring starts with its own moveTo
 * (lineTo the rest), so polygon holes cut through under evenodd fill and a
 * MultiPolygon's islands flatten into one ring set. With `close`, each ring
 * is closed (a closed canvas path, not a position — canonical rings are
 * stored open) which polygon outlines need: `stroke()`, unlike `fill()`,
 * does not close subpaths on its own.
 */
function traceRings(ctx, t, rings, close = false) {
  ctx.beginPath();
  for (const ring of rings) {
    const [fx, fy] = math.l0ToScreen(t, ring[0][0], ring[0][1]);
    ctx.moveTo(fx, fy);
    for (let i = 1; i < ring.length; i++) {
      const [px, py] = math.l0ToScreen(t, ring[i][0], ring[i][1]);
      ctx.lineTo(px, py);
    }
    if (close) ctx.closePath();
  }
}

/** 12 px screen-space text at a level-0 position, white text halo. */
function drawLabel(ctx, t, [x, y], label, color, dx) {
  const [sx, sy] = math.l0ToScreen(t, x, y);
  ctx.font = LABEL_FONT;
  ctx.textBaseline = 'middle';
  ctx.lineJoin = 'round';
  ctx.lineWidth = LABEL_HALO;
  ctx.strokeStyle = '#ffffff';
  ctx.strokeText(label, sx + dx, sy);
  ctx.fillStyle = color;
  ctx.fillText(label, sx + dx, sy);
}
