/**
 * Annotation overlay (M2). Pure function over a canvas 2D context
 * (duck-typed), so it can be unit-tested with a mock ctx in Node.
 *
 * Shapes (from the synced Python `annotations` trait; DESIGN.md §6.3):
 *   { id, kind: "point" | "line" | "polygon",
 *     points,             // point: [[x, y]]; line: [[x, y], ...];
 *                         // polygon: list of rings of [x, y] (holes kept)
 *     label: string | null, color: string | null, fill: string | null }
 * in level-0 slide px (origin at the slide origin, y down), reprojected
 * under the view's local transform.
 *
 * Styling is screen-constant (does not scale with zoom):
 *   strokes 1.5 px in `color` (default black); polygon interiors `fill`
 *   (default transparent; evenodd so holes cut through); points are
 *   ~4 px circles (body `fill` else `color`) with a white halo for
 *   contrast; labels are 12 px screen-space text next to points (white
 *   text halo, text in `color` else black).
 * Culling: shapes whose level-0 bbox does not intersect the canvas (plus a
 * screen-constant margin) are not drawn.
 */
import * as math from './tilemath.js';

export const DEFAULT_COLOR = '#000000';
export const STROKE_WIDTH = 1.5; // screen px (lines, polygon outlines)
export const POINT_RADIUS = 4; // screen px
export const HALO_EXTRA = 1.5; // white ring around point bodies, screen px
export const LABEL_FONT = '12px sans-serif';
export const LABEL_HALO = 3; // white text halo, screen px

/**
 * Draw every annotation in `shapes` under `transform` onto `ctx`.
 * `alpha` (0..1) sets the whole layer's opacity via `globalAlpha`.
 * Returns the number of shapes drawn (culled ones excluded).
 */
export function drawAnnotations(ctx, { transform: t, shapes, alpha = 1 }) {
  if (!Array.isArray(shapes) || shapes.length === 0) return 0;
  const a = Math.min(1, Math.max(0, Number(alpha) || 0));
  const [vx0, vy0] = math.screenToL0(t, 0, 0);
  const [vx1, vy1] = math.screenToL0(t, t.canvasW, t.canvasH);
  const margin = (POINT_RADIUS + HALO_EXTRA + LABEL_HALO) / t.zoom; // l0 px
  const x0 = vx0 - margin, y0 = vy0 - margin, x1 = vx1 + margin, y1 = vy1 + margin;

  ctx.save();
  ctx.globalAlpha = a;
  let n = 0;
  for (const s of shapes) {
    if (!s || !Array.isArray(s.points) || s.points.length === 0) continue;
    const [bx0, by0, bx1, by1] = shapeBbox(s.points);
    if (bx0 > x1 || bx1 < x0 || by0 > y1 || by1 < y0) continue;
    if (s.kind === 'point') drawPoint(ctx, t, s);
    else if (s.kind === 'line') drawLine(ctx, t, s);
    else if (s.kind === 'polygon') drawPolygon(ctx, t, s);
    n += 1;
  }
  ctx.restore();
  return n;
}

/**
 * Normalize a shape's points to a list of rings of [x, y]:
 * point/line carry one flat list of positions; polygon carries rings.
 */
function asRings(points) {
  return typeof points[0][0] === 'number' ? [points] : points;
}

/** Level-0 bbox over a shape's points (any kind): [x0, y0, x1, y1]. */
function shapeBbox(points) {
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const ring of asRings(points)) {
    for (const [x, y] of ring) {
      if (x < x0) x0 = x;
      if (x > x1) x1 = x;
      if (y < y0) y0 = y;
      if (y > y1) y1 = y;
    }
  }
  return [x0, y0, x1, y1];
}

function drawPoint(ctx, t, s) {
  const [x, y] = s.points[0];
  const [sx, sy] = math.l0ToScreen(t, x, y);
  const color = s.color || DEFAULT_COLOR;
  ctx.beginPath();
  ctx.arc(sx, sy, POINT_RADIUS + HALO_EXTRA, 0, 2 * Math.PI);
  ctx.fillStyle = '#ffffff';
  ctx.fill();
  ctx.beginPath();
  ctx.arc(sx, sy, POINT_RADIUS, 0, 2 * Math.PI);
  ctx.fillStyle = s.fill || color;
  ctx.fill();
  if (s.label) {
    const tx = sx + POINT_RADIUS + 2;
    ctx.font = LABEL_FONT;
    ctx.textBaseline = 'middle';
    ctx.lineJoin = 'round';
    ctx.lineWidth = LABEL_HALO;
    ctx.strokeStyle = '#ffffff';
    ctx.strokeText(s.label, tx, sy);
    ctx.fillStyle = color;
    ctx.fillText(s.label, tx, sy);
  }
}

/**
 * Trace `points` (a list of rings of [x, y]) as one path; each ring starts
 * with its own moveTo so polygon holes cut through under evenodd fill.
 * With `closeRings`, each ring is closed (last -> first), which polygon
 * outlines need: rings are stored open, and `stroke()` — unlike `fill()` —
 * does not close subpaths on its own.
 */
function tracePath(ctx, t, points, closeRings = false) {
  ctx.beginPath();
  for (const ring of asRings(points)) {
    const [fx, fy] = math.l0ToScreen(t, ring[0][0], ring[0][1]);
    ctx.moveTo(fx, fy);
    for (let i = 1; i < ring.length; i++) {
      const [px, py] = math.l0ToScreen(t, ring[i][0], ring[i][1]);
      ctx.lineTo(px, py);
    }
    if (closeRings) ctx.closePath();
  }
}

function drawLine(ctx, t, s) {
  tracePath(ctx, t, s.points);
  ctx.strokeStyle = s.color || DEFAULT_COLOR;
  ctx.lineWidth = STROKE_WIDTH;
  ctx.lineJoin = 'round';
  ctx.lineCap = 'round';
  ctx.stroke();
}

function drawPolygon(ctx, t, s) {
  tracePath(ctx, t, s.points, true);
  if (s.fill && s.fill !== 'transparent') {
    ctx.fillStyle = s.fill;
    ctx.fill('evenodd');
  }
  ctx.strokeStyle = s.color || DEFAULT_COLOR;
  ctx.lineWidth = STROKE_WIDTH;
  ctx.lineJoin = 'round';
  ctx.stroke();
}
